"""Tracks which songs the owner has removed from each of the GUI's per-song
lists (Redo / Upload to YouTube / Pending Uploads / Flagged for Lyrics Review) -- purely a display
filter, never touches a song's real files on disk. Scoped per list: a song
removed from Pending Uploads can still be found via the separate Upload to
YouTube list (owner request, 2026-09-15 -- "don't destroy the file, just
remove it from the list")."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("playalongvideoproduction")

# Separate from Settings (lyricvideo/settings.py) on purpose, same reasoning
# as batch.py's _STATE_FILE: this is remembered GUI display state, not
# owner-tunable render config.
_STATE_FILE = Path.home() / ".playalongvideoproduction" / "dismissed_songs.json"


def load_dismissed(list_name: str, path: Path = _STATE_FILE) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get(list_name, []))
    except (OSError, ValueError):
        return set()


def dismiss_song(list_name: str, slug: str, path: Path = _STATE_FILE) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    dismissed = set(data.get(list_name, []))
    dismissed.add(slug)
    data[list_name] = sorted(dismissed)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues
        log.warning("Could not save dismissed song state: %s", exc)


def undismiss_song(list_name: str, slug: str, path: Path = _STATE_FILE) -> None:
    """Brings a hidden song back to the list (a Redo of a song removed from Flagged for Lyrics Review does this)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    dismissed = set(data.get(list_name, []))
    if slug not in dismissed:
        return
    dismissed.discard(slug)
    data[list_name] = sorted(dismissed)
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues
        log.warning("Could not save dismissed song state: %s", exc)
