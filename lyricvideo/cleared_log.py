"""A permanent record of which songs are CLEARED for upload (they passed the lyric and timing checks) and which were pulled
and why (owner, 2026-09-20: "keep track of all the videos that have been cleared for upload ... if there bad remove them ...
they can be redone").

Append-only history in ~/.playalongvideoproduction/cleared_songs.json; the latest entry for a song decides its status.
`run_pipeline()` records every finished run (cleared when it has no concern, removed with the reason otherwise), so a redo that
fixes a song clears it again by itself. The pending-upload list (pipeline.list_pending_uploads) only offers songs with no
concern, so a removed song cannot be selected for upload; it stays in Flagged for Lyrics Review, where Upload Anyway is a
deliberate override."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
LOG_FILE = CREDENTIALS_DIR / "cleared_songs.json"


def _path(path: Path | None) -> Path:
    return Path(path) if path is not None else LOG_FILE


def history(path: Path | None = None) -> list[dict]:
    """Every entry ever recorded, oldest first ([] for a missing or unreadable file)."""
    try:
        data = json.loads(_path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _append(slug: str, status: str, note: str, path: Path | None, now: datetime | None) -> None:
    records = history(path)
    records.append({
        "slug": slug, "status": status, "note": note,
        "at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
    })
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def record_cleared(slug: str, note: str = "", path: Path | None = None, now: datetime | None = None) -> None:
    _append(slug, "cleared", note, path, now)


def record_removed(slug: str, reason: str, path: Path | None = None, now: datetime | None = None) -> None:
    _append(slug, "removed", reason, path, now)


def cleared_songs(path: Path | None = None) -> list[dict]:
    """The songs whose latest entry is 'cleared', sorted by name."""
    latest: dict[str, dict] = {}
    for entry in history(path):
        latest[entry["slug"]] = entry
    return sorted((e for e in latest.values() if e.get("status") == "cleared"), key=lambda e: e["slug"])
