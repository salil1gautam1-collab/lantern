"""Trend research — surface ranked topic candidates from pytrends + YouTube Data API.

For each theme, YouTube is queried with multiple modifier-augmented variants
(e.g. "stoic wisdom", "stoic for hard times") instead of just the bare keyword,
which surfaces topical wisdom content instead of viral entertainment.

Per-run output: top N candidates printed to terminal AND full signal dump written
to `records/<channel>/<timestamp>_research.json` for audit.

Both signal sources are wrapped defensively: a failure in one (network, quota,
pytrends scraper breakage) does not take down the whole run.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import REPO_ROOT, ChannelConfig, EnvConfig
from .records import write_record

log = logging.getLogger(__name__)

try:
    from pytrends.request import TrendReq

    PYTRENDS_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    PYTRENDS_AVAILABLE = False
    log.warning("pytrends unavailable at import time: %s", e)


SHORTS_TAGS = ("#shorts", "#short", "#tiktok", "#reels")

# Unicode ranges for scripts other than Latin/common European. When the channel
# language is English, titles containing these are usually noise for our purposes.
NON_LATIN_RE = re.compile(
    r"["
    # NOTE: Devanagari (Hindi) is intentionally NOT in this list — the india-channel
    # operator reads Hindi, so Hindi-titled wisdom content stays in research signal.
    # If a future channel's operator doesn't read Hindi, add r"ऀ-ॿ" below.
    r"඀-෿"    # Sinhala
    r"ঀ-৿"    # Bengali
    r"਀-੿"    # Gurmukhi (Punjabi)
    r"଀-୿"    # Oriya
    r"஀-௿"    # Tamil
    r"ఀ-౿"    # Telugu
    r"ಀ-೿"    # Kannada
    r"ഀ-ൿ"    # Malayalam
    r"က-႟"    # Burmese (Myanmar)
    r"฀-๿"    # Thai
    r"຀-໿"    # Lao
    r"぀-ゟ"    # Japanese Hiragana
    r"゠-ヿ"    # Japanese Katakana
    r"一-鿿"    # CJK Unified Ideographs
    r"가-힯"    # Korean Hangul
    r"؀-ۿ"    # Arabic
    r"֐-׿"    # Hebrew
    r"Ѐ-ӿ"    # Cyrillic
    r"]"
)


@dataclass
class Candidate:
    text: str
    sources: list[str] = field(default_factory=list)
    trend_score: int = 0
    youtube_view_count: int = 0
    youtube_video_count: int = 0
    niche_fit_score: float = 0.0
    final_score: float = 0.0


def _parse_iso8601_duration(s: str) -> int:
    """Parse YouTube's ISO 8601 duration string ('PT5M30S') to seconds.

    Returns 0 on parse failure so a missing duration doesn't crash the run.
    """
    match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s or "")
    if not match:
        return 0
    h, m, sec = match.groups()
    return int(h or 0) * 3600 + int(m or 0) * 60 + int(sec or 0)


def _build_search_queries(themes: list[str], modifiers: list[str]) -> list[str]:
    """Cross-product themes with modifiers, e.g. ['stoic wisdom', 'stoic for hard times']."""
    return [f"{theme} {mod}" for theme in themes for mod in modifiers]


def _fetch_pytrends_related(themes: list[str], geo: str) -> dict[str, list[dict]]:
    """Return {theme: [{'query': str, 'value': int}, ...]} from Google Trends.

    Best-effort: 5s sleep between calls, one-time 15s backoff on HTTP 429,
    then give up for that theme.
    """
    if not PYTRENDS_AVAILABLE:
        return {t: [] for t in themes}

    try:
        pytrends = TrendReq(hl="en-US", tz=330)
    except Exception as e:  # noqa: BLE001
        log.warning("pytrends init failed: %s — skipping.", e)
        return {t: [] for t in themes}

    out: dict[str, list[dict]] = {}
    for theme in themes:
        for attempt in range(2):
            try:
                pytrends.build_payload([theme], cat=0, timeframe="today 1-m", geo=geo)
                related = pytrends.related_queries()
                top_df = related.get(theme, {}).get("top")
                if top_df is None or top_df.empty:
                    out[theme] = []
                else:
                    out[theme] = [
                        {"query": str(row["query"]), "value": int(row["value"])}
                        for _, row in top_df.head(10).iterrows()
                    ]
                time.sleep(5)
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "429" in msg and attempt == 0:
                    log.warning(
                        "pytrends 429 for theme '%s' — backing off 15s before retry",
                        theme,
                    )
                    time.sleep(15)
                    continue
                log.warning(
                    "pytrends failed for theme '%s' (attempt %d): %s",
                    theme,
                    attempt + 1,
                    e,
                )
                out[theme] = []
                break
        else:
            out.setdefault(theme, [])

    return out


def _fetch_youtube_for_queries(
    queries: list[str],
    api_key: str,
    region_code: str,
    days_back: int = 30,
    duration: str = "medium",
    exclude_shorts: bool = True,
    min_seconds: int = 90,
    exclude_non_latin: bool = True,
) -> dict[str, list[dict]]:
    """Return {query: [{title, video_id, view_count, duration_seconds, ...}]}.

    Per-query cost: ~101 quota units (100 for search.list + ~1 for videos.list batch).
    """
    youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    published_after = (
        (datetime.now(timezone.utc) - timedelta(days=days_back))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    out: dict[str, list[dict]] = {}
    for query in queries:
        try:
            search_resp = (
                youtube.search()
                .list(
                    part="snippet",
                    q=query,
                    type="video",
                    regionCode=region_code,
                    order="viewCount",
                    publishedAfter=published_after,
                    maxResults=10,
                    relevanceLanguage="en",
                    videoDuration=duration,
                )
                .execute()
            )
            items = search_resp.get("items", [])
            video_ids = [
                it["id"]["videoId"] for it in items if "videoId" in it.get("id", {})
            ]

            details: dict[str, dict] = {}
            if video_ids:
                detail_resp = (
                    youtube.videos()
                    .list(
                        part="statistics,contentDetails",
                        id=",".join(video_ids),
                    )
                    .execute()
                )
                for v in detail_resp.get("items", []):
                    details[v["id"]] = {
                        "view_count": int(v.get("statistics", {}).get("viewCount", 0)),
                        "duration_seconds": _parse_iso8601_duration(
                            v.get("contentDetails", {}).get("duration", "PT0S")
                        ),
                    }

            kept: list[dict] = []
            for it in items:
                vid = it["id"].get("videoId")
                if not vid:
                    continue
                title = it["snippet"]["title"]
                d = details.get(vid, {"view_count": 0, "duration_seconds": 0})
                if exclude_shorts:
                    if d["duration_seconds"] and d["duration_seconds"] < min_seconds:
                        continue
                    title_lc = title.lower()
                    if any(tag in title_lc for tag in SHORTS_TAGS):
                        continue
                if exclude_non_latin and NON_LATIN_RE.search(title):
                    continue
                kept.append(
                    {
                        "title": title,
                        "video_id": vid,
                        "channel_title": it["snippet"]["channelTitle"],
                        "view_count": d["view_count"],
                        "duration_seconds": d["duration_seconds"],
                    }
                )
            out[query] = kept
        except HttpError as e:
            log.warning("YouTube search failed for query '%s': %s", query, e)
            out[query] = []
        except Exception as e:  # noqa: BLE001
            log.warning("YouTube unexpected error for query '%s': %s", query, e)
            out[query] = []
        time.sleep(2)  # stay under YouTube's per-minute Search Queries quota

    return out


def _build_candidates(
    pytrends_data: dict[str, list[dict]],
    youtube_data: dict[str, list[dict]],
    themes: list[str],
) -> list[Candidate]:
    """Merge pytrends + YouTube signals into a ranked list of Candidate objects."""
    raw: dict[str, dict] = {}
    theme_set = {t.lower() for t in themes}

    def _niche_fit(text: str) -> float:
        text_lc = text.lower()
        matches = sum(1 for t in theme_set if t in text_lc)
        return matches / max(len(theme_set), 1)

    for queries in pytrends_data.values():
        for q in queries:
            key = q["query"].lower().strip()
            if not key:
                continue
            entry = raw.setdefault(
                key,
                {
                    "text": q["query"],
                    "sources": set(),
                    "trend_score": 0,
                    "youtube_view_count": 0,
                    "youtube_video_count": 0,
                },
            )
            entry["sources"].add("pytrends")
            entry["trend_score"] = max(entry["trend_score"], int(q["value"]))

    for videos in youtube_data.values():
        for v in videos:
            key = v["title"].lower().strip()
            if not key:
                continue
            entry = raw.setdefault(
                key,
                {
                    "text": v["title"],
                    "sources": set(),
                    "trend_score": 0,
                    "youtube_view_count": 0,
                    "youtube_video_count": 0,
                },
            )
            entry["sources"].add("youtube")
            entry["youtube_view_count"] += v["view_count"]
            entry["youtube_video_count"] += 1

    candidates: list[Candidate] = []
    for entry in raw.values():
        fit = _niche_fit(entry["text"])
        view_log = math.log10(entry["youtube_view_count"] + 1) / 7
        score = 0.5 * (entry["trend_score"] / 100) + 0.5 * min(view_log, 1.0)
        candidates.append(
            Candidate(
                text=entry["text"],
                sources=sorted(entry["sources"]),
                trend_score=entry["trend_score"],
                youtube_view_count=entry["youtube_view_count"],
                youtube_video_count=entry["youtube_video_count"],
                niche_fit_score=round(fit, 2),
                final_score=round(score, 3),
            )
        )

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    return candidates


def run_research(channel: ChannelConfig, env: EnvConfig) -> None:
    """Run the research pipeline and print results."""
    assert env.youtube_api_key, "guarded by cli.py"

    rc = channel.research
    queries = _build_search_queries(channel.themes, rc.query_modifiers)
    est_quota = len(queries) * 101  # search.list 100 + videos.list batch ~1

    print(
        f"\n=== Lantern research: channel '{channel.slug}' (region {channel.region}) ===\n"
    )
    print(f"Themes:    {', '.join(channel.themes)}")
    print(f"Modifiers: {', '.join(rc.query_modifiers)}")
    print(
        f"YouTube:   {len(queries)} queries  |  ~{est_quota} quota units  |  "
        f"region={rc.youtube_region_code}  duration={rc.youtube_duration}"
    )
    print(f"pytrends:  {len(channel.themes)} themes  |  geo={rc.pytrends_geo}")
    print("Pulling signals... (this typically takes 60-120 seconds)\n")

    pytrends_data = _fetch_pytrends_related(channel.themes, rc.pytrends_geo)
    youtube_data = _fetch_youtube_for_queries(
        queries,
        env.youtube_api_key,
        rc.youtube_region_code,
        duration=rc.youtube_duration,
        exclude_shorts=rc.exclude_shorts,
        min_seconds=rc.min_video_seconds,
        exclude_non_latin=rc.exclude_non_latin_titles,
    )

    n_pytrends = sum(len(v) for v in pytrends_data.values())
    n_youtube = sum(len(v) for v in youtube_data.values())
    print(
        f"pytrends: {n_pytrends} related queries across {len(channel.themes)} themes"
    )
    print(f"youtube:  {n_youtube} videos kept after shorts filter across {len(queries)} queries")

    if n_pytrends == 0 and n_youtube == 0:
        print("\nNo signals returned from any source. Check network + API key.")
        return

    candidates = _build_candidates(pytrends_data, youtube_data, channel.themes)
    top = candidates[: rc.candidates_per_run]

    print(f"\n=== Top {len(top)} candidates ===\n")
    for i, c in enumerate(top, 1):
        src = "+".join(c.sources)
        print(
            f"{i:2}. [{src:18}] score={c.final_score:.3f}  "
            f"trend={c.trend_score:3}  yt_views={c.youtube_view_count:>9}  fit={c.niche_fit_score:.2f}"
        )
        print(f"    {c.text}\n")

    record_path = write_record(
        channel_slug=channel.slug,
        kind="research",
        payload={
            "themes": channel.themes,
            "query_modifiers": rc.query_modifiers,
            "pytrends_geo": rc.pytrends_geo,
            "youtube_region_code": rc.youtube_region_code,
            "youtube_duration": rc.youtube_duration,
            "exclude_shorts": rc.exclude_shorts,
            "min_video_seconds": rc.min_video_seconds,
            "exclude_non_latin_titles": rc.exclude_non_latin_titles,
            "raw_signals": {
                "pytrends": pytrends_data,
                "youtube": youtube_data,
            },
            "candidates": [asdict(c) for c in candidates],
            "top_count": len(top),
        },
    )
    print(f"Record written: {record_path.relative_to(REPO_ROOT)}")
