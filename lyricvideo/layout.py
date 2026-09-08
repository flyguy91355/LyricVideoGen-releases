from __future__ import annotations

from dataclasses import dataclass, field

from .models import InstrumentalChord, LyricLine, line_hash

# How long a just-hit chord stays in its bright "flash" state before settling
# to the steady active color.
CHORD_FLASH_DURATION_SECONDS = 0.3


@dataclass
class SceneWord:
    text: str
    chord: str | None = None
    chord_active: bool = False
    chord_flash: float = 0.0  # 1.0 = just hit, decays to 0.0 over CHORD_FLASH_DURATION_SECONDS
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
    instrumental_chord: str | None = None
    instrumental_chord_flash: float = 0.0


def find_current_line_index(lines: list[LyricLine], t: float) -> int:
    idx = 0
    for i, line in enumerate(lines):
        if line.start_time is not None and line.start_time <= t:
            idx = i
        else:
            break
    return idx


def _active_instrumental_chord(
    instrumental_chords: list[InstrumentalChord], t: float
) -> InstrumentalChord | None:
    for chord in instrumental_chords:
        if chord.start_time <= t < chord.end_time:
            return chord
    return None


def _flash_intensity(elapsed: float) -> float:
    """1.0 the instant something is hit, fading linearly to 0.0 over
    CHORD_FLASH_DURATION_SECONDS."""
    return min(max(1.0 - elapsed / CHORD_FLASH_DURATION_SECONDS, 0.0), 1.0)


def build_scene(
    lines: list[LyricLine],
    t: float,
    window: int = 1,
    instrumental_chords: list[InstrumentalChord] | None = None,
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
            active = bool(is_current and w.chord and w.start_time is not None and w.start_time <= t)
            word_sung = bool(is_current and w.start_time is not None and w.start_time <= t)
            words.append(
                SceneWord(
                    text=w.word,
                    # Every visible line (current AND upcoming) shows all of its
                    # own chords as soon as it's on screen; chord_active marks
                    # the one whose moment has arrived, and only the current
                    # line ever highlights -- an upcoming line's chords stay
                    # dim/pending since none of its words have been reached yet.
                    chord=w.chord,
                    chord_active=active,
                    chord_flash=_flash_intensity(t - w.start_time) if active else 0.0,
                    # Karaoke-style left-to-right word sweep on the current
                    # line, using the exact same real per-word timing as the
                    # chord reveal above.
                    word_active=word_sung,
                )
            )
        scene_lines.append(SceneLine(words=words, is_current=is_current, distance_from_current=offset))

    # Ken Burns progress spans the image's REAL on-screen duration -- until the
    # next line begins, or the song ends for the last line -- not just the
    # current line's own singing window. Using only the singing window let the
    # pan/zoom finish early and freeze for the rest of an instrumental gap
    # before the image actually changed.
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

    # Scroll progress stays tied to the line's own real singing window (not
    # stretched into a following instrumental gap) -- it's about how far
    # through being sung the line is, not how long its background image
    # happens to stay on screen.
    scroll_start = current.start_time or 0.0
    scroll_end = (
        current.end_time if (current.end_time and current.end_time > scroll_start) else scroll_start + 1.0
    )
    scroll_progress = min(max((t - scroll_start) / (scroll_end - scroll_start), 0.0), 1.0)

    active_instrumental = _active_instrumental_chord(instrumental_chords or [], t)

    return Scene(
        lines=scene_lines,
        image_key=line_hash(current.text),
        ken_burns_progress=ken_burns_progress,
        scroll_progress=scroll_progress,
        instrumental_chord=active_instrumental.chord if active_instrumental else None,
        instrumental_chord_flash=(
            _flash_intensity(t - active_instrumental.start_time) if active_instrumental else 0.0
        ),
    )
