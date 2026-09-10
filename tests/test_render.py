import numpy as np
from PIL import Image, ImageDraw, ImageFont

from lyricvideo.render import (
    CHORD_BOX, FRAME_SIZE, KEN_BURNS_PRESETS,
    apply_ken_burns, compute_chord_bar_layout, draw_chord_bar, draw_countdown, draw_scene,
    ken_burns_preset_for_key, _lane_label_font, _split_line_into_rows,
)
from lyricvideo.layout import Scene, SceneLine, SceneWord
from lyricvideo.models import ChordEvent, ChordTrack


def test_apply_ken_burns_returns_frame_sized_image():
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    out_start = apply_ken_burns(img, progress=0.0)
    out_end = apply_ken_burns(img, progress=1.0)
    assert out_start.size == FRAME_SIZE
    assert out_end.size == FRAME_SIZE


def test_apply_ken_burns_respects_custom_frame_size():
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    out = apply_ken_burns(img, progress=0.5, frame_size=(1280, 720))
    assert out.size == (1280, 720)


def test_apply_ken_burns_pans_toward_end_position():
    img = Image.new("RGB", (800, 600))
    for x in range(800):
        for y in range(0, 600, 50):
            img.paste((x % 256, 0, 0), (x, y, x + 1, y + 50))

    start = np.array(apply_ken_burns(img, progress=0.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    end = np.array(apply_ken_burns(img, progress=1.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    assert not np.array_equal(start, end)


def test_apply_ken_burns_can_zoom_out():
    img = Image.new("RGB", (800, 600), (50, 50, 50))
    tight = apply_ken_burns(img, progress=0.0, zoom_start=1.2, zoom_end=1.0)
    wide = apply_ken_burns(img, progress=1.0, zoom_start=1.2, zoom_end=1.0)
    assert tight.size == wide.size == FRAME_SIZE


def test_ken_burns_preset_for_key_is_deterministic():
    assert ken_burns_preset_for_key("same-key") == ken_burns_preset_for_key("same-key")


def test_ken_burns_preset_for_key_varies_across_keys():
    presets_seen = {ken_burns_preset_for_key(f"key-{i}") for i in range(len(KEN_BURNS_PRESETS) * 3)}
    assert len(presets_seen) > 1


def test_draw_scene_renders_without_error_and_draws_text(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    scene = Scene(
        lines=[
            SceneLine(
                words=[SceneWord(text="hello"), SceneWord(text="there")],
                is_current=True,
                distance_from_current=0,
            ),
        ],
        image_key="abc123",
        ken_burns_progress=0.5,
    )

    frame = draw_scene(scene, bg, test_font_path)

    assert frame.size == FRAME_SIZE
    assert frame.getextrema() != ((0, 0), (0, 0), (0, 0))


def test_draw_scene_scroll_progress_shifts_line_position(test_font_path):
    def make_scene(scroll_progress):
        return Scene(
            lines=[
                SceneLine(words=[SceneWord(text="hello")], is_current=True, distance_from_current=0),
            ],
            image_key="abc123",
            ken_burns_progress=0.0,
            scroll_progress=scroll_progress,
        )

    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    frame_at_start = np.array(draw_scene(make_scene(0.0), bg, test_font_path))
    frame_at_mid = np.array(draw_scene(make_scene(0.5), bg, test_font_path))

    assert not np.array_equal(frame_at_start, frame_at_mid)


def test_draw_scene_respects_custom_text_color(test_font_path):
    scene = Scene(
        lines=[SceneLine(words=[SceneWord(text="hi")], is_current=True, distance_from_current=0)],
        image_key="k", ken_burns_progress=0.0,
    )
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))

    default_frame = np.array(draw_scene(scene, bg, test_font_path))
    red_frame = np.array(draw_scene(scene, bg, test_font_path, text_color=(255, 0, 0)))

    assert not np.array_equal(default_frame, red_frame)


def test_draw_scene_respects_custom_frame_size(test_font_path):
    scene = Scene(
        lines=[SceneLine(words=[SceneWord(text="hi")], is_current=True, distance_from_current=0)],
        image_key="k", ken_burns_progress=0.0,
    )
    bg = Image.new("RGB", (1280, 720), (0, 0, 0))

    frame = draw_scene(scene, bg, test_font_path, frame_size=(1280, 720))

    assert frame.size == (1280, 720)


def test_compute_chord_bar_layout_scales_with_frame_size():
    default_layout = compute_chord_bar_layout(FRAME_SIZE)
    small_layout = compute_chord_bar_layout((1280, 720))

    assert default_layout["chord_box"] == CHORD_BOX
    assert small_layout["chord_box"] != CHORD_BOX
    assert all(0 <= c <= 1280 for c in (small_layout["chord_box"][0], small_layout["chord_box"][2]))


def test_draw_chord_bar_renders_without_error(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G")], key="C major", bpm=120.0)

    frame = draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path)

    assert frame.size == FRAME_SIZE
    assert frame.crop(CHORD_BOX).getextrema() != ((20, 20), (20, 20), (20, 20))


def test_draw_chord_bar_shows_dash_when_no_current_chord(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(5.0, 7.0, "C")])

    frame = draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path)

    assert frame.size == FRAME_SIZE


def test_draw_chord_bar_does_not_mutate_input_frame(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")])

    draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path)

    assert bg.getextrema() == ((20, 20), (20, 20), (20, 20))


def test_draw_chord_bar_respects_custom_accent_color(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")])

    default_frame = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path))
    red_frame = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, accent_color=(255, 0, 0)))

    assert not np.array_equal(default_frame, red_frame)


