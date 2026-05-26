"""YouTube upload — OAuth2 (Desktop flow) + videos.insert as PRIVATE draft.

This module **never publishes a video publicly**. It uploads as PRIVATE so the
operator clicks the final Publish button in YouTube Studio. Per the project's
non-negotiable guardrails.

First run opens a browser for one-time consent. Subsequent runs use the cached
refresh token in `secrets/youtube_token.json` (gitignored).

OAuth scope is the minimum needed: `youtube.upload` only — no read, no delete,
no analytics. Revoke at any time via https://myaccount.google.com/permissions.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import click
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import REPO_ROOT, ChannelConfig, EnvConfig
from .records import write_record

log = logging.getLogger(__name__)

# Minimum scope — write-only upload. No read, no delete, no analytics.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _get_credentials(client_json_path: Path, token_path: Path) -> Credentials:
    """Load cached creds or trigger the OAuth browser flow if needed."""
    creds: Credentials | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as e:  # noqa: BLE001
            log.warning("Cached token unreadable (%s) — re-authenticating.", e)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            print("Refreshed OAuth token (no browser needed).")
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as e:  # noqa: BLE001
            log.warning("Token refresh failed: %s — full re-auth.", e)

    # Fresh OAuth flow — opens browser
    if not client_json_path.exists():
        raise click.ClickException(
            f"OAuth client JSON not found at {client_json_path}. "
            f"Place it there, or update YOUTUBE_OAUTH_CLIENT_JSON in .env."
        )

    print()
    print("=" * 70)
    print("ONE-TIME OAUTH FLOW")
    print("A browser window will open to Google's consent screen.")
    print()
    print("CRITICAL — sign in with the YouTube channel's Google account.")
    print("This must NOT be a FlexWorx-administered account.")
    print()
    print("If you see 'Google hasn't verified this app' — that's normal for")
    print("personal OAuth apps. Click 'Advanced' -> 'Go to <app name>' to continue.")
    print()
    print("If you get 'Access blocked: <app> has not completed the Google verification")
    print("process' — go to Google Cloud Console -> APIs & Services -> OAuth consent")
    print("screen -> Test users, and add your Gmail address.")
    print("=" * 70)
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(client_json_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Token saved: {token_path}")
    print("Future uploads will not need the browser.")
    return creds


def _extract_metadata_from_script(script_path: Path | None) -> dict:
    """Parse title/description/tags from a script .md's '## YouTube metadata' section."""
    if not script_path or not script_path.exists():
        return {}
    text = script_path.read_text(encoding="utf-8")

    # Match the section header through end-of-line so a parenthetical after
    # "metadata" (e.g. "## YouTube metadata (we'll polish in the dashboard...)")
    # doesn't break the match.
    meta_match = re.search(
        r"##\s+YouTube metadata[^\n]*\n(.*?)(?=\n##|\Z)",
        text,
        flags=re.DOTALL,
    )
    if not meta_match:
        return {}
    section = meta_match.group(1)

    def _value_after(label: str) -> str:
        m = re.search(
            rf"\*\*{label}[^*]*\*\*\s*\n+([^\n*][^\n]*)",
            section,
            flags=re.IGNORECASE,
        )
        return m.group(1).strip() if m else ""

    title = _value_after("Title")
    description = _value_after("Description")
    tags_raw = _value_after("Tags")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    return {"title": title, "description": description, "tags": tags}


def _find_approved_videos(channel_slug: str) -> list[Path]:
    vd = REPO_ROOT / "output" / "video" / channel_slug
    if not vd.exists():
        return []
    return sorted(vd.glob("*.mp4.approved"))


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


def _find_provenance_for_stem(channel_slug: str, stem: str) -> dict:
    records_dir = REPO_ROOT / "records" / channel_slug
    if not records_dir.exists():
        return {}
    for rec in records_dir.glob("*_script.json"):
        try:
            data = json.loads(rec.read_text(encoding="utf-8"))
            if stem in data.get("script_path", ""):
                return data
        except Exception:
            continue
    return {}


