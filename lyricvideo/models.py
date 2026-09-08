from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ChordWord:
    word: str
    chord: str | None = None
    start_time: float | None = None
    end_time: float | None = None


@dataclass
class LyricLine:
    words: list[ChordWord] = field(default_factory=list)
    start_time: float | None = None
    end_time: float | None = None

    @property
    def text(self) -> str:
        return " ".join(w.word for w in self.words)


@dataclass
class InstrumentalChord:
    chord: str
    start_time: float
    end_time: float


@dataclass
class InstrumentalBlock:
    """An unordered-in-time chord progression found during parsing, before real
    timing exists -- positioned relative to the lyric lines it sits next to."""

    chords: list[str] = field(default_factory=list)
    before_line_index: int = 0  # index into the parsed lyric_lines list; == len(lyric_lines) means "after the last line" (an outro)


@dataclass
class Song:
    title: str
    audio_path: str
    vocal_stem_path: str | None = None
    instrumental_stem_path: str | None = None
    lines: list[LyricLine] = field(default_factory=list)
    instrumental_chords: list[InstrumentalChord] = field(default_factory=list)
    image_cache: dict[str, str] = field(default_factory=dict)


def line_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _song_to_dict(song: Song) -> dict:
    return asdict(song)


def _song_from_dict(data: dict) -> Song:
    lines = [
        LyricLine(
            words=[ChordWord(**w) for w in ln["words"]],
            start_time=ln.get("start_time"),
            end_time=ln.get("end_time"),
        )
        for ln in data.get("lines", [])
    ]
    instrumental_chords = [
        InstrumentalChord(**c) for c in data.get("instrumental_chords", [])
    ]
    return Song(
        title=data["title"],
        audio_path=data["audio_path"],
        vocal_stem_path=data.get("vocal_stem_path"),
        instrumental_stem_path=data.get("instrumental_stem_path"),
        lines=lines,
        instrumental_chords=instrumental_chords,
        image_cache=data.get("image_cache", {}),
    )


def save_song(song: Song, path: Path) -> None:
    path.write_text(json.dumps(_song_to_dict(song), indent=2), encoding="utf-8")


def load_song(path: Path) -> Song:
    return _song_from_dict(json.loads(path.read_text(encoding="utf-8")))
