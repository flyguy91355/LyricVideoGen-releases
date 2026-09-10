from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word, line_hash
from lyricvideo.layout import build_scene, find_current_line_index


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
