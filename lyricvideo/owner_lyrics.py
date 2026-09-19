"""The owner's own edited lyrics for a song set aside for review.

Saved as `work/<song>/lyrics_owner.txt` (one lyric per line) from the GUI's "Edit Lyrics" window. The next Redo uses
them INSTEAD of any online source (pipeline fetch stage) -- the owner's word is final, so no AI or audio check
overrides it -- while the aligner still times them and the sync check still validates that timing."""

from __future__ import annotations

import json
from pathlib import Path

from .models import load_song

OWNER_LYRICS_FILE = "lyrics_owner.txt"


def owner_lyrics_lines(work_dir: Path) -> list[str] | None:
    """The saved lines, or None when there is no (usable) owner edit."""
    try:
        text = (Path(work_dir) / OWNER_LYRICS_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines or None


def load_editable_lyrics(work_dir: Path) -> str:
    """What the editor should start with: the owner's previous edit if any, else the lyrics the video currently
    shows, else the fetched lyric lines, else nothing."""
    work_dir = Path(work_dir)
    saved = owner_lyrics_lines(work_dir)
    if saved is not None:
        return "\n".join(saved)
    try:
        song = load_song(work_dir / "lyrics_timed.json")
        lines = [" ".join(w.word for w in line.words) for line in song.lines if line.words]
        if lines:
            return "\n".join(lines)
    except Exception:
        pass
    try:
        data = json.loads((work_dir / "lyric_lines.json").read_text(encoding="utf-8"))
        return "\n".join(data["lines"] if isinstance(data, dict) else data)
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def save_owner_lyrics(work_dir: Path, text: str) -> list[str]:
    """Writes the owner's lyrics (blank lines and stray spaces dropped) and returns the lines. Saving nothing is
    refused, so an accidental select-all-delete cannot wipe a song's lyrics."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("There are no lyrics to save.")
    (Path(work_dir) / OWNER_LYRICS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines
