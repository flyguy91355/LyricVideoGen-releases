from __future__ import annotations

import bisect
import hashlib
import json
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
    )


def save_song(song: Song, path: Path) -> None:
    path.write_text(json.dumps(_song_to_dict(song), indent=2), encoding="utf-8")


def load_song(path: Path) -> Song:
    return _song_from_dict(json.loads(path.read_text(encoding="utf-8")))
