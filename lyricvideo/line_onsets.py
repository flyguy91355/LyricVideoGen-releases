"""Moves each sung line's first word to where the voice really starts (owner, 2026-10-31: "lyric timing not precise
enough"). Forced alignment lit a line's first word up to ~0.4 s late when the line followed straight on from the one
before (Best Story Ever Told: "I am living..." is sung at 56.83 s on the vocal stem, the video lit it at 57.2 s). The vocal
stem shows the start itself: the voice rising out of a dip to singing level. Only such a rise near the aligned start moves
the word, and only EARLIER: a word starting with a soft consonant ("Still", "Twenty") gets loud only at its vowel, so a rise
after the aligned start says nothing. The LATEST rise up to just past the aligned start is the line's own (an earlier one
can be the previous word's second syllable, "com|pare", or a breath); when that rise is at the aligned start, the line is
already right. Singing that runs on with no dip is left exactly as it was."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from .models import LyricLine

HOP = 0.01                 # seconds between loudness readings
FRAME = 0.03               # each reading's length
LOOK_BACK = 0.6            # an onset may be this much before the aligned start
MIN_SHIFT = 0.08           # smaller moves are within the measuring noise: left alone
AT_START = 0.05            # a rise this close after the aligned start is the line's own start
DIP = 0.15                 # the rise is measured over this much time before it
MIN_RISE_DB = 10.0         # how far the voice must rise out of the dip
SINGING_DB = 10.0          # the onset must reach within this much of the song's singing level
KEEP_PREVIOUS = 0.15       # the previous line's last word keeps at least this long
MIN_WORD = 0.05            # a moved word keeps at least this long


def loudness_db(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """dB of every HOP-spaced FRAME of a mono signal (reading i covers i*HOP .. i*HOP + FRAME)."""
    hop, frame = max(1, int(HOP * sample_rate)), max(1, int(FRAME * sample_rate))
    squares = np.concatenate([[0.0], np.cumsum(samples.astype(np.float64) ** 2)])
    starts = np.arange(0, max(1, len(samples) - frame + 1), hop)
    mean_square = (squares[starts + frame] - squares[starts]) / frame
    return 10 * np.log10(mean_square + 1e-12)


def onset_near(db: np.ndarray, start: float, earliest: float, singing_db: float) -> float | None:
    """The latest clear vocal onset between max(earliest, start - LOOK_BACK) and start + AT_START, or None."""
    dip = max(1, round(DIP / HOP))
    lo = max(round(max(earliest, start - LOOK_BACK) / HOP), dip)
    hi = min(round((start + AT_START) / HOP), len(db) - 1)
    found = None
    for i in range(lo, hi + 1):
        loud_now = db[i] >= singing_db - SINGING_DB and db[i] - db[i - dip:i].min() >= MIN_RISE_DB
        loud_before = db[i - 1] >= singing_db - SINGING_DB and db[i - 1] - db[max(0, i - 1 - dip):i - 1].min() >= MIN_RISE_DB
        if loud_now and not loud_before:
            found = i * HOP
    return found


def snap_line_starts(lines: list[LyricLine], db: np.ndarray) -> tuple[list[LyricLine], int]:
    """(the lines with each first word moved to its onset, how many moved). `db` is loudness_db of the vocal stem."""
    singing = db[db > db.max() - 40]
    singing_db = float(np.percentile(singing, 75)) if singing.size else float(db.max())
    out = [replace(line, words=[replace(w) for w in line.words]) for line in lines]
    moved = 0
    for n, line in enumerate(out):
        if not line.words or line.words[0].start_time is None:
            continue
        first = line.words[0]
        previous = out[n - 1].words[-1] if n and out[n - 1].words else None
        earliest = previous.start_time + KEEP_PREVIOUS if previous is not None and previous.start_time is not None else 0.0
        onset = onset_near(db, first.start_time, earliest, singing_db)
        if onset is None or onset > first.start_time - MIN_SHIFT or first.end_time - onset < MIN_WORD:
            continue
        first.start_time = onset
        line.start_time = onset
        if previous is not None and previous.end_time is not None and previous.end_time > onset:
            previous.end_time = onset
            if out[n - 1].end_time is not None and out[n - 1].end_time > onset:
                out[n - 1].end_time = onset
        moved += 1
    return out, moved


def snap_to_vocals(lines: list[LyricLine], vocals_path: Path) -> tuple[list[LyricLine], int]:
    import soundfile as sf
    audio, sample_rate = sf.read(str(vocals_path), always_2d=True)
    return snap_line_starts(lines, loudness_db(audio.mean(axis=1), sample_rate))
