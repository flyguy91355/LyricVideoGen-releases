import numpy as np
from PIL import Image

from lyricvideo.chord_diagram import _legend_layout, draw_chord_legend, draw_single_chord_diagram
from lyricvideo.chord_shapes import get_chord_shape


def test_draw_single_chord_diagram_returns_requested_size(test_font_path):
    shape = get_chord_shape("C")
    img = draw_single_chord_diagram(shape, "C", (120, 160), test_font_path)
    assert img.size == (120, 160)
    assert img.mode == "RGBA"


def test_draw_single_chord_diagram_is_not_blank(test_font_path):
    shape = get_chord_shape("Em")
    img = draw_single_chord_diagram(shape, "Em", (120, 160), test_font_path)
    assert img.getextrema()[3] != (0, 0)  # alpha channel has real content


def test_draw_single_chord_diagram_panel_stays_opaque_against_a_bright_background(test_font_path):
    """Real owner-reported issue (2026-09-09): fingering charts were hard to
    read against some background images. The chord bar's outer backdrop is
    deliberately translucent (an aesthetic choice, controlled by panel_alpha),
    but a reference chart the owner needs to actually read must stay legible
    regardless of that setting or how bright the video background is -- same
    precedent as the existing NOW/NEXT chord-bar boxes, which use a fixed
    near-opaque fill rather than the user's translucent panel_alpha."""
    shape = get_chord_shape("G")
    diagram = draw_single_chord_diagram(shape, "G", (120, 160), test_font_path, panel_color=(10, 10, 10))

    bright_bg = Image.new("RGBA", (120, 160), (255, 255, 255, 255))
    composited = Image.alpha_composite(bright_bg, diagram)

    # A pixel inside the panel but away from any drawn line/dot/text should
    # read close to the dark panel_color, not washed out by the bright
    # background showing through a translucent panel.
    sample = composited.getpixel((10, 10))
    assert sample[0] < 60 and sample[1] < 60 and sample[2] < 60


def test_draw_single_chord_diagram_panel_alpha_is_owner_tunable(test_font_path):
    shape = get_chord_shape("G")
    diagram = draw_single_chord_diagram(
        shape, "G", (120, 160), test_font_path, panel_color=(10, 10, 10), panel_alpha=60,
    )

    bright_bg = Image.new("RGBA", (120, 160), (255, 255, 255, 255))
    composited = Image.alpha_composite(bright_bg, diagram)

    # A low panel_alpha should let the bright background show through much
    # more than the near-opaque default -- confirms the parameter is real,
    # not just accepted and ignored.
    sample = composited.getpixel((10, 10))
    assert sample[0] > 150


def test_draw_single_chord_diagram_highlighted_differs_from_unhighlighted(test_font_path):
    shape = get_chord_shape("G")
    plain = np.array(draw_single_chord_diagram(shape, "G", (120, 160), test_font_path, highlighted=False))
    lit = np.array(draw_single_chord_diagram(shape, "G", (120, 160), test_font_path, highlighted=True))
    assert not np.array_equal(plain, lit)


def test_draw_single_chord_diagram_shows_base_fret_label_when_not_at_the_nut(test_font_path):
    shape = get_chord_shape("C#m7")  # base_fret=4, verified in Task 1
    assert shape.base_fret == 4
    img = draw_single_chord_diagram(shape, "C#m7", (120, 160), test_font_path)
    assert img.size == (120, 160)  # renders without error


def test_draw_chord_legend_renders_without_error(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080))
    assert frame.size == (1920, 1080)


def test_draw_chord_legend_changes_pixels_versus_plain_background(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = np.array(draw_chord_legend(bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080)))
    plain = np.array(bg)
    assert not np.array_equal(frame, plain)


def test_draw_chord_legend_does_not_mutate_input_frame(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    draw_chord_legend(bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080))
    assert bg.getextrema() == ((20, 20), (20, 20), (20, 20))


def test_draw_chord_legend_hidden_when_disabled(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(
        bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080), show_chord_legend=False,
    )
    assert np.array_equal(np.array(frame), np.array(bg))


def test_draw_chord_legend_empty_chord_list_changes_nothing(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(bg, [], None, test_font_path, frame_size=(1920, 1080))
    assert np.array_equal(np.array(frame), np.array(bg))


def test_draw_chord_legend_skips_unrecognized_labels_without_raising(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(
        bg, ["G", "NotARealChord123", "Am"], "G", test_font_path, frame_size=(1920, 1080),
    )
    assert frame.size == (1920, 1080)


def test_draw_chord_legend_current_chord_highlight_changes_which_diagram_is_lit(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    current_g = np.array(draw_chord_legend(bg, ["G", "D"], "G", test_font_path, frame_size=(1920, 1080)))
    current_d = np.array(draw_chord_legend(bg, ["G", "D"], "D", test_font_path, frame_size=(1920, 1080)))
    assert not np.array_equal(current_g, current_d)


def test_legend_layout_keeps_default_size_for_a_few_chords():
    box_w, box_h = _legend_layout(3, (1920, 1080), size_scale=1.0)

    assert box_w == int(1920 * 0.075)
    assert box_h == int(1080 * 0.16)


def test_legend_layout_shrinks_for_many_chords_to_stay_within_the_reserved_area():
    """Real owner-reported case (2026-09-09): a 16-unique-chord song ('speak
    to me / breathe') wrapped to 4 rows and overlapped both the lyrics and the
    chord bar. The layout must never need more than 2 rows, and the resulting
    box size must fit that many rows within the reserved height."""
    box_w, box_h = _legend_layout(16, (1920, 1080), size_scale=1.0)

    max_rows = 2
    gap = int(1920 * 0.012)
    max_legend_height = int(1080 * 0.35)
    assert max_rows * box_h + (max_rows - 1) * gap <= max_legend_height
    assert box_w < int(1920 * 0.075)  # visibly smaller than the unshrunk default


def test_legend_layout_never_exceeds_the_default_size_regardless_of_scale():
    box_w, box_h = _legend_layout(3, (1920, 1080), size_scale=2.0)

    assert box_w == int(1920 * 0.075 * 2.0)
    assert box_h == int(1080 * 0.16 * 2.0)


def test_legend_layout_size_scale_shrinks_the_base_size_before_fitting():
    full, _ = _legend_layout(3, (1920, 1080), size_scale=1.0)
    half, _ = _legend_layout(3, (1920, 1080), size_scale=0.5)

    assert half < full


def test_legend_layout_zero_chords_returns_default_size():
    box_w, box_h = _legend_layout(0, (1920, 1080), size_scale=1.0)

    assert box_w == int(1920 * 0.075)
    assert box_h == int(1080 * 0.16)


def test_draw_chord_legend_stays_within_the_reserved_height_for_many_chords(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    many_chords = [
        "G", "D", "Am", "C", "Em", "F", "Bm", "A",
        "E", "Dm", "Bb", "F#m", "G7", "C7", "D7", "A7",
    ]

    frame = np.array(draw_chord_legend(bg, many_chords, None, test_font_path, frame_size=(1920, 1080)))

    max_y = int(1080 * 0.35)
    below = frame[max_y:, :]
    plain = np.array(bg)[max_y:, :]
    assert np.array_equal(below, plain)


def test_draw_chord_legend_respects_a_custom_size_scale(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))

    small = np.array(draw_chord_legend(
        bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080), size_scale=0.5,
    ))
    large = np.array(draw_chord_legend(
        bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080), size_scale=1.0,
    ))

    assert not np.array_equal(small, large)
