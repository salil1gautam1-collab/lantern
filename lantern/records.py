"""Per-run provenance records — JSON files under `records/<channel>/`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import REPO_ROOT


def write_record(channel_slug: str, kind: str, payload: dict[str, Any]) -> Path:
    """Write a JSON record under `records/<channel>/<UTC-timestamp>_<kind>.json`.

    Returns the path written.
    """
    records_dir = REPO_ROOT / "records" / channel_slug
    records_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d_%H%M%S")
    path = records_dir / f"{ts}_{kind}.json"

    body = {
        "timestamp_utc": now.isoformat(),
        "channel": channel_slug,
        "kind": kind,
        **payload,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, ensure_ascii=False, default=str)
    return path
