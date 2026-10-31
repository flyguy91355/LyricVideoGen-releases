"""An original song the channel made with AI (owner, 2026-10-30: "make sure the ai generated check is right on youtube").

YouTube requires disclosing AI-generated music ("AI generated music" / "Creates music that's the main focus of the video",
support.google.com/youtube/answer/14328491). A folder holding AI_GENERATED.txt is uploaded with
status.containsSyntheticMedia = True (Studio's "AI use: Yes"), and its description never invents a history for the band.
An EASY CHORD version (`<song>/easychords`) follows its song. Delete the file to undo."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

AI_FILE = "AI_GENERATED.txt"


def is_ai_generated(folder: Path) -> bool:
    folder = Path(folder)
    if (folder / AI_FILE).exists():
        return True
    return folder.name == "easychords" and (folder.parent / AI_FILE).exists()


def mark_ai_generated(folder: Path, how: str = "AI-generated song") -> Path:
    """Writes the tag file (kept readable by hand) and returns its path."""
    path = Path(folder) / AI_FILE
    path.write_text(
        f"{how}\nTagged: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n"
        "YouTube uploads of this song are marked as altered or synthetic content (AI use: Yes).\n",
        encoding="utf-8",
    )
    return path
