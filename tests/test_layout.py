import pytest

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word, line_hash
from lyricvideo.layout import (
    _in_a_line, _plausible_line_end, _plausible_sung_intervals, build_scene, find_current_line_index,
)


def _make_lines():
    return [
        LyricLine(
            words=[
                Word(word="hello", start_time=0.0, end_time=0.5),
                Word(word="there", start_time=0.5, end_time=1.0),
            ],
            start_time=0.0, end_time=1.0,
        ),
        LyricLine(
            words=[
                Word(word="my", start_time=1.0, end_time=1.3),
                Word(word="friend", start_time=1.3, end_time=2.0),
            ],
            start_time=1.0, end_time=2.0,
        ),
        LyricLine(
            words=[Word(word="goodbye", start_time=2.0, end_time=2.6)],
            start_time=2.0, end_time=3.0,
        ),
    ]


def test_find_current_line_index():
    lines = _make_lines()
    assert find_current_line_index(lines, 0.2) == 0
    assert find_current_line_index(lines, 1.5) == 1
    assert find_current_line_index(lines, 2.9) == 2


def test_build_scene_shows_only_current_and_next_line_never_previous():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    offsets = sorted(l.distance_from_current for l in scene.lines)
    assert offsets == [0, 1]


def test_build_scene_word_active_sweeps_left_to_right_on_current_line():
    lines = _make_lines()
    scene_at_start = build_scene(lines, t=0.0)
    current_start = next(l for l in scene_at_start.lines if l.is_current)
    assert current_start.words[0].word_active is True
    assert current_start.words[1].word_active is False

    scene_mid = build_scene(lines, t=0.6)
    current_mid = next(l for l in scene_mid.lines if l.is_current)
    assert current_mid.words[0].word_active is True
    assert current_mid.words[1].word_active is True


def test_build_scene_next_line_words_never_marked_active():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    next_line = next(l for l in scene.lines if l.distance_from_current == 1)
    assert all(w.word_active is False for w in next_line.words)


def test_build_scene_ken_burns_progress_increases_within_line():
    lines = _make_lines()
    start_scene = build_scene(lines, t=1.0)
    mid_scene = build_scene(lines, t=1.5)
    end_scene = build_scene(lines, t=1.99)
    assert start_scene.ken_burns_progress < mid_scene.ken_burns_progress < end_scene.ken_burns_progress


def test_build_scene_ken_burns_progress_spans_gap_until_next_line_not_just_singing_end():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
        LyricLine(words=[Word(word="friend", start_time=3.0, end_time=3.5)], start_time=3.0, end_time=3.5),
    ]

    at_singing_end = build_scene(lines, t=1.0)
    mid_gap = build_scene(lines, t=2.0)
    just_before_next = build_scene(lines, t=2.99)

    assert at_singing_end.ken_burns_progress < 1.0
    assert at_singing_end.ken_burns_progress < mid_gap.ken_burns_progress < just_before_next.ken_burns_progress
    assert just_before_next.ken_burns_progress < 1.0
    assert at_singing_end.scroll_progress == 1.0


def test_build_scene_ken_burns_progress_uses_audio_duration_for_last_line():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
    ]

    mid = build_scene(lines, t=3.0, audio_duration=10.0)
    late = build_scene(lines, t=9.0, audio_duration=10.0)

    assert mid.ken_burns_progress < late.ken_burns_progress
    assert late.ken_burns_progress < 1.0


def test_build_scene_scroll_progress_tracks_time_through_current_line():
    lines = _make_lines()
    start_scene = build_scene(lines, t=1.0)
    mid_scene = build_scene(lines, t=1.5)
    end_scene = build_scene(lines, t=1.99)
    assert start_scene.scroll_progress == 0.0
    assert 0.0 < mid_scene.scroll_progress < 1.0
    assert start_scene.scroll_progress < mid_scene.scroll_progress < end_scene.scroll_progress


def test_build_scene_scroll_progress_not_distorted_by_an_outlier_word_duration():
    """Real bug found live, 2026-09-10: a repeated one-word line ("Memoria")
    got a 6.86s duration in forced alignment for what's normally close to a
    1-second utterance. scroll_progress must pace against the line's real,
    plausible end (capped the same way _plausible_sung_intervals already
    caps Ken Burns/in-a-line decisions), not the raw, inflated end_time."""
    lines = [
        LyricLine(
            words=[Word(word="Memoria", start_time=88.30, end_time=95.16)],
            start_time=88.30, end_time=95.16,
        ),
    ]

    # 89.30 is 1 second into the word -- with the raw (uncapped) 6.86s
    # duration this would barely register as progress; capped at 3.0s
    # (the same default as _plausible_sung_intervals), 1 of 3 seconds is
    # a full third of the way through.
    scene = build_scene(lines, t=89.30)

    assert scene.scroll_progress == pytest.approx(1.0 / 3.0, abs=0.01)