def test_draw_chord_bar_hides_timeline_lane_when_disabled(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 20.0, "G")])

    shown = draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_chord_timeline=True)
    hidden = draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_chord_timeline=False)

    layout = compute_chord_bar_layout(FRAME_SIZE)
    lane_box = layout["lane_box"]
    # With the lane hidden, that region must still show the plain panel background,
    # not a chord segment block -- so it must differ from the shown-lane version.
    assert not np.array_equal(np.array(shown.crop(lane_box)), np.array(hidden.crop(lane_box)))


def test_draw_chord_bar_hides_key_bpm_badge_when_disabled(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")], key="C major", bpm=120.0)

    shown = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_key_bpm=True))
    hidden = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_key_bpm=False))

    assert not np.array_equal(shown, hidden)


def test_draw_chord_bar_respects_custom_timeline_window(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    # A chord far enough out that only a wide window includes any of it.
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G"), ChordEvent(20.0, 22.0, "Am")])

    narrow = np.array(draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path, timeline_window_sec=3.0))
    wide = np.array(draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path, timeline_window_sec=25.0))

    assert not np.array_equal(narrow, wide)


def test_lane_label_font_returns_base_font_when_label_fits(test_font_path):
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    base_font = ImageFont.truetype(test_font_path, 30)

    font = _lane_label_font("C", draw, base_font, test_font_path, available_width=500, min_size=18)

    assert font is base_font


def test_lane_label_font_shrinks_when_label_does_not_fit(test_font_path):
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    base_font = ImageFont.truetype(test_font_path, 30)

    font = _lane_label_font("F#maj7", draw, base_font, test_font_path, available_width=40, min_size=18)

    assert font.size < base_font.size
    assert font.size >= 18


def test_lane_label_font_never_shrinks_below_the_floor(test_font_path):
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    base_font = ImageFont.truetype(test_font_path, 30)

    font = _lane_label_font("F#maj7", draw, base_font, test_font_path, available_width=1, min_size=18)

    assert font.size == 18


