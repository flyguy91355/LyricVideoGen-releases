from lyricvideo.models import ChordWord, InstrumentalChord, LyricLine
from lyricvideo.layout import build_scene, find_current_line_index


def _make_lines():
    return [
        LyricLine(
            words=[
                ChordWord(word="hello", chord="G", start_time=0.0, end_time=0.5),
                ChordWord(word="there", chord=None, start_time=0.5, end_time=1.0),
            ],
            start_time=0.0, end_time=1.0,
        ),
        LyricLine(
            words=[
                ChordWord(word="my", chord="D", start_time=1.0, end_time=1.3),
                ChordWord(word="friend", chord=None, start_time=1.3, end_time=2.0),
            ],
            start_time=1.0, end_time=2.0,
        ),
        LyricLine(
            words=[ChordWord(word="goodbye", chord="Em", start_time=2.0, end_time=2.6)],
            start_time=2.0, end_time=3.0,
        ),
    ]


def test_find_current_line_index():
    lines = _make_lines()
    assert find_current_line_index(lines, 0.2) == 0
    assert find_current_line_index(lines, 1.5) == 1
    assert find_current_line_index(lines, 2.9) == 2


def test_build_scene_current_line_shows_all_its_chords_immediately():
    lines = _make_lines()
    # t=0.0: line just started, "there" (chord=None) has no chord anyway, but
    # "hello"'s chord "G" should already be visible even before its own start_time
    # elapses further into the line -- only chord_active gates the highlight.
    scene = build_scene(lines, t=0.0)
    current = next(l for l in scene.lines if l.is_current)
    assert [w.text for w in current.words] == ["hello", "there"]
    assert current.words[0].chord == "G"


def test_build_scene_chord_active_flag_tracks_start_time_but_chord_stays_visible():
    lines = _make_lines()
    scene_early = build_scene(lines, t=0.9)
    current_early = next(l for l in scene_early.lines if l.is_current)
    assert current_early.words[0].chord == "G"
    assert current_early.words[0].chord_active is True  # 0.9 >= 0.0, already hit

    scene_next_line = build_scene(lines, t=1.1)
    current_next = next(l for l in scene_next_line.lines if l.is_current)
    assert current_next.words[0].chord == "D"
    assert current_next.words[0].chord_active is True  # 1.1 >= 1.0, already hit


def test_build_scene_next_line_shows_its_chords_but_never_active():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    next_line = next(l for l in scene.lines if l.distance_from_current == 1)
    assert next_line.words[0].chord == "Em"
    assert next_line.words[0].chord_active is False


def test_build_scene_shows_only_current_and_next_line_never_previous():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    offsets = sorted(l.distance_from_current for l in scene.lines)
    assert offsets == [0, 1]


def test_build_scene_chord_flash_decays_after_being_hit():
    lines = _make_lines()
    just_hit = build_scene(lines, t=0.0)
    current_just_hit = next(l for l in just_hit.lines if l.is_current)
    assert current_just_hit.words[0].chord_flash == 1.0

    mid_flash = build_scene(lines, t=0.15)
    current_mid = next(l for l in mid_flash.lines if l.is_current)
    assert 0.0 < current_mid.words[0].chord_flash < 1.0

    settled = build_scene(lines, t=0.9)
    current_settled = next(l for l in settled.lines if l.is_current)
    assert current_settled.words[0].chord_flash == 0.0
    assert current_settled.words[0].chord_active is True  # still active, just no longer flashing


def test_build_scene_word_active_sweeps_left_to_right_on_current_line():
    lines = _make_lines()
    # line 0: "hello"(0.0-0.5) "there"(0.5-1.0)
    scene_at_start = build_scene(lines, t=0.0)
    current_start = next(l for l in scene_at_start.lines if l.is_current)
    assert current_start.words[0].word_active is True   # "hello" reached
    assert current_start.words[1].word_active is False  # "there" not yet

    scene_mid = build_scene(lines, t=0.6)
    current_mid = next(l for l in scene_mid.lines if l.is_current)
    assert current_mid.words[0].word_active is True
    assert current_mid.words[1].word_active is True  # "there" now reached too


def test_build_scene_next_line_words_never_marked_active():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    next_line = next(l for l in scene.lines if l.distance_from_current == 1)
    assert all(w.word_active is False for w in next_line.words)


def test_build_scene_next_line_chords_never_flash():
    lines = _make_lines()
    scene = build_scene(lines, t=1.0, window=1)  # line 1 just became current at t=1.0
    next_line = next(l for l in scene.lines if l.distance_from_current == 1)
    assert next_line.words[0].chord_flash == 0.0


def test_build_scene_shows_active_instrumental_chord():
    lines = _make_lines()
    instrumental_chords = [
        InstrumentalChord(chord="Em7", start_time=-2.0, end_time=-1.0),
        InstrumentalChord(chord="G", start_time=-1.0, end_time=0.0),
    ]
    scene_first = build_scene(lines, t=-1.5, instrumental_chords=instrumental_chords)
    assert scene_first.instrumental_chord == "Em7"

    scene_second = build_scene(lines, t=-0.5, instrumental_chords=instrumental_chords)
    assert scene_second.instrumental_chord == "G"


def test_build_scene_instrumental_chord_none_when_not_covered():
    lines = _make_lines()
    scene = build_scene(lines, t=0.5, instrumental_chords=[])
    assert scene.instrumental_chord is None


def test_build_scene_ken_burns_progress_increases_within_line():
    lines = _make_lines()
    start_scene = build_scene(lines, t=1.0)
    mid_scene = build_scene(lines, t=1.5)
    end_scene = build_scene(lines, t=1.99)
    assert start_scene.ken_burns_progress < mid_scene.ken_burns_progress < end_scene.ken_burns_progress


def test_build_scene_ken_burns_progress_spans_gap_until_next_line_not_just_singing_end():
    # A real gap: line 0 stops being sung at t=1.0, but line 1 doesn't start
    # until t=3.0 (a 2-second instrumental pause) -- the image for line 0
    # should keep panning/zooming the whole time it's on screen (until t=3.0),
    # not freeze at t=1.0 when the previous (buggy) behavior would have hit
    # progress=1.0 already.
    lines = [
        LyricLine(
            words=[ChordWord(word="hello", chord="G", start_time=0.0, end_time=1.0)],
            start_time=0.0, end_time=1.0,
        ),
        LyricLine(
            words=[ChordWord(word="friend", chord="D", start_time=3.0, end_time=3.5)],
            start_time=3.0, end_time=3.5,
        ),
    ]

    at_singing_end = build_scene(lines, t=1.0)
    mid_gap = build_scene(lines, t=2.0)
    just_before_next = build_scene(lines, t=2.99)

    assert at_singing_end.ken_burns_progress < 1.0
    assert at_singing_end.ken_burns_progress < mid_gap.ken_burns_progress < just_before_next.ken_burns_progress
    assert just_before_next.ken_burns_progress < 1.0

    # scroll_progress, in contrast, is about the line's own singing window and
    # correctly reaches 1.0 already at t=1.0 -- it must NOT wait for the gap.
    assert at_singing_end.scroll_progress == 1.0


def test_build_scene_ken_burns_progress_uses_audio_duration_for_last_line():
    lines = [
        LyricLine(
            words=[ChordWord(word="hello", chord="G", start_time=0.0, end_time=1.0)],
            start_time=0.0, end_time=1.0,
        ),
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