def test_plausible_line_end_matches_plausible_sung_intervals_end():
    line = LyricLine(
        words=[Word(word="Memoria", start_time=88.30, end_time=95.16)],
        start_time=88.30, end_time=95.16,
    )

    assert _plausible_line_end(line) == 88.30 + 3.0


def test_plausible_line_end_normal_line_matches_its_own_raw_end():
    line = LyricLine(
        words=[Word(word="hello", start_time=0.0, end_time=0.5), Word(word="there", start_time=0.5, end_time=1.0)],
        start_time=0.0, end_time=1.0,
    )

    assert _plausible_line_end(line) == 1.0


def test_plausible_line_end_returns_none_for_a_totally_empty_line():
    assert _plausible_line_end(LyricLine()) is None


def test_build_scene_image_key_follows_lyric_line_text_while_singing():
    lines = _make_lines()
    scene = build_scene(lines, t=0.2)
    assert scene.image_key == line_hash("hello there")


def test_build_scene_image_key_follows_active_chord_during_instrumental_gap():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
        LyricLine(words=[Word(word="friend", start_time=5.0, end_time=5.5)], start_time=5.0, end_time=5.5),
    ]
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "Am"), ChordEvent(4.0, 6.0, "F"),
    ])

    scene_first_chord = build_scene(lines, t=1.5, chord_track=chord_track)
    scene_second_chord = build_scene(lines, t=2.5, chord_track=chord_track)

    assert scene_first_chord.image_key == line_hash("[Instrumental — chord: C]")
    assert scene_second_chord.image_key == line_hash("[Instrumental — chord: Am]")
    assert scene_first_chord.image_key != scene_second_chord.image_key


def test_build_scene_image_key_before_first_line_uses_instrumental_chord_too():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=5.0, end_time=5.5)], start_time=5.0, end_time=5.5),
    ]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 5.0, "G")])

    scene = build_scene(lines, t=1.0, chord_track=chord_track)

    assert scene.image_key == line_hash("[Instrumental — chord: G]")


def test_build_scene_image_key_gap_with_no_chord_track_falls_back_to_generic_key():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
        LyricLine(words=[Word(word="friend", start_time=5.0, end_time=5.5)], start_time=5.0, end_time=5.5),
    ]

    scene = build_scene(lines, t=2.0)  # no chord_track passed at all

    assert scene.image_key == line_hash("[Instrumental]")


def test_plausible_sung_intervals_normal_line_matches_its_own_start_and_end():
    line = LyricLine(
        words=[Word(word="hello", start_time=0.0, end_time=0.5), Word(word="there", start_time=0.5, end_time=1.0)],
        start_time=0.0, end_time=1.0,
    )

    assert _plausible_sung_intervals(line) == [(0.0, 1.0)]


def test_plausible_sung_intervals_caps_an_outlier_word_duration_and_splits_on_the_resulting_gap():
    """Real bug, 2026-09-09 'speak to me' redo: forced alignment gave the
    word 'Breathe,' a 105-second duration (46.98-152.20) while the rest of
    the line's words were tightly clustered together 8 seconds later
    (153.92-163.20). The outlier word's duration must be capped, and the
    resulting gap correctly splits the line into two real intervals."""
    line = LyricLine(
        words=[
            Word(word="Breathe,", start_time=46.98, end_time=152.20),
            Word(word="breathe", start_time=153.92, end_time=154.28),
            Word(word="in", start_time=154.32, end_time=154.46),
            Word(word="the", start_time=154.52, end_time=154.66),
            Word(word="air,", start_time=154.92, end_time=155.80),
            Word(word="don't", start_time=159.98, end_time=160.38),
            Word(word="be", start_time=160.44, end_time=160.60),
            Word(word="afraid", start_time=160.74, end_time=161.82),
            Word(word="to", start_time=161.82, end_time=161.94),
            Word(word="care", start_time=162.10, end_time=163.20),
        ],
        start_time=46.98, end_time=163.20,
    )

    intervals = _plausible_sung_intervals(line, max_word_duration=3.0, max_gap=5.0)

    assert intervals == [(46.98, 49.98), (153.92, 163.20)]


