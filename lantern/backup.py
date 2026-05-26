"""Bundle project essentials into a timestamped local .zip.

Includes the small/portable stuff (`.env`, OAuth secrets, channel configs,
provenance records, the source package, and the written scripts) so the
project can be moved to another PC. Bulky media (`output/voiceover/`,
`output/video/`, `output/instagram/`, `cache/`, `assets/music/`) and the
virtualenv stay behind — they're regeneratable, would balloon the archive,
and aren't needed to set up Lantern on a new machine.

The archive contains your `.env` and OAuth refresh token. Treat it as
sensitive. Move via USB or encrypted storage; do NOT sync to FlexWorx-linked
cloud drives.
"""

from __future__ import annotations

import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .config import REPO_ROOT

log = logging.getLogger(__name__)

# Paths (relative to REPO_ROOT) to include. Files or directories both OK.
INCLUDE: tuple[str, ...] = (
    ".env",
    ".env.example",
    ".gitignore",
    "requirements.txt",
    "README.md",
    "channels",
    "secrets",        # OAuth client JSON + youtube_token.json (sensitive — see warning)
    "records",        # per-video provenance JSONs (small)
    "lantern",        # the Python package
    "output/scripts", # the .md scripts you wrote (small text files; worth keeping)
)

# Skip these path components anywhere in the tree
SKIP_DIR_NAMES: frozenset[str] = frozenset({"__pycache__", ".git", "node_modules"})
SKIP_SUFFIXES: tuple[str, ...] = (".pyc", ".part", ".pyo")


def _should_skip(path: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in path.parts):
        return True
    if path.suffix in SKIP_SUFFIXES:
        return True
    return False


def _iter_files(src: Path, rel_target: Path):
    """Yield (real_path, arcname_str) pairs for everything under `src` that survives the skip filter.

    If `src` is a file, just yield it once at `rel_target`.
    If `src` is a directory, walk recursively.
    """
    if src.is_file():
        yield src, str(rel_target).replace("\\", "/")
        return
    for p in src.rglob("*"):
        if p.is_dir() or _should_skip(p):
            continue
        arc = str(rel_target / p.relative_to(src)).replace("\\", "/")
        yield p, arc


def run_backup(output_dir: Path | None = None) -> Path:
    """Build the backup .zip. Returns the archive path."""
    output_dir = output_dir or (REPO_ROOT / "backups")
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    archive = output_dir / f"lantern-backup-{ts}.zip"

    print(f"Building backup: {archive.relative_to(REPO_ROOT) if archive.is_relative_to(REPO_ROOT) else archive}\n")

    total_files = 0
    total_src_bytes = 0

    with zipfile.ZipFile(
        str(archive), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        for entry in INCLUDE:
            src = REPO_ROOT / entry
            if not src.exists():
                print(f"  skip (missing): {entry}")
                continue
            count = 0
            entry_bytes = 0
            for fp, arc in _iter_files(src, Path(entry)):
                zf.write(str(fp), arcname=arc)
                count += 1
                entry_bytes += fp.stat().st_size
            total_files += count
            total_src_bytes += entry_bytes
            print(f"  + {entry:22}  {count:5} files   {entry_bytes / 1024:9.1f} KB")

    size_bytes = archive.stat().st_size
    print()
    print("Backup complete:")
    print(f"  Archive:        {archive}")
    print(f"  Files included: {total_files}")
    print(f"  Source size:    {total_src_bytes / 1024 / 1024:.2f} MB")
    print(
        f"  Zip size:       {size_bytes / 1024 / 1024:.2f} MB"
        f"  ({(1 - size_bytes / max(total_src_bytes, 1)) * 100:.0f}% compression)"
    )
    print()
    print("WARNING — this archive contains:")
    print("  - your .env (Pexels/Pixabay/YouTube API keys)")
    print("  - secrets/client_secret.json (OAuth client)")
    print("  - secrets/youtube_token.json (refresh token, after first OAuth)")
    print("Treat the .zip as sensitive. Move via USB or encrypted storage.")
    print("Do NOT sync to OneDrive / Drive / Dropbox that is FlexWorx-tenant linked.")
    print()
    print("backups/ is gitignored — the archive will not accidentally end up in git.")

    return archive
