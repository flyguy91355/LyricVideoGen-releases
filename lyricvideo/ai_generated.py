"""An original song the channel made with AI (owner, 2026-10-30: "make sure the ai generated check is right on youtube").

YouTube requires disclosing AI-generated music ("AI generated music" / "Creates music that's the main focus of the video",
support.google.com/youtube/answer/14328491). A folder holding AI_GENERATED.txt is uploaded with
status.containsSyntheticMedia = True (Studio's "AI use: Yes"), and its description never invents a history for the band.
An EASY CHORD version (`<song>/easychords`) follows its song. Delete the file to undo.

A song made by Create a Song (song_maker.py) also has ai_song.json -- what was asked for (band, style, key...). Such a song
waits for the owner to watch it (owner, 2026-10-31: "Wait for me"): it is Flagged with WATCH_FIRST_REASON and never
uploaded until Mark Verified or Upload Anyway.

AI_MADE_BY_OTHERS.txt marks an AI song someone ELSE made (owner, 2026-11-25: "Storm in My Heart" is a Rockin' Jukebox AI
song titled as Elvis): it is uploaded with the AI label too and every reply, engagement comment and description is told
it is AI, but none of our-own-song behaviour applies (AI upload lane, AI review list, certain lyrics, chord gap fill,
"original song made for this channel"). `ai_song_kind` is what viewer-facing text is written from."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

AI_FILE = "AI_GENERATED.txt"
AI_SONG_FILE = "ai_song.json"
AI_OTHER_FILE = "AI_MADE_BY_OTHERS.txt"
AI_OWN = "own"        # ai_song_kind: the channel's own AI song (AI_FILE)
AI_OTHER = "other"    # ai_song_kind: an AI song someone else made (AI_OTHER_FILE)
WATCH_FIRST_REASON = "New AI song: watch it first, then Mark Verified or Upload Anyway."


def is_ai_generated(folder: Path) -> bool:
    folder = Path(folder)
    if (folder / AI_FILE).exists():
        return True
    return folder.name == "easychords" and (folder.parent / AI_FILE).exists()


def ai_song_kind(folder: Path) -> str:
    """AI_OWN, AI_OTHER, or "" for a song not known to be AI. An EASY CHORD version follows its song."""
    if is_ai_generated(folder):
        return AI_OWN
    folder = Path(folder)
    if (folder / AI_OTHER_FILE).exists() or (folder.name == "easychords" and (folder.parent / AI_OTHER_FILE).exists()):
        return AI_OTHER
    return ""


def _song_folder(folder: Path) -> Path:
    folder = Path(folder)
    return folder.parent if folder.name == "easychords" else folder


def created_song(folder: Path) -> bool:
    """True for a song Create a Song made (an EASY CHORD version follows its song)."""
    return (_song_folder(folder) / AI_SONG_FILE).exists()


def requested_key(folder: Path) -> str:
    """The key the song was asked to be in ("" when none, or unreadable)."""
    try:
        key = json.loads((_song_folder(folder) / AI_SONG_FILE).read_text(encoding="utf-8")).get("key")
    except (OSError, ValueError, AttributeError):
        return ""
    return key.strip() if isinstance(key, str) else ""


def mark_ai_generated(folder: Path, how: str = "AI-generated song") -> Path:
    """Writes the tag file (kept readable by hand) and returns its path."""
    path = Path(folder) / AI_FILE
    path.write_text(
        f"{how}\nTagged: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n"
        "YouTube uploads of this song are marked as altered or synthetic content (AI use: Yes).\n",
        encoding="utf-8",
    )
    return path


def mark_ai_made_by_others(folder: Path, how: str = "AI-generated song made by someone else") -> Path:
    """Writes AI_OTHER_FILE (kept readable by hand) and returns its path."""
    path = Path(folder) / AI_OTHER_FILE
    path.write_text(
        f"{how}\nTagged: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n"
        "This song is AI-generated, but not by this channel. Uploads are marked as altered or synthetic content (AI use: "
        "Yes), and replies, comments and descriptions never say or suggest it is not AI or a real artist's recording.\n",
        encoding="utf-8",
    )
    return path
