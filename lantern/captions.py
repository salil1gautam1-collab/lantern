"""Caption generation + burn-in via faster-whisper + ffmpeg.

`faster-whisper` transcribes the voiceover .mp3 to timestamped segments
(local, no API, free). We write an .srt file then use ffmpeg's `subtitles`
filter to burn the captions directly onto the video.

First time you transcribe, faster-whisper downloads its model (~150MB for
`base.en`) from HuggingFace to `~/.cache/huggingface/`. Subsequent runs use
the cached model — no network needed.

We use the system ffmpeg (the one installed via winget Gyan.FFmpeg) for the
burn-in step, not moviepy's bundled imageio-ffmpeg, because the `subtitles`
filter needs libass support which the system ffmpeg has out of the box.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _format_srt_timestamp(seconds: float) -> str:
    """Convert seconds (float) to SRT format `HH:MM:SS,mmm`."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def transcribe_to_srt(
    audio_path: Path,
    srt_path: Path,
    model_size: str = "base.en",
) -> int:
    """Transcribe `audio_path` with faster-whisper, write SRT to `srt_path`.

    Returns the number of subtitle segments written.
    """
    from faster_whisper import WhisperModel  # local import — heavy

    log.info(
        "Loading faster-whisper model: %s (downloads ~150MB on first use)",
        model_size,
    )
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    log.info("Transcribing: %s", audio_path.name)
    segments, _info = model.transcribe(str(audio_path), beam_size=5)

    count = 0
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start = _format_srt_timestamp(seg.start)
            end = _format_srt_timestamp(seg.end)
            text = seg.text.strip()
            if not text:
                continue
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
            count = i
    return count


def burn_subtitles(
    input_video: Path,
    srt_path: Path,
    output_video: Path,
    font_name: str = "Arial",
    font_size: int = 36,
    outline: int = 2,
) -> None:
    """Burn `srt_path` into `input_video` via ffmpeg, writing to `output_video`.

    `input_video` and `output_video` MAY be the same path — we use a temp file
    in the same directory and rename.

    We run ffmpeg with the input video's directory as CWD and reference files
    by their basenames. This dodges the Windows path-escaping mess that the
    `subtitles` filter chokes on (colons + backslashes).
    """
    work_dir = input_video.parent
    local_srt = work_dir / "_tmp_captions.srt"
    local_output = work_dir / "_tmp_captioned.mp4"

    shutil.copy(str(srt_path), str(local_srt))
    try:
        style = (
            f"FontName={font_name},"
            f"FontSize={font_size},"
            f"PrimaryColour=&Hffffff&,"
            f"OutlineColour=&H000000&,"
            f"BorderStyle=1,"
            f"Outline={outline},"
            f"Alignment=2,"
            f"MarginV=40"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", input_video.name,
            "-vf", f"subtitles=_tmp_captions.srt:force_style='{style}'",
            "-c:a", "copy",
            "-preset", "medium",
            "_tmp_captioned.mp4",
        ]
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg subtitle burn failed (returncode={result.returncode}):\n"
                f"{result.stderr[-1500:]}"
            )

        if output_video.exists():
            output_video.unlink()
        local_output.rename(output_video)
    finally:
        for tmp in (local_srt, local_output):
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
