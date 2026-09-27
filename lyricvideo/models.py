from __future__ import annotations

import bisect
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Word:
    word: str
    start_time: float | None = None
    end_time: float | None = None


@dataclass
class LyricLine:
    words: list[Word] = field(default_factory=list)
    start_time: float | None = None
    end_time: float | None = None

    @property
    def text(self) -> str:
        return " ".join(w.word for w in self.words)


@dataclass
class ChordEvent:
    """A chord held from `start` to `end` (seconds). Label like 'Am', 'F#', 'N'."""

    start: float
    end: float
    label: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ChordTrack:
    """Timeline of chords plus global musical info for one song, produced by
    detect_chords() and otherwise independent of the lyric lines entirely."""

    events: list[ChordEvent] = field(default_factory=list)
    key: str = ""
    bpm: float = 0.0


def current_chord_at(track: ChordTrack, t: float) -> ChordEvent | None:
    """The chord event covering time t, or None (before the first event, after the
    last, or an empty track). O(log n) via bisect against events sorted by start
    time (detect_chords always emits them in order)."""
    starts = [e.start for e in track.events]
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        return None
    event = track.events[i]
    return event if t < event.end else None


def next_chord_after(track: ChordTrack, t: float) -> ChordEvent | None:
    """The first chord event starting after t whose label differs from whatever is
    current at t, so a merged-boundary duplicate segment is never reported as "next"."""
    current = current_chord_at(track, t)
    for event in track.events:
        if event.start > t and (current is None or event.label != current.label):
            return event
    return None


@dataclass
class Song:
    title: str
    audio_path: str
    vocal_stem_path: str | None = None
    instrumental_stem_path: str | None = None
    lines: list[LyricLine] = field(default_factory=list)
    chord_track: ChordTrack = field(default_factory=ChordTrack)
    image_cache: dict[str, str] = field(default_factory=dict)
    lyrics_source: str = ""             # which fetch_lyric_lines_verified() source won ("", if unchecked)
    lyrics_accuracy_concern: str = ""   # "" means check_lyric_accuracy() passed; non-empty = flagged


def display_slug(work_dir: Path) -> str:
    """The slug to show/log for work_dir -- ordinarily just its own directory name, but an EASY CHORD
    (capo) variant's directory is always literally named "easychords" (nested inside its original song's
    own folder, owner 2026-09-23: "i dont need twice the folder"), which would collide across every song
    if used bare anywhere a slug needs to stay unique (e.g. cleared_log.py's history, upload_label()).
    Falls back to "<parent>/easychords" in that one case; every other folder is unaffected."""
    work_dir = Path(work_dir)
    if work_dir.name == "easychords":
        return f"{work_dir.parent.name}/easychords"
    return work_dir.name


def original_song_dir(work_dir: Path) -> Path:
    """The folder of the song work_dir was made from: an EASY CHORD (capo) variant's `<song>/easychords` folder is the same
    song, audio and lyric timing with only the chords respelled, so whatever belongs to the SONG rather than to the video
    (its Whisper transcript, its key) lives in the parent folder. Any other folder is its own original."""
    work_dir = Path(work_dir)
    return work_dir.parent if work_dir.name == "easychords" else work_dir


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Writes `text` to `path` so no reader -- another thread building a song list, the 20-minute tick -- and no crash or
    earlyoom kill mid-write ever sees a half-written file: a uniquely named temp file in the same folder is written and
    flushed to disk, then os.replace()d over the target (atomic on Linux and Windows). Line endings are translated exactly
    like Path.write_text. Windows refuses the replace while another handle has the target open, so that is retried
    briefly; on any failure the temp file is removed and the original is left untouched."""
    path = Path(path)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temporary, "x", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def line_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _song_to_dict(song: Song) -> dict:
    return asdict(song)


def _song_from_dict(data: dict) -> Song:
    # Picks only Word's own fields rather than Word(**w): songs saved by the
    # pre-merge pipeline carry a "chord" key on every word (from the old ChordWord
    # model) that Word no longer has -- Word(**w) would raise TypeError on every
    # such file (confirmed live 2026-09-09, broke Redo on every pre-existing song).
    lines = [
        LyricLine(
            words=[
                Word(word=w["word"], start_time=w.get("start_time"), end_time=w.get("end_time"))
                for w in ln["words"]
            ],
            start_time=ln.get("start_time"),
            end_time=ln.get("end_time"),
        )
        for ln in data.get("lines", [])
    ]
    chord_data = data.get("chord_track") or {}
    chord_track = ChordTrack(
        events=[ChordEvent(**e) for e in chord_data.get("events", [])],
        key=chord_data.get("key", ""),
        bpm=chord_data.get("bpm", 0.0),
    )
    return Song(
        title=data["title"],
        audio_path=data["audio_path"],
        vocal_stem_path=data.get("vocal_stem_path"),
        instrumental_stem_path=data.get("instrumental_stem_path"),
        lines=lines,
        chord_track=chord_track,
        image_cache=data.get("image_cache", {}),
        lyrics_source=data.get("lyrics_source", ""),
        lyrics_accuracy_concern=data.get("lyrics_accuracy_concern", ""),
    )


def save_song(song: Song, path: Path) -> None:
    """Atomic (atomic_write_text): a kill mid-save, or a song list reading the file at that moment, never sees a truncated
    lyrics_timed.json. Before the file is replaced, an owner verification recorded in the older whole-file format is carried
    over to the lyric-timing fingerprint (owner_verified.py), so a rewrite that leaves the words and their timing alone --
    a key correction, a concern update -- does not silently undo the owner's approval."""
    path = Path(path)
    if path.name == "lyrics_timed.json" and (path.parent / "owner_verified.json").exists():
        try:
            from .owner_verified import carry_over_legacy_record   # local: owner_verified imports this module
            carry_over_legacy_record(path.parent)
        except Exception:
            pass                                    # never let the verification bookkeeping block a save
    atomic_write_text(path, json.dumps(_song_to_dict(song), indent=2))


def load_song(path: Path) -> Song:
    return _song_from_dict(json.loads(path.read_text(encoding="utf-8")))
