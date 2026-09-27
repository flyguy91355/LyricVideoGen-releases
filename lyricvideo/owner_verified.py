"""owner_verified: "if i decide its a good video its a good video" (owner, 2026-09-20).

The timing check (timing_gate.py) is only an automatic yardstick -- Whisper's ears -- and a hard song can fail it while looking right
to the owner (Like a Prayer: 83%, with the last lines marked wrong where Whisper heard "I'm a prisoner" for the backing vocals). A song
the owner watched and approved is recorded here, in its own small file next to lyrics_timed.json (never in the song's own concern
field, which other checks own). A verified song is offered in the Upload to YouTube and Pending lists and leaves Flagged for Lyrics
Review, overriding ANY check -- timing or lyric text -- because the owner looked at the whole video.

The record is tied to a fingerprint of what the owner watched and vouched for: the lyric words and their timing
(`timing_fingerprint`). A Redo re-aligns the words (and Edit Lyrics changes them), so the verification lapses on its own and
the new version has to be watched (or pass) again. A rewrite that leaves the words and timing alone -- a key correction
(Set Key, scripts/settle_keys.py --apply), a concern written or cleared -- keeps it (2026-09-26, issue #7 review: hashing the
whole file let those undo an approval silently). Records written before this carry only a whole-file hash ("fingerprint"),
still honored while the file is byte-for-byte unchanged; models.save_song upgrades such a record just before the file is
first rewritten (carry_over_legacy_record)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .cleared_log import record_cleared
from .models import Song, atomic_write_text, display_slug, load_song

FILENAME = "owner_verified.json"
_TIMED = "lyrics_timed.json"


def _fingerprint(song_dir: Path) -> str | None:
    """The older whole-file hash (kept so records written before timing_fingerprint existed stay valid)."""
    try:
        return hashlib.sha256((Path(song_dir) / _TIMED).read_bytes()).hexdigest()
    except OSError:
        return None


def timing_fingerprint(song: Song) -> str:
    """A hash of exactly what the owner approves when watching: every line's words and each word's start and end time.
    The chords, the key, the images and the concern text are not part of it."""
    payload = [[[w.word, w.start_time, w.end_time] for w in line.words] for line in song.lines]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


# The saved file's timing fingerprint, remembered per version of lyrics_timed.json ((inode, mtime_ns, size); every save_song
# is an atomic replace, so a rewrite gets a new inode): upload_label() runs verification() for every Upload to YouTube row,
# and without this each verified song's ~80 KB file was parsed again on the Tk thread every time that list was built.
_SAVED_FINGERPRINTS: dict[str, tuple[tuple[int, int, int], str]] = {}
_SAVED_FINGERPRINTS_LOCK = threading.Lock()


def _saved_timing_fingerprint(song_dir: Path) -> str:
    timed_path = Path(song_dir) / _TIMED
    st = os.stat(timed_path)
    signature = (st.st_ino, st.st_mtime_ns, st.st_size)
    key = os.path.abspath(timed_path)
    with _SAVED_FINGERPRINTS_LOCK:
        cached = _SAVED_FINGERPRINTS.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    fingerprint = timing_fingerprint(load_song(timed_path))
    with _SAVED_FINGERPRINTS_LOCK:
        if len(_SAVED_FINGERPRINTS) >= 5000:
            _SAVED_FINGERPRINTS.clear()
        _SAVED_FINGERPRINTS[key] = (signature, fingerprint)
    return fingerprint


def _load_record(song_dir: Path) -> dict | None:
    try:
        record = json.loads((Path(song_dir) / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def mark_verified(song_dir: Path, automatic_share: float | None = None, needed: float | None = None) -> None:
    """Records that the owner watched this song's current version and approved it."""
    song_dir = Path(song_dir)
    fingerprint = _fingerprint(song_dir)
    if fingerprint is None:
        raise FileNotFoundError(f"{song_dir / _TIMED} not found")
    record = {
        "fingerprint": fingerprint, "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "automatic_share": automatic_share, "needed": needed,
    }
    try:
        record["timing_fingerprint"] = timing_fingerprint(load_song(song_dir / _TIMED))
    except Exception:
        pass                                        # an unreadable file keeps only the whole-file hash
    atomic_write_text(song_dir / FILENAME, json.dumps(record, indent=2))
    score = "no automatic score" if automatic_share is None else f"automatic {automatic_share:.0%}"
    record_cleared(display_slug(song_dir), f"verified by the owner ({score})")


def verification(song_dir: Path, song: Song | None = None) -> dict | None:
    """The owner's record when it is for the lyric timing the song has NOW, else None (never verified, redone since,
    unreadable). `song` is the already-loaded lyrics_timed.json, when the caller has it (saves parsing it again)."""
    song_dir = Path(song_dir)
    record = _load_record(song_dir)
    if record is None:
        return None
    expected = record.get("timing_fingerprint")
    if expected:
        try:
            current = timing_fingerprint(song) if song is not None else _saved_timing_fingerprint(song_dir)
        except Exception:
            return None
        return record if current == expected else None
    fingerprint = _fingerprint(song_dir)                # a record from before timing_fingerprint existed
    return record if fingerprint is not None and record.get("fingerprint") == fingerprint else None


def carry_over_legacy_record(song_dir: Path) -> None:
    """Called by models.save_song just BEFORE lyrics_timed.json is rewritten: a verification recorded in the older
    whole-file format that still matches the file on disk gets that file's timing fingerprint added, so the rewrite keeps it
    when the words and timing stay the same (and lapses it, as before, when they change). Anything else is left alone."""
    song_dir = Path(song_dir)
    record = _load_record(song_dir)
    if record is None or record.get("timing_fingerprint"):
        return
    fingerprint = _fingerprint(song_dir)
    if fingerprint is None or record.get("fingerprint") != fingerprint:
        return
    record["timing_fingerprint"] = timing_fingerprint(load_song(song_dir / _TIMED))
    atomic_write_text(song_dir / FILENAME, json.dumps(record, indent=2))


def upload_label(song_dir: Path) -> str:
    """The row text in the Upload to YouTube list: the song's name, plus who vouched for it when the owner did."""
    song_dir = Path(song_dir)
    slug = display_slug(song_dir)
    record = verification(song_dir)
    if record is None:
        return slug
    share = record.get("automatic_share")
    return f"{slug}  ✔ verified by you ({'no automatic score' if share is None else f'{share:.0%} automatic'})"
