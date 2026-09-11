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
    prev_image_key: str | None = None
    image_blend: float = 1.0  # 0.0 = fully prev_image_key, 1.0 = fully image_key


@dataclass
class ImageSegment:
    """One span of time during which the SAME background image is shown --
    the unit build_image_timeline() produces and build_scene() looks up by
    time. Every boundary is a real event from the song itself (a line's own
    start, or a real chord onset) -- never an independent fixed timer, so
    the display can't drift out of sync with the actual music."""
    start: float
    end: float
    image_key: str


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


def _instrumental_micro_segments(
    chord_track: ChordTrack | None, gap_start: float, gap_end: float,
) -> list[tuple[float, float, str]]:
    """(start, end, image_key) triples covering [gap_start, gap_end) at raw
    per-chord granularity -- one per chord event overlapping the gap, plus a
    generic '[Instrumental]' segment for any sub-range the chord track
    doesn't cover at all. These are the RAW boundaries _merge_into_hold_blocks
    smooths into hold-respecting blocks; every one of them is a real chord
    onset, never a guess."""
    if gap_end <= gap_start:
        return []
    if chord_track is None or not chord_track.events:
        return [(gap_start, gap_end, line_hash("[Instrumental]"))]

    segments: list[tuple[float, float, str]] = []
    cursor = gap_start
    for event in chord_track.events:
        seg_start, seg_end = max(event.start, gap_start), min(event.end, gap_end)
        if seg_end <= seg_start:
            continue
        if seg_start > cursor:
            segments.append((cursor, seg_start, line_hash("[Instrumental]")))
        segments.append((seg_start, seg_end, line_hash(f"[Instrumental — chord: {event.label}]")))
        cursor = seg_end
    if cursor < gap_end:
        segments.append((cursor, gap_end, line_hash("[Instrumental]")))
    return segments


def _merge_into_hold_blocks(
    micro_segments: list[tuple[float, float, str]], min_hold_seconds: float,
) -> list[ImageSegment]:
    """Walks micro_segments in order, merging consecutive short ones forward
    until the accumulated run spans at least min_hold_seconds -- real owner
    complaint: fast chord changes made the background image flip too often.
    A segment that already meets the minimum ON ITS OWN flushes any shorter
    run pending before it (even if that run is still under the minimum --
    there's nothing left to merge it with once a self-sufficient segment
    follows) and then stands as its own block; a trailing short run with
    nothing left to merge with at all is still emitted as its own final
    block rather than dropped. Every block's own start/end are real
    boundaries lifted straight from micro_segments, so a block can never
    drift from the actual music -- it can only show a real chord's image a
    little LONGER than that one chord's own raw span, never independently
    of it."""
    blocks: list[ImageSegment] = []
    pending_start: float | None = None
    pending_key: str | None = None
    pending_end: float | None = None
    for start, end, key in micro_segments:
        if end - start >= min_hold_seconds:
            if pending_start is not None:
                blocks.append(ImageSegment(pending_start, pending_end, pending_key))
                pending_start = None
            blocks.append(ImageSegment(start, end, key))
            continue
        if pending_start is None:
            pending_start, pending_key = start, key
        pending_end = end
        if pending_end - pending_start >= min_hold_seconds:
            blocks.append(ImageSegment(pending_start, pending_end, pending_key))
            pending_start = None
    if pending_start is not None:
        blocks.append(ImageSegment(pending_start, pending_end, pending_key))
    return blocks


def build_image_timeline(
    lines: list[LyricLine],
    chord_track: ChordTrack | None,
    audio_duration: float,
    min_hold_seconds: float = 2.0,
) -> list[ImageSegment]:
    """The full ordered background-image schedule for a song, start to end:
    one segment per sung line's own plausible interval(s) (image keyed to
    that line's text, unchanged from the original per-line behavior), and --
    during instrumental stretches -- one segment per real chord onset UNLESS
    that chord's own span is shorter than min_hold_seconds, in which case it
    merges forward with the chord(s) after it until the combined block
    reaches that minimum (see _merge_into_hold_blocks). build_scene() looks
    this up by time instead of re-deriving the active chord itself, and also
    uses it to find the PREVIOUS segment for the crossfade transition."""
    vocal_intervals = sorted(
        (start, end, line_hash(line.text))
        for line in lines
        for start, end in _plausible_sung_intervals(line)
    )

    segments: list[tuple[float, float, str]] = []
    cursor = 0.0
    for start, end, key in vocal_intervals:
        gap_start = cursor
        if start > gap_start:
            segments.extend(
                (s.start, s.end, s.image_key)
                for s in _merge_into_hold_blocks(
                    _instrumental_micro_segments(chord_track, gap_start, start), min_hold_seconds,
                )
            )
        if end > cursor:
            segments.append((max(start, cursor), end, key))
            cursor = end
    if cursor < audio_duration:
        segments.extend(
            (s.start, s.end, s.image_key)
            for s in _merge_into_hold_blocks(
                _instrumental_micro_segments(chord_track, cursor, audio_duration), min_hold_seconds,
            )
        )

    return [ImageSegment(start, end, key) for start, end, key in segments]