def test_plausible_sung_intervals_empty_line_returns_no_intervals():
    assert _plausible_sung_intervals(LyricLine()) == []


def test_plausible_sung_intervals_falls_back_to_line_window_when_words_have_no_timing():
    """A line with a real (start_time, end_time) but no per-word timing at
    all has nothing to sanity-check against -- it must keep working exactly
    as before this fix, not lose its 'in a line' status entirely."""
    line = LyricLine(start_time=0.0, end_time=2.0)

    assert _plausible_sung_intervals(line) == [(0.0, 2.0)]


def test_in_a_line_false_during_the_fake_gap_of_a_mistimed_line():
    line = LyricLine(
        words=[
            Word(word="Breathe,", start_time=46.98, end_time=152.20),
            Word(word="care", start_time=153.92, end_time=154.28),
            Word(word="more", start_time=162.10, end_time=163.20),
        ],
        start_time=46.98, end_time=163.20,
    )

    assert _in_a_line([line], 48.0) is True    # within the capped real start of "Breathe,"
    assert _in_a_line([line], 100.0) is False  # deep in the fake 105-second gap
    assert _in_a_line([line], 163.0) is True   # within the real tightly-clustered tail


def test_build_scene_instrumental_ken_burns_paces_to_the_active_chord_not_the_mistimed_line():
    """The other half of the same real bug: once _in_a_line correctly reports
    an instrumental stretch inside a mistimed line, the Ken Burns pan must
    pace itself to the actual active chord's own (short) duration -- not the
    (possibly very long) span between the tracked line and the next one --
    or the pan still reads as frozen even though the image is now correctly
    following the chord."""
    line = LyricLine(
        words=[
            Word(word="Breathe,", start_time=46.98, end_time=152.20),
            Word(word="care", start_time=153.92, end_time=163.20),
        ],
        start_time=46.98, end_time=163.20,
    )
    chord_track = ChordTrack(events=[
        ChordEvent(46.98, 72.1, "Dmaj7"), ChordEvent(72.1, 132.0, "Bm7"), ChordEvent(132.0, 153.92, "E"),
    ])

    early = build_scene([line], t=135.0, chord_track=chord_track, audio_duration=240.0)
    late = build_scene([line], t=150.0, chord_track=chord_track, audio_duration=240.0)

    # Both instants sit inside the SAME chord event (E, 132.0-153.92) -- if
    # Ken Burns were still paced to the old current-line-to-audio-end span
    # (46.98 to 240.0, matching this test's own mistimed line), progress at
    # both points would barely move. Paced to the 21.92-second chord instead,
    # the difference between t=135 and t=150 is a real, substantial jump.
    assert late.ken_burns_progress - early.ken_burns_progress > 0.3


def test_build_scene_blanks_stale_current_line_during_a_real_instrumental_gap():
    """Real bug, 2026-09-10 ("Wish You Were Here"): a line's forced-alignment
    end_time stretched 85 seconds past its own real content, all the way to
    the next real line's start_time (radio-dialogue intro text with nothing
    to align against until real singing resumed) -- find_current_line_index
    keys only on start_time, so that stale line's text just sat on screen
    the entire gap even though _in_a_line already knew nothing was actually
    being sung there. The upcoming (distance_from_current=1) line must still
    show normally -- only the CURRENT slot goes blank."""
    lines = [
        LyricLine(
            words=[
                Word(word="Yes", start_time=9.56, end_time=9.9),
                Word(word="nonsense", start_time=9.9, end_time=10.2),
            ],
            start_time=9.56, end_time=94.36,
        ),
        LyricLine(
            words=[
                Word(word="Now", start_time=94.98, end_time=95.2),
                Word(word="which", start_time=95.2, end_time=95.5),
            ],
            start_time=94.98, end_time=96.58,
        ),
    ]

    mid_gap = build_scene(lines, t=50.0, window=1)
    current = next(l for l in mid_gap.lines if l.distance_from_current == 0)
    upcoming = next(l for l in mid_gap.lines if l.distance_from_current == 1)
    assert current.words == []
    assert current.text == ""
    assert [w.text for w in upcoming.words] == ["Now", "which"]

    within_real_content = build_scene(lines, t=9.7, window=1)
    still_current = next(l for l in within_real_content.lines if l.distance_from_current == 0)
    assert [w.text for w in still_current.words] == ["Yes", "nonsense"]
