"""owner_verified: "if i decide its a good video its a good video" (owner, 2026-09-20).

The timing check (timing_gate.py) is only an automatic yardstick -- Whisper's ears -- and a hard song can fail it while looking right
to the owner (Like a Prayer: 83%, with the last lines marked wrong where Whisper heard "I'm a prisoner" for the backing vocals). A song
the owner watched and approved is recorded here, in its own small file next to lyrics_timed.json (never in the song's own concern
field, which other checks own). A verified song is offered in the Upload to YouTube and Pending lists and leaves Flagged for Lyrics
Review, overriding ANY check -- timing or lyric text -- because the owner looked at the whole video.

The record is tied to a fingerprint of the exact lyrics_timed.json that was watched: a Redo writes a new timing file, so the
verification lapses on its own and the new version has to be watched (or pass) again."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .cleared_log import record_cleared

FILENAME = "owner_verified.json"


def _fingerprint(song_dir: Path) -> str | None:
    try:
        return hashlib.sha256((Path(song_dir) / "lyrics_timed.json").read_bytes()).hexdigest()
    except OSError:
        return None


def mark_verified(song_dir: Path, automatic_share: float | None = None, needed: float | None = None) -> None:
    """Records that the owner watched this song's current version and approved it."""
    song_dir = Path(song_dir)
    fingerprint = _fingerprint(song_dir)
    if fingerprint is None:
        raise FileNotFoundError(f"{song_dir / 'lyrics_timed.json'} not found")
    record = {
        "fingerprint": fingerprint, "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "automatic_share": automatic_share, "needed": needed,
    }
    (song_dir / FILENAME).write_text(json.dumps(record, indent=2), encoding="utf-8")
    score = "no automatic score" if automatic_share is None else f"automatic {automatic_share:.0%}"
    record_cleared(song_dir.name, f"verified by the owner ({score})")


def verification(song_dir: Path) -> dict | None:
    """The owner's record when it is for the timing file the song has NOW, else None (never verified, redone since, unreadable)."""
    song_dir = Path(song_dir)
    try:
        record = json.loads((song_dir / FILENAME).read_text(encoding="utf-8"))
        fingerprint = _fingerprint(song_dir)
        return record if fingerprint is not None and record.get("fingerprint") == fingerprint else None
    except (OSError, ValueError, AttributeError):
        return None


def upload_label(song_dir: Path) -> str:
    """The row text in the Upload to YouTube list: the song's name, plus who vouched for it when the owner did."""
    song_dir = Path(song_dir)
    record = verification(song_dir)
    if record is None:
        return song_dir.name
    share = record.get("automatic_share")
    return f"{song_dir.name}  ✔ verified by you ({'no automatic score' if share is None else f'{share:.0%} automatic'})"