def _upload_one(video_path: Path, channel: ChannelConfig, youtube) -> dict:
    """Upload a single .approved video. Returns the videos.insert response."""
    stem = video_path.name.removesuffix(".mp4.approved")

    script_path = _find_script_for_stem(channel.slug, stem)
    md_metadata = _extract_metadata_from_script(script_path)
    provenance = _find_provenance_for_stem(channel.slug, stem)
    ai_used = any(
        marker in provenance.get("provenance", "")
        for marker in ("ai-drafted", "ai-edited")
    )

    up = channel.upload
    title = (
        md_metadata.get("title")
        or stem.replace("-", " ").replace("_", " ").strip().title()
    )
    description = md_metadata.get("description") or (
        f"Practical wisdom on: {title}\n\n"
        f"From the {channel.name} channel — {channel.niche}."
    )
    tags = md_metadata.get("tags") or up.default_tags

    if ai_used:
        description += (
            "\n\n[AI-assisted: this script was AI-drafted and human-edited. "
            "The 'altered/AI content' flag has been declared in YouTube Studio.]"
        )

    body = {
        "snippet": {
            "title": title[:100],            # YouTube hard limit
            "description": description[:5000],
            "tags": tags[:30],                # YouTube tag count limit
            "categoryId": str(up.category_id),
            "defaultLanguage": up.default_language,
        },
        "status": {
            "privacyStatus": up.privacy_status,
            "madeForKids": False,
            "selfDeclaredMadeForKids": False,
        },
    }

    print(f"\nUploading: {video_path.name}")
    print(f"  Title:   {title[:80]}")
    print(f"  Privacy: {up.privacy_status}  Category: {up.category_id}")
    print(f"  Tags:    {', '.join(tags[:6])}{'...' if len(tags) > 6 else ''}")
    print(f"  AI-disclosure flag needed in Studio: {ai_used}")
    print(f"  File size: {video_path.stat().st_size / 1024 / 1024:.1f} MB")

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )
    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  ...{int(status.progress() * 100)}%")

    print(f"  DONE. Video ID: {response['id']}")
    print(f"  Studio: https://studio.youtube.com/video/{response['id']}/edit")
    return response


def run_upload(
    channel: ChannelConfig,
    env: EnvConfig,
    specific_video: Path | None = None,
    auth_only: bool = False,
) -> None:
    """Top-level: authenticate, then upload one or all .approved videos."""
    client_json_path = (REPO_ROOT / env.youtube_oauth_client_json).resolve()
    token_path = (REPO_ROOT / env.youtube_token_cache).resolve()

    print(f"OAuth client JSON: {client_json_path.relative_to(REPO_ROOT)}")
    print(f"Token cache:       {token_path.relative_to(REPO_ROOT)}")

    creds = _get_credentials(client_json_path, token_path)
    print(f"Authenticated. Token valid: {creds.valid}")

    if auth_only:
        print("--auth-only set; nothing uploaded.")
        return

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    if specific_video:
        videos = [specific_video.resolve()]
    else:
        videos = _find_approved_videos(channel.slug)

    if not videos:
        raise click.ClickException(
            f"No .approved videos in output/video/{channel.slug}/. "
            f"Approve a draft via the dashboard first."
        )

    print(f"\nFound {len(videos)} video(s) to upload.")

    for v in videos:
        try:
            response = _upload_one(v, channel, youtube)

            # Rename .approved -> .uploaded so the same file isn't re-uploaded
            uploaded_path = v.with_name(
                v.name.removesuffix(".approved") + ".uploaded"
            )
            v.rename(uploaded_path)

            # Pull provenance again for the record (just the AI flag fact)
            stem = v.name.removesuffix(".mp4.approved")
            provenance = _find_provenance_for_stem(channel.slug, stem)
            ai_used = any(
                marker in provenance.get("provenance", "")
                for marker in ("ai-drafted", "ai-edited")
            )

            record_path = write_record(
                channel_slug=channel.slug,
                kind="upload",
                payload={
                    "video_file": str(uploaded_path.relative_to(REPO_ROOT)),
                    "youtube_video_id": response["id"],
                    "youtube_studio_url": (
                        f"https://studio.youtube.com/video/{response['id']}/edit"
                    ),
                    "privacy_status_at_upload": channel.upload.privacy_status,
                    "title": response.get("snippet", {}).get("title"),
                    "category_id": str(channel.upload.category_id),
                    "ai_disclosure_needed": ai_used,
                    "human_approved": True,
                    "human_published": False,   # operator clicks Publish in Studio
                    "human_disclosure_box_checked": False,  # operator confirms in Studio
                },
            )
            print(f"  Record: {record_path.relative_to(REPO_ROOT)}")
        except Exception as e:  # noqa: BLE001
            log.error("Upload failed for %s: %s", v.name, e)
            print(f"  FAILED: {e}")
            print(f"  File kept as .approved for retry: {v.name}")

    print()
    print("=" * 70)
    print("UPLOAD PASS DONE")
    print()
    print("Videos uploaded as PRIVATE. You click Publish in YouTube Studio.")
    print("For any video where ai_disclosure_needed=True, also check the")
    print("'altered or synthetic content' disclosure box on the Publish page.")
    print("=" * 70)
