"""Video assembly — compose voiceover + b-roll + music into a draft .mp4.

Uses moviepy v2 (different API from v1: `.with_audio()` / `.subclipped()` /
`.resized()` style). Pulls b-roll from Pexels and Pixabay, caches downloads
under cache/stock/ (Pixabay TOS requires 24h caching anyway), and maintains
an asset-usage ledger so the same clip does not appear in two recent videos.
Generates a draft thumbnail via Pillow.

Records full provenance — including the license text captured at download
time — to records/<channel>/<ts>_assemble.json.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import requests

from .config import REPO_ROOT, ChannelConfig, EnvConfig
from .records import write_record

log = logging.getLogger(__name__)

PEXELS_VIDEOS_ENDPOINT = "https://api.pexels.com/videos/search"
PIXABAY_VIDEOS_ENDPOINT = "https://pixabay.com/api/videos/"

PEXELS_LICENSE_TEXT = (
    "Pexels License — free for commercial and personal use, "
    "no attribution required (https://www.pexels.com/license/)."
)
PIXABAY_LICENSE_TEXT = (
    "Pixabay Content License — free for commercial and non-commercial use, "
    "no attribution required (https://pixabay.com/service/license-summary/)."
)


def _pexels_search_videos(query: str, api_key: str, per_page: int) -> list[dict]:
    headers = {"Authorization": api_key}
    params = {
        "query": query,
        "per_page": per_page,
        "orientation": "landscape",
        "size": "medium",
    }
    resp = requests.get(PEXELS_VIDEOS_ENDPOINT, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    out: list[dict] = []
    for v in resp.json().get("videos", []):
        files = v.get("video_files", []) or []
        if not files:
            continue
        files = sorted(files, key=lambda f: f.get("width") or 0)
        chosen = next(
            (f for f in files if 720 <= (f.get("width") or 0) <= 1920),
            files[len(files) // 2],
        )
        out.append(
            {
                "source": "pexels",
                "id": v.get("id"),
                "download_url": chosen.get("link"),
                "page_url": v.get("url"),
                "width": chosen.get("width"),
                "height": chosen.get("height"),
                "duration_seconds": v.get("duration"),
                "photographer": (v.get("user") or {}).get("name"),
                "photographer_url": (v.get("user") or {}).get("url"),
                "license_text": PEXELS_LICENSE_TEXT,
                "query_used": query,
            }
        )
    return out


def _pixabay_search_videos(query: str, api_key: str, per_page: int) -> list[dict]:
    params = {"key": api_key, "q": query, "per_page": max(per_page, 3)}
    resp = requests.get(PIXABAY_VIDEOS_ENDPOINT, params=params, timeout=15)
    resp.raise_for_status()
    out: list[dict] = []
    for v in resp.json().get("hits", []):
        videos = v.get("videos") or {}
        chosen = videos.get("medium") or videos.get("large") or videos.get("small")
        if not chosen or not chosen.get("url"):
            continue
        out.append(
            {
                "source": "pixabay",
                "id": v.get("id"),
                "download_url": chosen["url"],
                "page_url": v.get("pageURL"),
                "width": chosen.get("width"),
                "height": chosen.get("height"),
                "duration_seconds": v.get("duration"),
                "photographer": v.get("user"),
                "photographer_url": (
                    f"https://pixabay.com/users/{v.get('user')}-{v.get('user_id')}/"
                ),
                "license_text": PIXABAY_LICENSE_TEXT,
                "query_used": query,
            }
        )
    return out


def _cache_dir() -> Path:
    d = REPO_ROOT / "cache" / "stock"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path_for(asset: dict) -> Path:
    ext = (asset.get("download_url") or "").rsplit(".", 1)[-1].split("?")[0].lower()
    if ext not in {"mp4", "mov", "webm", "m4v"}:
        ext = "mp4"
    return _cache_dir() / f"{asset['source']}_{asset['id']}.{ext}"


def _download_to_cache(asset: dict) -> Path:
    path = _cache_path_for(asset)
    if path.exists() and path.stat().st_size > 1024:
        return path
    resp = requests.get(asset["download_url"], stream=True, timeout=60)
    resp.raise_for_status()
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(chunk_size=256 * 1024):
            f.write(chunk)
    tmp.replace(path)
    return path


def _evict_cache(max_gb: float) -> None:
    cache = _cache_dir()
    files = sorted(cache.iterdir(), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files if p.is_file())
    max_bytes = max_gb * 1024 * 1024 * 1024
    while total > max_bytes and files:
        oldest = files.pop(0)
        if oldest.is_file():
            total -= oldest.stat().st_size
            try:
                oldest.unlink()
                log.info("cache evict: %s", oldest.name)
            except OSError as e:
                log.warning("cache evict failed for %s: %s", oldest.name, e)


def _ledger_path(channel_slug: str) -> Path:
    return REPO_ROOT / "records" / channel_slug / "_asset_usage_ledger.json"


def _load_ledger(channel_slug: str) -> list[dict]:
    p = _ledger_path(channel_slug)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_ledger(channel_slug: str, ledger: list[dict], keep_recent: int) -> None:
    # Keep a window larger than keep_recent for analytical purposes,
    # but only the most recent N participate in dedupe.
    trimmed = ledger[-max(keep_recent * 5, 200):]
    p = _ledger_path(channel_slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")


def _filter_unused(assets: list[dict], ledger: list[dict], keep_recent: int) -> list[dict]:
    recent_keys = {
        f"{u['source']}_{u['id']}" for u in ledger[-keep_recent:]
    }
    return [a for a in assets if f"{a['source']}_{a['id']}" not in recent_keys]


def _select_clips(needed: int, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    if len(candidates) >= needed:
        return random.sample(candidates, needed)
    # Not enough unique candidates — allow repeats but shuffle for variety
    out = list(candidates)
    while len(out) < needed:
        out.extend(random.sample(candidates, min(len(candidates), needed - len(out))))
    return out[:needed]


def _build_video(
    voice_audio_path: Path,
    clip_paths: list[Path],
    music_path: Path | None,
    output_path: Path,
    resolution: tuple[int, int],
    fps: int,
    clip_avg_seconds: int,
    music_volume: float,
) -> float:
    """Compose the video. Returns final duration in seconds."""
    from moviepy import (
        AudioFileClip,
        CompositeAudioClip,
        VideoFileClip,
        concatenate_videoclips,
    )
    from moviepy.audio.fx import AudioLoop, MultiplyVolume

    voice = AudioFileClip(str(voice_audio_path))
    duration = voice.duration
    target_w, target_h = resolution

    segments: list = []
    total = 0.0
    for cp in clip_paths:
        if total >= duration:
            break
        try:
            clip = VideoFileClip(str(cp), audio=False)
        except Exception as e:  # noqa: BLE001
            log.warning("skipping unreadable clip %s: %s", cp.name, e)
            continue

        seg_len = min(float(clip_avg_seconds), clip.duration, duration - total)
        start = max(0.0, (clip.duration - seg_len) / 2)
        seg = clip.subclipped(start, start + seg_len)

        # Resize while preserving aspect; center-crop to target if needed
        if seg.w != target_w or seg.h != target_h:
            scale = max(target_w / seg.w, target_h / seg.h)
            seg = seg.resized(new_size=(int(seg.w * scale), int(seg.h * scale)))
            # Center-crop
            x_off = max(0, (seg.w - target_w) // 2)
            y_off = max(0, (seg.h - target_h) // 2)
            seg = seg.cropped(x1=x_off, y1=y_off, width=target_w, height=target_h)

        segments.append(seg)
        total += seg_len

    if not segments:
        raise click.ClickException("All b-roll clips were unreadable.")

    video = concatenate_videoclips(segments, method="chain")
    if video.duration > duration:
        video = video.subclipped(0, duration)

    # Audio mix
    if music_path and music_path.exists():
        music = AudioFileClip(str(music_path)).with_effects([MultiplyVolume(music_volume)])
        if music.duration < duration:
            music = music.with_effects([AudioLoop(duration=duration)])
        else:
            music = music.subclipped(0, duration)
        final_audio = CompositeAudioClip([voice, music])
    else:
        final_audio = voice

    video = video.with_audio(final_audio).with_fps(fps)
    video.write_videofile(
        str(output_path),
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        logger=None,
    )
    video.close()
    voice.close()
    return float(duration)


def _build_thumbnail(title: str, output_path: Path, resolution: tuple[int, int]) -> None:
    """Draft thumbnail: warm cream title text on dark navy background."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", resolution, color=(20, 18, 28))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 72)
    except OSError:
        font = ImageFont.load_default()

    words = title.split()
    lines: list[str] = []
    current = ""
    max_text_w = resolution[0] - 120
    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) < max_text_w:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    line_h = 84
    total_h = line_h * len(lines)
    y0 = (resolution[1] - total_h) // 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (resolution[0] - line_w) // 2
        y = y0 + i * line_h
        draw.text((x + 3, y + 3), line, fill=(0, 0, 0), font=font)
        draw.text((x, y), line, fill=(245, 235, 210), font=font)

    img.save(str(output_path), "PNG")


