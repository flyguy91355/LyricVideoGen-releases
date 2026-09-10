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


def _plausible_sung_intervals(
    line: LyricLine, max_word_duration: float = 3.0, max_gap: float = 5.0,
) -> list[tuple[float, float]]:
    """The real singing window(s) within a line, discounting alignment
    artifacts -- NOT just the line's own raw (start_time, end_time) envelope.
    Real bug found live, 2026-09-09: forced alignment gave a single word a
    105-second duration while the rest of that same line's words were
    tightly and plausibly clustered together 8 seconds later, so the whole
    116-second span (trusting the line's raw end_time) read as 'still
    singing' and suppressed the instrumental per-chord image-follow and Ken
    Burns pacing for nearly two minutes. Any single word's duration is capped
    at max_word_duration (generous even for a long held note), and a gap
    between consecutive (capped) words wider than max_gap starts a new
    interval. A line with no per-word timing at all falls back to its own
    (start_time, end_time) as a single interval unchanged -- there's no
    word-level data to sanity-check against, so there's nothing to cap.
    Returns [] only when there's neither word timing nor a line-level window."""
    words = [w for w in line.words if w.start_time is not None and w.end_time is not None]
    if not words:
        if line.start_time is not None and line.end_time is not None:
            return [(line.start_time, line.end_time)]
        return []
    intervals: list[list[float]] = [
        [words[0].start_time, min(words[0].end_time, words[0].start_time + max_word_duration)]
    ]
    for w in words[1:]:
        capped_end = min(w.end_time, w.start_time + max_word_duration)
        if w.start_time - intervals[-1][1] > max_gap:
            intervals.append([w.start_time, capped_end])
        else:
            intervals[-1][1] = capped_end
    return [(start, end) for start, end in intervals]


def _plausible_line_end(line: LyricLine, max_word_duration: float = 3.0) -> float | None:
    """The line's real, plausible end -- the end of its last plausible sung
    interval (see _plausible_sung_intervals above), not its raw end_time,
    which a single outlier word can inflate arbitrarily. Real bug found
    live, 2026-09-10: a repeated one-word line ("Memoria") got a 6.86s
    duration in forced alignment for what's normally close to a 1-second
    utterance -- unlike the earlier 2026-09-09 "Breathe," bug, this single
    line's own start/end weren't wildly displaced, so `_in_a_line` and Ken
    Burns pacing weren't affected, but the line's own on-screen SCROLL
    animation (paced across its own start->end) was distorted, since that
    used the line's raw end_time directly. Returns None if the line has no
    plausible interval at all (matches _plausible_sung_intervals' own
    empty-list case)."""
    intervals = _plausible_sung_intervals(line, max_word_duration=max_word_duration)
    return intervals[-1][1] if intervals else None


def _in_a_line(lines: list[LyricLine], t: float) -> bool:
    """True if t falls within some line's own real, plausible singing
    window(s) -- False during an intro, an instrumental gap between two lines,
    or after the last line has finished. Used to decide whether the background
    image should follow the lyric text or the active chord (2026-09-09 owner
    request: the image used to freeze on the last-sung line for the whole
    instrumental gap). Uses _plausible_sung_intervals rather than a line's raw
    start_time/end_time envelope, since a single misaligned word can otherwise
    make the rest of that line falsely read as 'still singing' for a long time
    (real bug, 2026-09-09)."""
    return any(
        start <= t < end
        for line in lines
        for start, end in _plausible_sung_intervals(line)
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
    # This baseline is what still governs a normal instrumental gap with no
    # chord data to further distinguish it.
    kb_start = current.start_time or 0.0
    if idx + 1 < len(lines) and lines[idx + 1].start_time is not None:
        kb_end = lines[idx + 1].start_time
    elif audio_duration is not None:
        kb_end = audio_duration
    else:
        kb_end = None
    if kb_end is None or kb_end <= kb_start:
        kb_end = current.end_time if (current.end_time and current.end_time > kb_start) else kb_start + 1.0

    in_a_line = _in_a_line(lines, t)

    if not in_a_line and chord_track is not None:
        # Instrumental with real chord data: pace the pan to the ACTIVE
        # chord's own duration instead of the baseline current-to-next-line
        # span, which can be far longer than what's actually being shown
        # right now (real bug, 2026-09-09: a mistimed line's raw end_time
        # stretched that span to ~2 minutes, making the pan read as frozen
        # even after _in_a_line correctly started following the chord).
        chord = current_chord_at(chord_track, t)
        if chord is not None:
            kb_start, kb_end = chord.start, max(chord.end, chord.start + 1.0)

    ken_burns_progress = min(max((t - kb_start) / (kb_end - kb_start), 0.0), 1.0)

    scroll_start = current.start_time or 0.0
    plausible_end = _plausible_line_end(current)
    scroll_end = plausible_end if (plausible_end and plausible_end > scroll_start) else scroll_start + 1.0
    scroll_progress = min(max((t - scroll_start) / (scroll_end - scroll_start), 0.0), 1.0)

    if in_a_line:
        image_key = line_hash(current.text)
    else:
        image_key = _instrumental_image_key(chord_track or ChordTrack(), t)

    return Scene(
        lines=scene_lines,
        image_key=image_key,
        ken_burns_progress=ken_burns_progress,
        scroll_progress=scroll_progress,
    )
