"""Local review dashboard — FastAPI app for the human-in-the-loop step.

You run it with `python -m lantern dashboard` and visit http://127.0.0.1:8000.
It lists draft videos awaiting review, lets you edit the script (the dominant
element per the project's review-pressure rule), preview the rendered .mp4,
and either Approve (queue for upload) or Reject (delete files).

Approve renames `<stem>.mp4` to `<stem>.mp4.approved` as a sentinel. Upload.py
(module 6) will pick up the .approved files and push them to YouTube as
PRIVATE/SCHEDULED drafts. Nothing in this dashboard touches YouTube directly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import REPO_ROOT, ChannelConfig

log = logging.getLogger(__name__)

_PKG = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_PKG / "templates"))

app = FastAPI(title="Lantern Review Dashboard")
app.mount("/static", StaticFiles(directory=str(_PKG / "static")), name="static")

_channel: ChannelConfig | None = None


def _ch() -> ChannelConfig:
    if _channel is None:
        raise RuntimeError("Dashboard channel not initialised. Use run_dashboard().")
    return _channel


def _video_dir() -> Path:
    return REPO_ROOT / "output" / "video" / _ch().slug


def _video_path(stem: str) -> Path:
    return _video_dir() / f"{stem}.mp4"


def _thumb_path(stem: str) -> Path:
    return _video_dir() / f"{stem}_thumb.png"


def _audio_path(stem: str) -> Path:
    return REPO_ROOT / "output" / "voiceover" / _ch().slug / f"{stem}.mp3"


def _script_path(stem: str) -> Path | None:
    """Find the .md script for this stem. Falls back to other channel dirs if not present
    in the active channel's scripts dir (handy for stems carried over from a channel pivot).
    """
    primary = REPO_ROOT / "output" / "scripts" / _ch().slug / f"{stem}.md"
    if primary.exists():
        return primary
    # Fallback: any channel's scripts dir
    scripts_root = REPO_ROOT / "output" / "scripts"
    if scripts_root.exists():
        for sub in scripts_root.iterdir():
            if not sub.is_dir():
                continue
            cand = sub / f"{stem}.md"
            if cand.exists():
                return cand
    return None


def _list_drafts() -> list[dict]:
    vd = _video_dir()
    if not vd.exists():
        return []
    drafts = []
    for mp4 in vd.glob("*.mp4"):
        if mp4.name.endswith(".approved") or mp4.name.endswith(".part"):
            continue
        stem = mp4.stem
        thumb = _thumb_path(stem)
        drafts.append(
            {
                "stem": stem,
                "video_size_mb": round(mp4.stat().st_size / 1024 / 1024, 1),
                "has_thumbnail": thumb.exists(),
                "thumbnail_url": f"/draft/{stem}/thumbnail" if thumb.exists() else None,
                "modified_ts": mp4.stat().st_mtime,
            }
        )
    return sorted(drafts, key=lambda d: d["modified_ts"], reverse=True)


def _get_draft(stem: str) -> dict:
    video = _video_path(stem)
    if not video.exists():
        raise HTTPException(status_code=404, detail=f"No video for stem '{stem}'")
    script = _script_path(stem)
    if script and script.exists():
        script_content = script.read_text(encoding="utf-8")
        script_path_str = str(script.relative_to(REPO_ROOT))
    else:
        script_content = (
            "(No script .md found for this video. "
            "Approving or editing will create a new file under "
            f"output/scripts/{_ch().slug}/{stem}.md)"
        )
        script_path_str = "(not found)"
    return {
        "stem": stem,
        "video_url": f"/draft/{stem}/video",
        "thumbnail_url": f"/draft/{stem}/thumbnail",
        "script_content": script_content,
        "script_path": script_path_str,
        "video_size_mb": round(video.stat().st_size / 1024 / 1024, 1),
    }


@app.get("/")
def index(request: Request):
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "channel": _ch(),
            "drafts": _list_drafts(),
        },
    )


@app.get("/draft/{stem}")
def detail(request: Request, stem: str):
    draft = _get_draft(stem)
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "channel": _ch(),
            "draft": draft,
        },
    )


@app.get("/draft/{stem}/video")
def serve_video(stem: str):
    p = _video_path(stem)
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(p), media_type="video/mp4")


@app.get("/draft/{stem}/thumbnail")
def serve_thumb(stem: str):
    p = _thumb_path(stem)
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(p), media_type="image/png")


@app.post("/draft/{stem}/save-script")
def save_script(stem: str, content: str = Form(...)):
    script = _script_path(stem)
    if script is None:
        # Create new under the active channel's scripts dir
        script_dir = REPO_ROOT / "output" / "scripts" / _ch().slug
        script_dir.mkdir(parents=True, exist_ok=True)
        script = script_dir / f"{stem}.md"
    script.write_text(content, encoding="utf-8")
    log.info("Saved script: %s (%d chars)", script, len(content))
    return RedirectResponse(f"/draft/{stem}?saved=1", status_code=303)


@app.post("/draft/{stem}/approve")
def approve(stem: str):
    """Mark this draft as approved-for-upload by renaming the .mp4 to .mp4.approved.
    Upload.py (module 6) picks up .approved files and uploads them as PRIVATE drafts
    to YouTube. NOTHING in this dashboard talks to YouTube directly.
    """
    video = _video_path(stem)
    if not video.exists():
        raise HTTPException(status_code=404)
    target = video.with_suffix(".mp4.approved")
    video.rename(target)
    log.info("Approved: %s -> %s", video.name, target.name)
    return RedirectResponse("/?approved=" + stem, status_code=303)


@app.post("/draft/{stem}/reject")
def reject(stem: str):
    """Delete all files for this draft: video, thumbnail, voiceover mp3, script md."""
    removed: list[str] = []
    for p in (_video_path(stem), _thumb_path(stem), _audio_path(stem)):
        if p.exists():
            p.unlink()
            removed.append(p.name)
    script = _script_path(stem)
    if script and script.exists():
        script.unlink()
        removed.append(script.name)
    log.info("Rejected stem %s; removed %d files: %s", stem, len(removed), removed)
    return RedirectResponse("/?rejected=" + stem, status_code=303)


def run_dashboard(channel: ChannelConfig, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the dashboard. Blocks until you Ctrl+C."""
    global _channel
    _channel = channel
    print()
    print(f"  Lantern dashboard for channel '{channel.slug}' ({channel.region})")
    print(f"  Open in your browser: http://{host}:{port}")
    print(f"  Press Ctrl+C to stop.")
    print()
    uvicorn.run(app, host=host, port=port, log_level="warning")