def _segment_index_at(timeline: list[ImageSegment], t: float) -> int | None:
    for i, seg in enumerate(timeline):
        if seg.start <= t < seg.end:
            return i
    return None


def build_scene(
    lines: list[LyricLine],
    t: float,
    chord_track: ChordTrack | None = None,
    window: int = 1,
    audio_duration: float | None = None,
    min_hold_seconds: float = 2.0,
    image_transition_seconds: float = 0.25,
    image_timeline: list[ImageSegment] | None = None,
) -> Scene:
    if not lines:
        raise ValueError("no lines to build a scene from")

    idx = find_current_line_index(lines, t)
    current = lines[idx]

    # Computed up front (not just where Ken Burns pacing already used it below)
    # so the CURRENT line's own text can be blanked during a real instrumental
    # gap too. Real bug, 2026-09-10 ("Wish You Were Here"): a line's forced-
    # alignment end_time stretched to the next real line's start_time 85
    # seconds later (radio-dialogue intro text with nothing to align against
    # until real singing resumed) -- find_current_line_index keys only on
    # start_time, so that stale line just sat on screen the entire gap. This
    # reuses the same plausibility check the Ken Burns pacing below already
    # trusted, rather than adding a second, possibly-inconsistent notion of
    # "are we still in this line."
    in_a_line = _in_a_line(lines, t)

    scene_lines: list[SceneLine] = []
    # Only current (0) and upcoming (+1..+window) lines are shown -- no previous line.
    for offset in range(0, window + 1):
        i = idx + offset
        if not (0 <= i < len(lines)):
            continue
        line = lines[i]
        is_current = offset == 0
        words = []
        if not (is_current and not in_a_line):
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

    # The image timeline (one segment per sung line, one per hold-respecting
    # instrumental block -- see build_image_timeline) drives both the
    # instrumental Ken Burns span and the image_key/crossfade below, so a
    # merged hold block's pan covers its WHOLE visible span instead of
    # resetting at each chord absorbed into it.
    timeline = image_timeline
    if timeline is None:
        timeline_duration = audio_duration if audio_duration is not None else t + 1.0
        timeline = build_image_timeline(lines, chord_track, timeline_duration, min_hold_seconds)
    seg_idx = _segment_index_at(timeline, t)

    if not in_a_line and seg_idx is not None:
        # Instrumental with a real timeline segment: pace the pan to that
        # segment's own span instead of the baseline current-to-next-line
        # span, which can be far longer than what's actually being shown
        # right now (real bug, 2026-09-09: a mistimed line's raw end_time
        # stretched that span to ~2 minutes, making the pan read as frozen
        # even after _in_a_line correctly started following the chord).
        segment = timeline[seg_idx]
        kb_start, kb_end = segment.start, max(segment.end, segment.start + 1.0)

    ken_burns_progress = min(max((t - kb_start) / (kb_end - kb_start), 0.0), 1.0)

    scroll_start = current.start_time or 0.0
    plausible_end = _plausible_line_end(current)
    scroll_end = plausible_end if (plausible_end and plausible_end > scroll_start) else scroll_start + 1.0
    scroll_progress = min(max((t - scroll_start) / (scroll_end - scroll_start), 0.0), 1.0)

    if seg_idx is not None:
        image_key = timeline[seg_idx].image_key
    elif in_a_line:
        image_key = line_hash(current.text)
    else:
        image_key = _instrumental_image_key(chord_track or ChordTrack(), t)

    # Crossfade: blend from the PREVIOUS timeline segment's image into this
    # one over image_transition_seconds, starting the instant this segment
    # begins -- capped so the fade can never eat more than 40% of either
    # neighboring segment's own length (a briefly-held image must not spend
    # its whole visible life mid-fade).
    prev_image_key: str | None = None
    image_blend = 1.0
    if seg_idx is not None and seg_idx > 0:
        segment = timeline[seg_idx]
        prev_segment = timeline[seg_idx - 1]
        span = min(
            image_transition_seconds,
            (segment.end - segment.start) * 0.4,
            (prev_segment.end - prev_segment.start) * 0.4,
        )
        if span > 0:
            prev_image_key = prev_segment.image_key
            image_blend = min(1.0, max(0.0, (t - segment.start) / span))

    return Scene(
        lines=scene_lines,
        image_key=image_key,
        ken_burns_progress=ken_burns_progress,
        scroll_progress=scroll_progress,
        prev_image_key=prev_image_key,
        image_blend=image_blend,
    )
