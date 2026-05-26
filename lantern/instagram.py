"""Instagram Reels export — 9:16 vertical cut + caption .txt for manual posting.

**Never auto-posts.** Meta aggressively bans automated Instagram accounts.
This module produces a .mp4 + a .txt caption in output/instagram/<channel>/;
you open them and post manually.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import click

from .config import REPO_ROOT, ChannelConfig, EnvConfig
from .records import write_record

log = logging.getLogger(__name__)


def _latest_video(channel_slug: str) -> Path | None:
    """Return the most-recent .mp4.uploaded (preferred) or .mp4 in the channel's video dir."""
    vd = REPO_ROOT / "output" / "video" / channel_slug
    if not vd.exists():
        return None
    # Prefer uploaded (we know YouTube URL); fall back to plain .mp4
    uploaded = sorted(vd.glob("*.mp4.uploaded"), key=lambda p: p.stat().st_mtime, reverse=True)
    if uploaded:
        return uploaded[0]
    plain = sorted(vd.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return plain[0] if plain else None


def _find_youtube_url_for_stem(channel_slug: str, stem: str) -> str:
    """Look in records/<channel>/ for the upload record matching this stem; return Studio URL."""
    records_dir = REPO_ROOT / "records" / channel_slug
    if not records_dir.exists():
        return ""
    for rec in records_dir.glob("*_upload.json"):
        try:
            data = json.loads(rec.read_text(encoding="utf-8"))
            if stem in data.get("video_file", ""):
                vid_id = data.get("youtube_video_id")
                if vid_id:
                    return f"https://www.youtube.com/watch?v={vid_id}"
        except Exception:
            continue
    return ""


def _find_script_for_stem(channel_slug: str, stem: str) -> Path | None:
    primary = REPO_ROOT / "output" / "scripts" / channel_slug / f"{stem}.md"
    if primary.exists():
        return primary
    scripts_root = REPO_ROOT / "output" / "scripts"
    if scripts_root.exists():
        for sub in scripts_root.iterdir():
            if sub.is_dir():
                cand = sub / f"{stem}.md"
                if cand.exists():
                    return cand
    return None


def _extract_title_from_script(script_path: Path | None) -> str:
    if not script_path or not script_path.exists():
        return ""
    text = script_path.read_text(encoding="utf-8")
    # First line h1 (e.g. "# When everything falls apart")
    m = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    if m:
        return m.group(1).strip()
    # Fallback: **Title (...):** under metadata
    m2 = re.search(r"\*\*Title[^*]*\*\*\s*\n+([^\n*][^\n]*)", text, flags=re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return ""


def _strip_video_suffixes(name: str) -> str:
    """e.g. 'foo.mp4.uploaded' -> 'foo'  /  'foo.mp4' -> 'foo'."""
    for ext in (".mp4.uploaded", ".mp4.approved", ".mp4"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _build_caption(
    title: str,
    youtube_url: str,
    hashtags: list[str],
    template: str,
) -> str:
    tags_str = " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in hashtags)
    return template.format(
        title=title or "",
        youtube_url=youtube_url or "(upload to YouTube first to get the URL)",
        hashtags=tags_str,
    )


def _crop_and_resize_vertical(
    input_path: Path,
    output_path: Path,
    target_w: int,
    target_h: int,
    max_duration: float,
) -> float:
    """Crop input video to 9:16 center, resize to target, trim to max_duration. Returns final duration."""
    from moviepy import VideoFileClip

    src = VideoFileClip(str(input_path))
    duration = min(float(max_duration), src.duration)
    clip = src.subclipped(0, duration)

    # Decide whether to crop horizontally (typical for 16:9 source) or vertically
    target_aspect = target_w / target_h          # 1080/1920 = 0.5625
    src_aspect = src.w / src.h                   # 1920/1080 = 1.7778
    if src_aspect > target_aspect:
        # Source is wider than target -> crop sides, keep full height
        new_w = src.h * target_aspect
        x_off = (src.w - new_w) / 2
        clip = clip.cropped(x1=x_off, y1=0, x2=x_off + new_w, y2=src.h)
    elif src_aspect < target_aspect:
        # Source is taller than target -> crop top/bottom (rare for our case)
        new_h = src.w / target_aspect
        y_off = (src.h - new_h) / 2
        clip = clip.cropped(x1=0, y1=y_off, x2=src.w, y2=y_off + new_h)
    # Then resize to exact target
    clip = clip.resized(new_size=(target_w, target_h))

    clip.write_videofile(
        str(output_path),
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        logger=None,
    )
    clip.close()
    src.close()
    return duration


def run_instagram(
    channel: ChannelConfig,
    env: EnvConfig,
    specific_video: Path | None = None,
) -> None:
    """Build a 9:16 cut + caption .txt for manual posting to Instagram."""
    ig = channel.instagram
    if not ig.enabled:
        raise click.ClickException(
            "Instagram export disabled in channel config (instagram.enabled=false)."
        )

    if specific_video:
        src = specific_video.resolve()
    else:
        src = _latest_video(channel.slug)
        if src is None:
            raise click.ClickException(
                f"No video found in output/video/{channel.slug}/. "
                f"Run 'python -m lantern assemble' first."
            )
        print(f"Using latest video: {src.relative_to(REPO_ROOT)}")

    if not src.exists():
        raise click.ClickException(f"Video not found: {src}")

    stem = _strip_video_suffixes(src.name)

    # Pull title from script, YouTube URL from upload record (if uploaded yet)
    script_path = _find_script_for_stem(channel.slug, stem)
    title = _extract_title_from_script(script_path) or stem.replace("-", " ").replace("_", " ").title()
    youtube_url = _find_youtube_url_for_stem(channel.slug, stem)
    if not youtube_url:
        log.warning("No upload record yet for %s — caption will have placeholder URL.", stem)

    out_dir = REPO_ROOT / "output" / "instagram" / channel.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    video_out = out_dir / f"{stem}_reel.mp4"
    caption_out = out_dir / f"{stem}_reel.txt"

    print(f"\nSource:        {src.relative_to(REPO_ROOT)}")
    print(f"Title:         {title}")
    print(f"Target:        {ig.target_width}x{ig.target_height} (9:16)  max {ig.max_duration_seconds}s")
    print(f"YouTube URL:   {youtube_url or '(not uploaded yet)'}")
    print(f"Cropping + resizing...")

    duration = _crop_and_resize_vertical(
        input_path=src,
        output_path=video_out,
        target_w=ig.target_width,
        target_h=ig.target_height,
        max_duration=ig.max_duration_seconds,
    )

    caption_text = _build_caption(
        title=title,
        youtube_url=youtube_url,
        hashtags=ig.default_hashtags,
        template=ig.caption_template,
    )
    caption_out.write_text(caption_text, encoding="utf-8")

    size_mb = video_out.stat().st_size / 1024 / 1024
    print(f"\nReel:    {video_out.relative_to(REPO_ROOT)} ({size_mb:.1f} MB, {duration:.1f}s)")
    print(f"Caption: {caption_out.relative_to(REPO_ROOT)}")
    print()
    print("--- Caption preview ---")
    print(caption_text)
    print("--- end ---")
    print()
    print("DO MANUALLY: copy the .mp4 and the caption to Instagram yourself.")
    print("This module never posts. Meta automation gets accounts banned.")

    record_path = write_record(
        channel_slug=channel.slug,
        kind="instagram",
        payload={
            "source_video": str(src.relative_to(REPO_ROOT)),
            "reel_video": str(video_out.relative_to(REPO_ROOT)),
            "caption_file": str(caption_out.relative_to(REPO_ROOT)),
            "duration_seconds": round(duration, 1),
            "title": title,
            "youtube_url": youtube_url,
            "hashtags": ig.default_hashtags,
            "human_posted": False,    # operator posts manually
        },
    )
    print(f"Record:  {record_path.relative_to(REPO_ROOT)}")
