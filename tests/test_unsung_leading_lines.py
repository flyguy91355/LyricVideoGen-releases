"""A line a lyrics provider stamps in the first seconds, long before anyone sings and with nothing heard near it, is
metadata (composer, contributor, producer...), not a lyric -- whatever its wording. Owner, 2026-09-20: "if its not part of
the audio, its a credit, right?" Song text is invented."""

from lyricvideo.anchors import HeardWord
from lyricvideo.lyric_audio_match import drop_unsung_leading_lines

LINES = ["By. Someone", "Composer 2", "I come home in the morning light", "My mother says when you gonna live your life"]
TIMES = [0.5, 1.0, 17.0, 20.0]
HEARD = [HeardWord("i", 17.2, 17.4), HeardWord("come", 17.5, 17.8)]
LOUD_FROM_17S = [0.0] * 34 + [1.0] * 100                    # 0.5 s hops: silent until 17 s, then singing


def run(lines=LINES, times=TIMES, heard=HEARD, loudness=LOUD_FROM_17S):
    return drop_unsung_leading_lines(lines, times, heard, loudness, hop=0.5)


def test_leading_lines_stamped_at_the_start_of_a_long_intro_are_dropped():
    lines, times, dropped = run()

    assert lines == LINES[2:] and times == TIMES[2:] and dropped == LINES[:2]


def test_nothing_is_dropped_when_singing_starts_at_once():
    early = [HeardWord("by", 0.6, 0.8)]

    assert run(heard=early, loudness=[1.0] * 100)[0] == LINES


def test_a_real_first_line_after_a_short_lead_in_is_kept():
    lines, times, dropped = run(times=[2.0, 2.5, 3.0, 5.0], heard=[HeardWord("i", 3.0, 3.2)], loudness=[0.0] * 6 + [1.0] * 100)

    assert dropped == [] and lines == LINES


def test_only_the_leading_run_is_considered():
    lines, _, dropped = run(lines=["I come home", "Composer 2", "I go out"], times=[17.0, 1.0, 20.0], heard=[HeardWord("i", 17.1, 17.3)])

    assert lines == ["I come home", "Composer 2", "I go out"] and dropped == []


def test_a_line_stamped_late_in_the_intro_is_not_a_credit():
    """A real first line whose source runs a few seconds early is not metadata: credits are stamped near 0."""
    lines, _, dropped = run(times=[9.0, 10.0, 17.0, 20.0])

    assert dropped == []


def test_without_source_times_or_audio_information_nothing_is_dropped():
    assert run(times=None)[0] == LINES
    assert run(heard=[], loudness=[])[0] == LINES


def test_at_most_four_leading_lines_are_dropped_and_a_real_line_always_remains():
    many = [f"Credit {n}" for n in range(6)] + ["I come home in the morning light"]
    lines, _, dropped = run(lines=many, times=[0.1 * n for n in range(6)] + [17.0])

    assert len(dropped) == 4 and lines[-1] == "I come home in the morning light"
