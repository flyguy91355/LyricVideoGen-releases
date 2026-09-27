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
import threading
from datetime import datetime
from pathlib import Path

from .models import atomic_write_text

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
LOG_FILE = CREDENTIALS_DIR / "cleared_songs.json"

# One writer at a time (issue #7 review): the Generate/Batch worker, Mark Verified on the Tk thread and the timing check can
# all append at once; two unguarded read-append-replace passes lost entries, and a shared ".tmp" name made the second
# os.replace fail.
_LOCK = threading.Lock()


def _path(path: Path | None) -> Path:
    return Path(path) if path is not None else LOG_FILE


def history(path: Path | None = None) -> list[dict]:
    """Every entry ever recorded, oldest first ([] for a missing or unreadable file)."""
    try:
        data = json.loads(_path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _existing_records(target: Path) -> list[dict]:
    """The history to append to. A file that exists but cannot be parsed is renamed aside
    (`<name>.corrupt-<time>`) rather than silently replaced by a one-entry list: the record is meant to be permanent."""
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, list):
        return data
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    os.replace(target, target.with_name(f"{target.name}.corrupt-{stamp}"))
    return []


def _append(slug: str, status: str, note: str, path: Path | None, now: datetime | None) -> None:
    target = _path(path)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        records = _existing_records(target)
        records.append({
            "slug": slug, "status": status, "note": note,
            "at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
        })
        atomic_write_text(target, json.dumps(records, indent=2))


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
