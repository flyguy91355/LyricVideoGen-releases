"""A line's first word is moved to where the voice starts (owner, 2026-10-31: "lyric timing not precise enough" ...
"looked perfect to me"): only earlier, only out of a clear dip, never onto the previous word's own syllable."""

import numpy as np

from lyricvideo.line_onsets import HOP, snap_line_starts
from lyricvideo.models import LyricLine, Word

SILENT, SUNG, DIP = -60.0, -15.0, -40.0


def _db(seconds, *spans):
    """Loudness readings: silence, with each (start, end, level) span written over it."""
    db = np.full(round(seconds / HOP), SILENT)
    for start, end, level in spans:
        db[round(start / HOP):round(end / HOP)] = level
    return db


def _line(*words):
    line = LyricLine(words=[Word(w, s, e) for w, s, e in words])
    line.start_time, line.end_time = line.words[0].start_time, line.words[-1].end_time
    return line


def test_a_first_word_lit_late_after_a_dip_moves_to_where_the_voice_rises():
    db = _db(6, (1.0, 2.4, SUNG), (2.4, 2.6, DIP), (2.6, 5.0, SUNG))
    lines = [_line(("by", 1.0, 1.6), ("above", 1.7, 2.9)), _line(("I", 3.0, 3.3), ("am", 3.4, 3.8))]

    out, moved = snap_line_starts(lines, db)

    assert moved == 1 and abs(out[1].words[0].start_time - 2.6) < 0.015 and out[1].start_time == out[1].words[0].start_time
    assert out[0].words[-1].end_time == out[1].words[0].start_time == out[0].end_time      # the previous word ends there
    assert lines[1].words[0].start_time == 3.0                                               # the input is untouched


def test_a_rise_at_the_aligned_start_keeps_it_even_with_an_earlier_syllable_rise():
    # "com|pare" dips and rises at 2.0; "To" rises at 2.6, right where it is placed.
    db = _db(6, (1.0, 1.9, SUNG), (1.9, 2.0, DIP), (2.0, 2.5, SUNG), (2.5, 2.6, DIP), (2.6, 4.0, SUNG))
    lines = [_line(("compare", 1.0, 2.55)), _line(("To", 2.62, 2.9), ("the", 3.0, 3.2))]

    assert snap_line_starts(lines, db)[1] == 0


def test_a_rise_after_the_aligned_start_or_no_dip_moves_nothing():
    soft_consonant = _db(6, (1.0, 2.4, SUNG), (2.4, 2.7, DIP), (2.7, 4.0, SUNG))       # "Still": loud only at its vowel
    legato = _db(6, (1.0, 5.0, SUNG))
    lines = [_line(("so", 1.0, 2.3)), _line(("Still", 2.6, 3.0), ("here", 3.1, 3.5))]

    assert snap_line_starts(lines, soft_consonant)[1] == 0
    assert snap_line_starts(lines, legato)[1] == 0


def test_a_tiny_move_or_one_into_the_previous_words_start_is_left_alone():
    near = _db(6, (1.0, 2.9, SUNG), (2.9, 2.95, DIP), (2.95, 4.0, SUNG))                # 0.05 s early: noise
    lines = [_line(("so", 1.0, 2.8)), _line(("I", 3.0, 3.3))]
    assert snap_line_starts(lines, near)[1] == 0

    into_previous = _db(6, (1.0, 2.5, DIP), (2.5, 4.0, SUNG))                          # the rise is the previous word's own
    lines = [_line(("oh", 2.45, 2.9)), _line(("I", 3.0, 3.3))]
    assert snap_line_starts(lines, into_previous)[1] == 0


def test_the_align_stage_saves_the_moved_starts_and_keeps_the_timing_when_the_vocals_cannot_be_read(tmp_path, monkeypatch):
    from dataclasses import replace
    from pathlib import Path

    from lyricvideo.models import load_song
    from lyricvideo.pipeline import run_pipeline
    from tests.test_pipeline import _patch_common

    _patch_common(monkeypatch, tmp_path)

    def earlier(lines, vocals_path):
        first = replace(lines[0], words=[replace(lines[0].words[0], start_time=lines[0].words[0].start_time - 0.3)]
                        + lines[0].words[1:])
        return [first] + lines[1:], 1

    monkeypatch.setattr("lyricvideo.pipeline.snap_to_vocals", lambda lines, path: (_ for _ in ()).throw(OSError("no wav")))
    run_pipeline(Path("audio.mp3"), tmp_path / "kept")
    kept = load_song(tmp_path / "kept" / "lyrics_timed.json").lines[0].words[0].start_time

    monkeypatch.setattr("lyricvideo.pipeline.snap_to_vocals", earlier)
    run_pipeline(Path("audio.mp3"), tmp_path / "moved")
    moved = load_song(tmp_path / "moved" / "lyrics_timed.json").lines[0].words[0].start_time

    assert abs(moved - (kept - 0.3)) < 1e-6
