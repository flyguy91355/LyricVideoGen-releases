from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np

NOVELTY_SILENCE_THRESHOLD = 1e-6


def _even_spans(start_time: float, end_time: float, num_chords: int) -> list[tuple[float, float]]:
    step = (end_time - start_time) / num_chords
    return [(start_time + i * step, start_time + (i + 1) * step) for i in range(num_chords)]


def _select_boundaries_with_min_spacing(
    novelty: np.ndarray,
    frame_times: np.ndarray,
    needed_boundaries: int,
    min_spacing_seconds: float,
    start_time: float,
    end_time: float,
) -> list[float] | None:
    """Greedily accepts frame times highest-novelty-first, rejecting any
    candidate within min_spacing_seconds of an already-accepted one (a
    minimum-distance non-maximum-suppression) OR of start_time/end_time.
    Without the first guard, a single abrupt transition (a real chord change
    OR a drum hit/pick attack) smears across several consecutive CQT frames
    -- window overlap, not several real events -- and naively taking the
    globally top-K novelty frames lets that one transition's neighboring
    frames fill most or all of the boundary slots, starving every OTHER real
    chord change of a boundary (confirmed against real generated songs: a
    111s/20-chord block collapsed to one 101s span plus a cluster of
    sub-0.1s spans around one transition). Without the second guard,
    analyzing only the hard-cropped [start_time, end_time) slice makes the
    very first/last frame's novelty (silence-padding-edge vs. real content)
    look like a genuine change even though it's a window artifact at the
    slice's own boundary, not a real INTERNAL one -- also confirmed against
    real generated songs at this scale (one dropped internal boundary,
    replaced by a spurious one right at an edge). Returns None if fewer than
    needed_boundaries candidates survive both constraints, so the caller can
    fall back to even spacing rather than return a partial, still-bad
    result."""
    order = np.argsort(novelty)[::-1]
    chosen: list[float] = []
    for idx in order:
        t = float(frame_times[idx])
        if t - start_time < min_spacing_seconds or end_time - t < min_spacing_seconds:
            continue
        if all(abs(t - c) >= min_spacing_seconds for c in chosen):
            chosen.append(t)
            if len(chosen) == needed_boundaries:
                return sorted(chosen)
    return None


def detect_chord_change_times(
    instrumental_wav_path: Path,
    start_time: float,
    end_time: float,
    num_chords: int,
    sr: int = 22050,
) -> list[tuple[float, float]]:
    """Split [start_time, end_time) into num_chords contiguous spans, anchored to
    real harmonic-change points detected in the instrumental audio where possible
    (via frame-to-frame chroma distance), falling back to even spacing when the
    audio has too few frames or no meaningful harmonic content to anchor to
    (e.g. silence) -- an honest default rather than a confident-looking guess.
    """
    if num_chords <= 0:
        return []

    duration = end_time - start_time
    if num_chords == 1 or duration <= 0:
        return [(start_time, end_time)] * max(num_chords, 1)

    needed_boundaries = num_chords - 1

    y, _ = librosa.load(str(instrumental_wav_path), sr=sr, offset=start_time, duration=duration)
    if len(y) == 0:
        return _even_spans(start_time, end_time, num_chords)

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    novelty = np.linalg.norm(np.diff(chroma, axis=1), axis=0)

    if len(novelty) < needed_boundaries or np.max(novelty) < NOVELTY_SILENCE_THRESHOLD:
        return _even_spans(start_time, end_time, num_chords)

    frame_times = librosa.frames_to_time(np.arange(len(novelty)), sr=sr) + start_time
    # No two boundaries closer than half the expected average chord duration
    # -- enough to reject a single transition's smeared neighboring frames
    # without being so wide it can't fit genuinely short real chords.
    min_spacing_seconds = (duration / num_chords) / 2
    boundary_times = _select_boundaries_with_min_spacing(
        novelty, frame_times, needed_boundaries, min_spacing_seconds, start_time, end_time
    )
    if boundary_times is None:
        return _even_spans(start_time, end_time, num_chords)

    edges = [start_time, *boundary_times, end_time]
    return list(zip(edges[:-1], edges[1:]))
