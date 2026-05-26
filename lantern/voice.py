"""Voiceover synthesis via edge-tts (free Microsoft TTS, no API key)."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import click

from .config import REPO_ROOT, ChannelConfig, EnvConfig
from .records import write_record

log = logging.getLogger(__name__)

# Anything in the "## YouTube metadata" tail section of a script template
# is dashboard metadata, not narration. Cut it before TTS.
NARRATION_BOUNDARY_RE = re.compile(r"^##\s+YouTube metadata", re.MULTILINE)


def extract_narration_text(md: str) -> str:
    """Strip markdown, comments, and metadata; return the prose to narrate."""
    text = NARRATION_BOUNDARY_RE.split(md, maxsplit=1)[0]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)        # HTML comments
    text = re.sub(r"^#+\s+.*$", "", text, flags=re.MULTILINE)      # markdown headers
    text = re.sub(r"^---+\s*$", "", text, flags=re.MULTILINE)      # horizontal rules
    text = re.sub(r"^>\s*.*$", "", text, flags=re.MULTILINE)       # blockquote metadata
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)                  # **bold** -> bold
    text = re.sub(r"\n{3,}", "\n\n", text)                          # collapse extra blanks
    return text.strip()


def _latest_script(channel_slug: str) -> Path | None:
    """Return the most-recently-modified .md script for this channel, or None."""
    scripts_dir = REPO_ROOT / "output" / "scripts" / channel_slug
    if not scripts_dir.exists():
        return None
    md_files = sorted(
        scripts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return md_files[0] if md_files else None


async def _synth_async(
    text: str, voice: str, rate: str, volume: str, output_path: Path
) -> None:
    import edge_tts  # local import; only loaded when voice subcommand runs

    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    await communicate.save(str(output_path))


def synthesize(
    text: str, voice: str, rate: str, volume: str, output_path: Path
) -> None:
    """Render text to audio file via edge-tts. Blocks until complete."""
    asyncio.run(_synth_async(text, voice, rate, volume, output_path))


def run_voice(
    channel: ChannelConfig, env: EnvConfig, script_path: Path | None
) -> None:
    """Render the voiceover for a script. If script_path is None, use the latest."""
    if script_path is None:
        script_path = _latest_script(channel.slug)
        if script_path is None:
            raise click.ClickException(
                f"No scripts found in output/scripts/{channel.slug}/. "
                f"Run 'python -m lantern script --topic ...' first."
            )
        print(f"Using latest script: {script_path.relative_to(REPO_ROOT)}")

    if not script_path.exists():
        raise click.ClickException(f"Script not found: {script_path}")

    md = script_path.read_text(encoding="utf-8")
    text = extract_narration_text(md)

    word_count = len(text.split())
    if word_count < 5:
        raise click.ClickException(
            f"Script {script_path.name} has only {word_count} narration words after "
            f"stripping headers/comments/metadata. Did you write the body, "
            f"or is the script still just the template prompts?"
        )

    estimated_minutes = word_count / 150  # ~150 wpm typical narration

    voice_cfg = channel.voice
    output_dir = REPO_ROOT / "output" / "voiceover" / channel.slug
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{script_path.stem}.mp3"

    print(f"Script:        {script_path.relative_to(REPO_ROOT)}")
    print(f"Narration:     {word_count} words (~{estimated_minutes:.1f} min)")
    print(
        f"Voice:         {voice_cfg.primary} (rate={voice_cfg.rate}, volume={voice_cfg.volume})"
    )
    print(f"Synthesizing... (edge-tts streams from Microsoft, roughly real-time)")

    try:
        synthesize(
            text=text,
            voice=voice_cfg.primary,
            rate=voice_cfg.rate,
            volume=voice_cfg.volume,
            output_path=output_path,
        )
    except Exception as e:  # noqa: BLE001 — surface any edge-tts failure cleanly
        raise click.ClickException(f"edge-tts synthesis failed: {e}")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\nAudio written: {output_path.relative_to(REPO_ROOT)} ({size_mb:.2f} MB)")

    record_path = write_record(
        channel_slug=channel.slug,
        kind="voice",
        payload={
            "script_path": str(script_path.relative_to(REPO_ROOT)),
            "audio_path": str(output_path.relative_to(REPO_ROOT)),
            "voice": voice_cfg.primary,
            "rate": voice_cfg.rate,
            "volume": voice_cfg.volume,
            "word_count": word_count,
            "estimated_minutes": round(estimated_minutes, 2),
            "audio_size_bytes": output_path.stat().st_size,
        },
    )
    print(f"Record:        {record_path.relative_to(REPO_ROOT)}")
