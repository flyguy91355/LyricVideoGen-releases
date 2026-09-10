from __future__ import annotations

from dataclasses import dataclass, field

from .models import ChordTrack, LyricLine, current_chord_at, line_hash


@dataclass
class SceneWord:
    text: str
    word_active: bool = False  # karaoke-style: true once this word has actually been sung


@dataclass
class SceneLine:
    words: list[SceneWord] = field(default_factory=list)
    is_current: bool = False
    distance_from_current: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Scene:
    lines: list[SceneLine]
    image_key: str
    ken_burns_progress: float
    scroll_progress: float = 0.0


def find_current_line_index(lines: list[LyricLine], t: float) -> int:
    idx = 0
    for i, line in enumerate(lines):
        if line.start_time is not None and line.start_time <= t:
            idx = i
        else:
            break
    return idx


def _in_a_line(lines: list[LyricLine], t: float) -> bool:
    """True if t falls within some line's own [start_time, end_time) singing
    window -- False during an intro, an instrumental gap between two lines, or
    after the last line has finished. Used to decide whether the background image
    should follow the lyric text or the active chord (2026-09-09 owner request:
    the image used to freeze on the last-sung line for the whole instrumental gap)."""
    return any(
        l.start_time is not None and l.end_time is not None and l.start_time <= t < l.end_time
        for l in lines
    )


def _instrumental_image_key(chord_track: ChordTrack, t: float) -> str:
    chord = current_chord_at(chord_track, t)
    caption = f"[Instrumental — chord: {chord.label}]" if chord else "[Instrumental]"
    return line_hash(caption)


def build_scene(
    lines: list[LyricLine],
    t: float,
    chord_track: ChordTrack | None = None,
    window: int = 1,
    audio_duration: float | None = None,
) -> Scene:
    if not lines:
        raise ValueError("no lines to build a scene from")

    idx = find_current_line_index(lines, t)
    current = lines[idx]

    scene_lines: list[SceneLine] = []
    # Only current (0) and upcoming (+1..+window) lines are shown -- no previous line.
    for offset in range(0, window + 1):
        i = idx + offset
        if not (0 <= i < len(lines)):
            continue
        line = lines[i]
        is_current = offset == 0
        words = []
        for w in line.words:
            word_sung = bool(is_current and w.start_time is not None and w.start_time <= t)
            words.append(SceneWord(text=w.word, word_active=word_sung))
        scene_lines.append(SceneLine(words=words, is_current=is_current, distance_from_current=offset))

    # Ken Burns progress spans the image's REAL on-screen duration -- until the
    # next line begins, or the song ends for the last line -- not just the
    # current line's own singing window (unchanged from before this merge).
    kb_start = current.start_time or 0.0
    if idx + 1 < len(lines) and lines[idx + 1].start_time is not None:
        kb_end = lines[idx + 1].start_time
    elif audio_duration is not None:
        kb_end = audio_duration
    else:
        kb_end = None
    if kb_end is None or kb_end <= kb_start:
        kb_end = current.end_time if (current.end_time and current.end_time > kb_start) else kb_start + 1.0
    ken_burns_progress = min(max((t - kb_start) / (kb_end - kb_start), 0.0), 1.0)

    scroll_start = current.start_time or 0.0
    scroll_end = (
        current.end_time if (current.end_time and current.end_time > scroll_start) else scroll_start + 1.0
    )
    scroll_progress = min(max((t - scroll_start) / (scroll_end - scroll_start), 0.0), 1.0)

    if _in_a_line(lines, t):
        image_key = line_hash(current.text)
    else:
        image_key = _instrumental_image_key(chord_track or ChordTrack(), t)

    return Scene(
        lines=scene_lines,
        image_key=image_key,
        ken_burns_progress=ken_burns_progress,
        scroll_progress=scroll_progress,
    )