def test_draw_chord_bar_still_draws_a_label_for_a_very_short_chord_segment(test_font_path):
    """Real owner-reported issue (2026-09-09): a short-duration chord's
    timeline-lane box was too narrow to fit its label at the default size, so
    the label was skipped entirely -- a blank colored box with no chord name,
    even though the highlight/sync was correct. The label must now always be
    drawn (shrunk to fit, or overflowing into the next segment as a last
    resort) rather than ever silently disappearing."""
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    # A long label ("F#maj7") crammed into a very short 0.3s slot within a
    # wide 12s timeline window -- narrow enough that the label cannot fit at
    # the default lane font size.
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 0.3, "F#maj7"), ChordEvent(0.3, 12.0, "G"),
    ])
    layout = compute_chord_bar_layout(FRAME_SIZE)
    lx0, ly0, lx1, ly1 = layout["lane_box"]
    pps = (lx1 - lx0) / 12.0
    bx0, bx1 = int(lx0) + 1, int(lx0 + 0.3 * pps) - 1

    frame = draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path, timeline_window_sec=12.0)
    region = np.array(frame.crop((bx0, ly0 + 6, bx1, ly1 - 6)))

    # More than one distinct color in the segment's own box means something
    # (the label) was drawn on top of its solid fill -- not just a blank box.
    unique_colors = {tuple(pixel) for row in region for pixel in row}
    assert len(unique_colors) > 1


def _row_width(row, draw, font):
    space_w = draw.textlength(" ", font=font)
    widths = [draw.textlength(w.text, font=font) for w in row]
    return sum(widths) + space_w * max(len(row) - 1, 0)


def test_split_line_into_rows_keeps_a_short_line_on_one_row(test_font_path):
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(test_font_path, 20)
    words = [SceneWord(text=w) for w in "hello there".split()]

    rows = _split_line_into_rows(words, draw, font, max_width=1000)

    assert len(rows) == 1
    assert [w.text for w in rows[0]] == ["hello", "there"]


def test_split_line_into_rows_splits_at_commas_when_too_wide(test_font_path):
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(test_font_path, 20)
    words = [SceneWord(text=w) for w in "one, two, three, four, five, six".split()]
    full_width = _row_width(words, draw, font)

    rows = _split_line_into_rows(words, draw, font, max_width=full_width * 0.5)

    assert len(rows) > 1
    # No words lost or reordered.
    assert [w.text for row in rows for w in row] == [w.text for w in words]
    # Every row must actually fit inside max_width.
    for row in rows:
        assert _row_width(row, draw, font) <= full_width * 0.5
    # Splits happen after a comma wherever a row has more than one word.
    for row in rows[:-1]:
        if len(row) > 1:
            assert row[-1].text.endswith(",")


def test_split_line_into_rows_falls_back_to_word_wrap_without_commas(test_font_path):
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(test_font_path, 20)
    words = [SceneWord(text=w) for w in "supercalifragilisticexpialidocious is quite a remarkably long word indeed today".split()]
    full_width = _row_width(words, draw, font)

    rows = _split_line_into_rows(words, draw, font, max_width=full_width * 0.5)

    assert len(rows) > 1
    assert [w.text for row in rows for w in row] == [w.text for w in words]
    for row in rows:
        assert _row_width(row, draw, font) <= full_width * 0.5


def test_split_line_into_rows_never_drops_a_single_word_wider_than_max_width(test_font_path):
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(test_font_path, 20)
    words = [SceneWord(text="Supercalifragilisticexpialidocious")]

    rows = _split_line_into_rows(words, draw, font, max_width=10)

    assert [w.text for row in rows for w in row] == ["Supercalifragilisticexpialidocious"]


def test_draw_scene_wraps_a_long_line_so_it_never_touches_the_frame_edges(test_font_path):
    # The exact line that overflowed both edges of a real rendered video
    # (2026-09-09, "speak-to-me-breathe") at the default 48pt font size.
    long_text = (
        "Run, rabbit, run, dig that hole, forget the Sun, and when at last "
        "the work is done, don't sit down, its time to dig another one"
    )
    scene = Scene(
        lines=[
            SceneLine(
                words=[SceneWord(text=w) for w in long_text.split()],
                is_current=True, distance_from_current=0,
            ),
        ],
        image_key="k", ken_burns_progress=0.0,
    )
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))

    frame = np.array(draw_scene(scene, bg, test_font_path))

    edge_width = 10
    assert np.array_equal(frame[:, :edge_width], np.zeros_like(frame[:, :edge_width]))
    assert np.array_equal(frame[:, -edge_width:], np.zeros_like(frame[:, -edge_width:]))


