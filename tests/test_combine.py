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
