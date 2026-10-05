"""A song YouTube has banned (owner, 2026-10-04: "All Along the Watchtower by Bob Dylan has been banned ... dropped from any further
action, will just stay on my computer, maybe with a tag that it is banned").

A folder holding BANNED_ON_YOUTUBE.txt is left out of every list and every scan: Pending/Upload/Flagged/EASY lists, Redo, the comment
and playlist walks, the thumbnail backfill, schedule_upload. Its files are never touched or deleted -- the text file is the tag (open it
for the reason and date); delete it to bring the song back. An EASY CHORD version (`<song>/easychords`) follows its song."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

BANNED_FILE = "BANNED_ON_YOUTUBE.txt"


class SongBanned(RuntimeError):
    """A banned song is never uploaded."""


def is_banned(folder: Path) -> bool:
    folder = Path(folder)
    if (folder / BANNED_FILE).exists():
        return True
    return folder.name == "easychords" and (folder.parent / BANNED_FILE).exists()


def mark_banned(folder: Path, reason: str = "YouTube banned this video") -> Path:
    """Writes the tag file (kept readable by hand) and returns its path."""
    path = Path(folder) / BANNED_FILE
    path.write_text(
        f"{reason}\nTagged: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n"
        "This song stays on this computer only. The app skips it everywhere (lists, uploads, comments, thumbnails, redo).\n"
        "Delete this file to bring it back.\n",
        encoding="utf-8",
    )
    return path