def test_draw_scene_current_and_next_line_never_overlap_when_both_wrap(test_font_path):
    """Real regression, 2026-09-09 'speak to me' redo: the lyric-wrap fix let
    a line take multiple rows but never widened the fixed gap between the
    current-line slot and the next-line preview slot below it, so a long
    current line's lower rows rendered on top of the next line's text."""
    current_text = (
        "Run, rabbit, run, dig that hole, forget the Sun, and when at last "
        "the work is done, don't sit down, its time to dig another one"
    )
    next_text = (
        "Long you live, and high you fly, but only if you ride the tide, "
        "balanced on the biggest wave, you race towards an early grave"
    )
    scene = Scene(
        lines=[
            SceneLine(
                words=[SceneWord(text=w) for w in current_text.split()],
                is_current=True, distance_from_current=0,
            ),
            SceneLine(
                words=[SceneWord(text=w) for w in next_text.split()],
                is_current=False, distance_from_current=1,
            ),
        ],
        image_key="k", ken_burns_progress=0.0, scroll_progress=0.0,
    )
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))

    frame = np.array(draw_scene(scene, bg, test_font_path))

    rows_with_content = np.any(frame != 0, axis=(1, 2))
    content_rows = np.where(rows_with_content)[0]
    assert content_rows.size > 0

    # With the old fixed single-row line_height (77px for this font/size) the
    # next line's 2-row block would land squarely inside the current line's
    # own 3-row block (measured directly: total span collapses to ~225px and
    # the two blocks' pixel ranges overlap). The dynamic per-line-height step
    # this test guards keeps them apart, spreading the real content across
    # ~317px. 280 sits clearly between the two -- only the fixed behavior
    # reaches it.
    total_span = content_rows.max() - content_rows.min()
    assert total_span >= 280


def test_draw_scene_wrapped_line_keeps_the_configured_font_size(test_font_path):
    """No bandaid font-shrinking -- a long line wraps onto more rows at the
    exact same font size, never smaller."""
    from PIL import ImageFont as PILImageFont

    long_text = (
        "Run, rabbit, run, dig that hole, forget the Sun, and when at last "
        "the work is done, don't sit down, its time to dig another one"
    )
    scene = Scene(
        lines=[
            SceneLine(
                words=[SceneWord(text=w) for w in long_text.split()],
                is_current=True, distance_from_current=0,
            ),
        ],
        image_key="k", ken_burns_progress=0.0,
    )
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))

    frame = np.array(draw_scene(scene, bg, test_font_path, font_size=48))
    reference_font = PILImageFont.truetype(test_font_path, 48)
    ascent, descent = reference_font.getmetrics()

    # The wrapped text must still reach the full glyph height a real 48pt
    # font produces -- proof the font was never shrunk to fit.
    rows_with_content = np.any(frame != 0, axis=(1, 2))
    content_rows = np.where(rows_with_content)[0]
    assert content_rows.size > 0
    assert (content_rows.max() - content_rows.min()) >= (ascent + descent)


def test_draw_countdown_renders_without_error(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))

    frame = draw_countdown(bg, 3, test_font_path)

    assert frame.size == FRAME_SIZE
    assert frame.getextrema() != ((20, 20), (20, 20), (20, 20))


def test_draw_countdown_does_not_mutate_input_frame(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))

    draw_countdown(bg, 3, test_font_path)

    assert bg.getextrema() == ((20, 20), (20, 20), (20, 20))


def test_draw_countdown_stays_modest_in_size(test_font_path):
    """Owner feedback, 2026-09-10: 'dont make it gawdy' -- the countdown
    panel must stay a small centered element, not dominate the frame."""
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))

    frame = draw_countdown(bg, 3, test_font_path)

    changed = np.array(frame).any(axis=2)
    changed_fraction = changed.sum() / changed.size
    assert changed_fraction < 0.15


def test_draw_countdown_different_numbers_produce_different_frames(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))

    frame_3 = np.array(draw_countdown(bg, 3, test_font_path))
    frame_1 = np.array(draw_countdown(bg, 1, test_font_path))

    assert not np.array_equal(frame_3, frame_1)
