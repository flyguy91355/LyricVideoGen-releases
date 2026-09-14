import pytest

from lyricvideo.models import Word, LyricLine
from lyricvideo.combine import combine_alignment, AlignmentSanityError


def _parsed_lines():
    return [
        LyricLine(words=[Word(word="hello"), Word(word="there")]),
        LyricLine(words=[Word(word="my"), Word(word="friend")]),
    ]


def test_combine_alignment_assigns_times_in_order():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (1.0, 1.3), (1.3, 1.8)]

    result = combine_alignment(lines, word_times, audio_duration=2.0)

    assert result[0].words[0].start_time == 0.0
    assert result[0].words[0].end_time == 0.4
    assert result[0].start_time == 0.0
    assert result[0].end_time == 0.9
    assert result[1].words[0].start_time == 1.0
    assert result[1].end_time == 1.8


def test_combine_alignment_word_count_mismatch_raises():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9)]  # only 2, but 4 words total

    with pytest.raises(AlignmentSanityError):
        combine_alignment(lines, word_times, audio_duration=2.0)


def test_combine_alignment_out_of_duration_raises():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (1.0, 1.3), (1.3, 5.0)]  # 5.0 > duration

    with pytest.raises(AlignmentSanityError):
        combine_alignment(lines, word_times, audio_duration=2.0)


def test_combine_alignment_non_monotonic_raises():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (0.5, 0.7), (1.3, 1.8)]  # word 3 goes backward

    with pytest.raises(AlignmentSanityError):
        combine_alignment(lines, word_times, audio_duration=2.0)


def test_combine_alignment_tolerates_a_last_word_ending_a_hair_past_the_audio_duration():
    """align_words() resamples the stem to the model's rate with a ceil'd
    length, so a word sung right up to the file's final sample can end a few
    microseconds past the original-rate duration -- a rounding artifact,
    never a reason to abort the whole align stage."""
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (1.0, 1.3), (1.3, 2.0 + 1 / 16000)]

    result = combine_alignment(lines, word_times, audio_duration=2.0)

    assert result[-1].end_time == 2.0 + 1 / 16000


def test_combine_alignment_accepts_issue_6_final_word_overshoot():
    """GitHub issue #6 (2026-09-14), the reporter's exact numbers: the aligner
    ran the last word out to the stem's final resampled sample, 11
    microseconds past the 44.1 kHz duration, and the strict check that
    v2.0.3 still shipped (`end > audio_duration`, no tolerance) aborted the
    whole align stage. Pinned so the tolerance can't quietly regress."""
    lines = [LyricLine(words=[Word(word="rain"), Word(word="no")])]
    word_times = [(200.0, 207.1556522513123), (211.81600437785048, 232.4375625)]

    result = combine_alignment(lines, word_times, audio_duration=232.43755102040816)

    assert result[0].words[-1].end_time == 232.4375625
