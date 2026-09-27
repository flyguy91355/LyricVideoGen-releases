"""A permanent record of every song that was REDONE (owner, 2026-09-19: "keep track of the songs that are redone, so we
can replace them if they're up on YouTube, and replace the file if they're not").

`backup_song_outputs()` (called by every redo path: the GUI's Redo, a Batch regenerate, scripts) starts a record;
`run_pipeline()` finishes it. A redone song that is already on YouTube stays on the "still needs replacing there"
list until it is marked replaced; a song that is not on YouTube needs nothing further -- the redo already replaced its
local file in place (the previous version is kept in its redo_backup_<time> folder) and the normal upload flow will
pick up the new one. Stored in ~/.playalongvideoproduction/redone_songs.json."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .models import atomic_write_text
from .youtube_state import load_youtube_state

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
LOG_FILE = CREDENTIALS_DIR / "redone_songs.json"

# Every read-modify-write below holds this, and writes through a uniquely named temp file (atomic_write_text): the app has
# one redo writer at a time today, but a shared ".tmp" name and an unguarded read-append-replace would lose records the
# moment two ever overlapped (the same race cleared_log.py had).
_LOCK = threading.RLock()


def _path(path: Path | None) -> Path:
    return Path(path) if path is not None else LOG_FILE


def redone_songs(path: Path | None = None) -> list[dict]:
    """Every redo record, oldest first ([] for a missing or unreadable file)."""
    try:
        data = json.loads(_path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _records_to_update(path: Path | None) -> list[dict]:
    """The records a write builds on. Unlike redone_songs(), a file that exists but cannot be parsed is renamed aside
    (`<name>.corrupt-<time>`) before a fresh log starts, and a file that cannot be READ right now raises (the callers
    catch it) -- either way the permanent record is never silently replaced by a one-entry list."""
    target = _path(path)
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
    target.replace(target.with_name(f"{target.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"))
    return []


def _save(records: list[dict], path: Path | None) -> None:
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(records, indent=2))


def _now(now: datetime | None) -> str:
    return (now or datetime.now().astimezone()).isoformat(timespec="seconds")


def note_redo_started(work_dir: Path, backup_dir: Path, path: Path | None = None, now: datetime | None = None) -> None:
    state = load_youtube_state(Path(work_dir))
    with _LOCK:
        records = _records_to_update(path)
        records.append({
            "slug": Path(work_dir).name, "status": "started", "started_at": _now(now), "finished_at": None,
            "backup_dir": str(backup_dir), "on_youtube": state is not None,
            "video_id": state.video_id if state is not None else None,
            "replaced_on_youtube": False, "set_aside": False, "concern": "",
        })
        _save(records, path)


def note_redo_finished(work_dir: Path, concern: str = "", path: Path | None = None, now: datetime | None = None) -> None:
    """Completes the latest unfinished redo record for this song; a song that was never redone has none, so a
    brand-new song's first run records nothing."""
    slug = Path(work_dir).name
    with _LOCK:
        records = _records_to_update(path)
        for record in reversed(records):
            if record.get("slug") == slug and record.get("status") == "started":
                record.update(status="done", finished_at=_now(now), concern=concern, set_aside=bool(concern.strip()))
                _save(records, path)
                return


def youtube_replacements_pending(path: Path | None = None) -> list[dict]:
    """Songs whose LATEST redo is finished, that were already on YouTube, and whose new version has not yet been put
    up there in place of the old one."""
    latest: dict[str, dict] = {}
    for record in redone_songs(path):
        latest[record["slug"]] = record
    return [
        r for r in latest.values()
        if r.get("status") == "done" and r.get("on_youtube") and not r.get("replaced_on_youtube")
    ]


def mark_replaced_on_youtube(slug: str, path: Path | None = None) -> None:
    with _LOCK:
        records = _records_to_update(path)
        for record in reversed(records):
            if record.get("slug") == slug:
                record["replaced_on_youtube"] = True
                _save(records, path)
                return