def run_assemble(
    channel: ChannelConfig, env: EnvConfig, voice_audio: Path | None
) -> None:
    """Top-level entry: find inputs, build video + thumbnail, record provenance."""
    if voice_audio is None:
        vo_dir = REPO_ROOT / "output" / "voiceover" / channel.slug
        if vo_dir.exists():
            mp3s = sorted(vo_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
            if mp3s:
                voice_audio = mp3s[0]
        if voice_audio is None:
            raise click.ClickException(
                f"No voiceover mp3 in output/voiceover/{channel.slug}/. "
                f"Run 'python -m lantern voice' first."
            )
        print(f"Using latest voiceover: {voice_audio.relative_to(REPO_ROOT)}")
    else:
        # Resolve user-supplied relative paths to absolute so .relative_to(REPO_ROOT) works
        voice_audio = voice_audio.resolve()

    asm = channel.assemble
    res = (asm.resolution_width, asm.resolution_height)

    # 1. Search Pexels + Pixabay
    print("\n=== Searching stock sources ===")
    all_candidates: list[dict] = []
    for theme in channel.themes:
        if env.pexels_api_key:
            try:
                hits = _pexels_search_videos(theme, env.pexels_api_key, asm.clips_per_query)
                all_candidates.extend(hits)
                print(f"  Pexels  '{theme}': {len(hits)} videos")
            except Exception as e:  # noqa: BLE001
                log.warning("Pexels search failed for '%s': %s", theme, e)
            time.sleep(0.5)
        if env.pixabay_api_key:
            try:
                hits = _pixabay_search_videos(theme, env.pixabay_api_key, asm.clips_per_query)
                all_candidates.extend(hits)
                print(f"  Pixabay '{theme}': {len(hits)} videos")
            except Exception as e:  # noqa: BLE001
                log.warning("Pixabay search failed for '%s': %s", theme, e)
            time.sleep(0.5)

    if not all_candidates:
        raise click.ClickException(
            "Stock searches returned zero results. Check Pexels/Pixabay API keys + network."
        )
    print(f"Total candidates: {len(all_candidates)}")

    # 2. Dedupe vs recent-use ledger
    ledger = _load_ledger(channel.slug)
    fresh = _filter_unused(all_candidates, ledger, asm.ledger_keep_recent)
    if not fresh:
        log.warning("All candidates were used recently. Allowing repeats this run.")
        fresh = all_candidates
    else:
        print(f"After dedupe vs last {asm.ledger_keep_recent} videos: {len(fresh)}")

    # 3. Estimate clip count from voice duration
    from moviepy import AudioFileClip

    a = AudioFileClip(str(voice_audio))
    voice_duration = a.duration
    a.close()
    needed = max(2, int(voice_duration / asm.clip_seconds_avg) + 1)
    print(f"Voiceover {voice_duration:.1f}s -> need {needed} clip slots (~{asm.clip_seconds_avg}s each)")

    selected = _select_clips(needed, fresh)
    print(f"Selected {len(selected)} clips")

    # 4. Download
    print("\n=== Downloading to cache ===")
    clip_paths: list[Path] = []
    used: list[dict] = []
    for i, a_item in enumerate(selected, 1):
        try:
            path = _download_to_cache(a_item)
            clip_paths.append(path)
            used.append(a_item)
            print(f"  {i}/{len(selected)}: {a_item['source']}/{a_item['id']} -> {path.name}")
        except Exception as e:  # noqa: BLE001
            log.warning("download failed for %s/%s: %s", a_item["source"], a_item["id"], e)

    if not clip_paths:
        raise click.ClickException("All downloads failed.")

    ledger.extend({"source": u["source"], "id": u["id"]} for u in used)
    _save_ledger(channel.slug, ledger, asm.ledger_keep_recent)
    _evict_cache(env.stock_cache_max_gb)

    # 5. Music (optional)
    music_path: Path | None = None
    if asm.music_enabled:
        music_dir = REPO_ROOT / "assets" / "music"
        tracks = list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.wav"))
        if tracks:
            music_path = random.choice(tracks)
            print(f"\nMusic: {music_path.name}")
        else:
            print("\nMusic: none (assets/music/ is empty)")

    # 6. Build video + thumbnail
    output_dir = REPO_ROOT / "output" / "video" / channel.slug
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = voice_audio.stem
    video_path = output_dir / f"{stem}.mp4"
    thumb_path = output_dir / f"{stem}_thumb.png"
    title = stem.replace("-", " ").replace("_", " ").strip()

    print(f"\n=== Composing video ({res[0]}x{res[1]} @ {asm.fps}fps) ===")
    duration = _build_video(
        voice_audio_path=voice_audio,
        clip_paths=clip_paths,
        music_path=music_path,
        output_path=video_path,
        resolution=res,
        fps=asm.fps,
        clip_avg_seconds=asm.clip_seconds_avg,
        music_volume=asm.music_volume,
    )

    print("Generating draft thumbnail...")
    _build_thumbnail(title, thumb_path, (1280, 720))

    size_mb = video_path.stat().st_size / (1024 * 1024)
    print(f"\nVideo:     {video_path.relative_to(REPO_ROOT)} ({size_mb:.1f} MB, {duration:.1f}s)")
    print(f"Thumbnail: {thumb_path.relative_to(REPO_ROOT)}")

    # 7. Provenance record (full license text per asset)
    record_path = write_record(
        channel_slug=channel.slug,
        kind="assemble",
        payload={
            "voice_audio": str(voice_audio.relative_to(REPO_ROOT)),
            "video_path": str(video_path.relative_to(REPO_ROOT)),
            "thumbnail_path": str(thumb_path.relative_to(REPO_ROOT)),
            "video_duration_seconds": round(duration, 1),
            "video_size_bytes": video_path.stat().st_size,
            "resolution": list(res),
            "fps": asm.fps,
            "music_used": (str(music_path.relative_to(REPO_ROOT)) if music_path else None),
            "music_volume": asm.music_volume if music_path else None,
            "b_roll_assets": [
                {
                    "source": u["source"],
                    "id": u["id"],
                    "page_url": u["page_url"],
                    "download_url": u["download_url"],
                    "photographer": u["photographer"],
                    "photographer_url": u.get("photographer_url"),
                    "license_text": u["license_text"],
                    "width": u.get("width"),
                    "height": u.get("height"),
                    "duration_seconds": u.get("duration_seconds"),
                    "query_used": u.get("query_used"),
                }
                for u in used
            ],
            "b_roll_count": len(used),
        },
    )
    print(f"Record:    {record_path.relative_to(REPO_ROOT)}")
