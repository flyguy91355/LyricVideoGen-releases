import numpy as np
import pytest
from PIL import Image

from lyricvideo.chord_diagram import _LEGEND_MAX_HEIGHT_FRAC, _legend_layout, draw_capo_badge, draw_chord_legend, draw_single_chord_diagram
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


# --- capo badge (owner, 2026-09-23: "i want the CAPO 3 under the chords finger position area") ----------------

def test_draw_capo_badge_renders_without_error(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_capo_badge(bg, 3, test_font_path, frame_size=(1920, 1080))
    assert frame.size == (1920, 1080)


def test_draw_capo_badge_changes_pixels_versus_plain_background(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = np.array(draw_capo_badge(bg, 3, test_font_path, frame_size=(1920, 1080)))
    plain = np.array(bg)
    assert not np.array_equal(frame, plain)


def test_draw_capo_badge_does_not_mutate_input_frame(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    draw_capo_badge(bg, 3, test_font_path, frame_size=(1920, 1080))
    assert bg.getextrema() == ((20, 20), (20, 20), (20, 20))


def test_draw_capo_badge_hidden_when_capo_is_none():
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_capo_badge(bg, None, "unused.ttf", frame_size=(1920, 1080))
    assert np.array_equal(np.array(frame), np.array(bg))


def test_draw_capo_badge_sits_below_the_legends_own_reserved_height(test_font_path):
    """Never overlaps the chord-fingering legend above it, whatever size that legend actually drew at."""
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = np.array(draw_capo_badge(bg, 3, test_font_path, frame_size=(1920, 1080)))

    reserved_for_legend = int(1080 * _LEGEND_MAX_HEIGHT_FRAC)
    above = frame[:reserved_for_legend, :]
    plain_above = np.array(bg)[:reserved_for_legend, :]
    assert np.array_equal(above, plain_above)


# --- issue #7 review ---------------------------------------------------------------------------------------------

def _textured_frame(size=(1920, 1080), seed=3):
    """Random pixels: a composite that is off by even one value somewhere shows up against this."""
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8), "RGB")


@pytest.mark.parametrize("label", ["Abm", "G#maj7", "C#m7"])
@pytest.mark.parametrize(
    "n_chords,frame_size", [(3, (1920, 1080)), (3, (1280, 720)), (16, (1920, 1080)), (5, (3840, 2160))],
)
def test_base_fret_tag_sits_clear_of_every_string_fret_line_and_dot(label, n_chords, frame_size, test_font_path):
    """The "4fr" tag was squeezed in at the right edge, where the high-e string and the top fret line ran through it
    and that string's barre dot touched it, on every shape that does not start at the nut."""
    from PIL import ImageDraw

    from lyricvideo.chord_diagram import _diagram_geometry

    shape = get_chord_shape(label)
    assert shape.base_fret != 1
    box = _legend_layout(n_chords, frame_size)
    draw = ImageDraw.Draw(Image.new("RGBA", box))
    g = _diagram_geometry(shape, box, test_font_path, draw)

    x0, y0, x1, y1 = draw.textbbox(g.fret_tag_xy, g.fret_tag, font=g.small_font)
    assert g.fret_tag == "4fr"
    assert 3 <= x0 and x1 <= box[0] and 0 <= y0 and y1 <= box[1]  # inside the panel, clear of its outline
    assert x1 < g.string_xs[0] - g.dot_radius  # left of the low-E string AND of any dot on it
    for i, fret in enumerate(shape.frets):
        if fret > 0:
            cy = (g.fret_ys[fret - 1] + g.fret_ys[fret]) / 2
            dot = (g.string_xs[i] - g.dot_radius, cy - g.dot_radius, g.string_xs[i] + g.dot_radius, cy + g.dot_radius)
            assert x1 < dot[0] or dot[3] < y0 or y1 < dot[1]


def test_nut_shapes_keep_their_symmetric_grid(test_font_path):
    from PIL import ImageDraw

    from lyricvideo.chord_diagram import _diagram_geometry

    box = (144, 172)
    g = _diagram_geometry(get_chord_shape("G"), box, test_font_path, ImageDraw.Draw(Image.new("RGBA", box)))
    side_pad = int(144 * 0.12)
    assert g.fret_tag is None
    assert g.string_xs[0] == side_pad and g.string_xs[-1] == pytest.approx(144 - side_pad)


_MANY_CHORDS = ["G", "D", "Am", "C", "Em", "F", "Bm", "A", "E", "Dm", "Bb", "F#m", "G7", "C7", "D7", "A7"]


@pytest.mark.parametrize("frame_size", [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)])
@pytest.mark.parametrize("n_chords", [1, 5, 6, 7, 10, 12, 16])
def test_draw_chord_legend_never_draws_below_its_reserved_height(frame_size, n_chords, test_font_path):
    """A 2-row legend (6-10 chords) used to reach 0.38 of the frame height: the height budget ignored the top margin
    the legend is drawn at, so its second row ran past the 0.35h the capo badge is placed below."""
    labels = _MANY_CHORDS[:n_chords]
    bg = Image.new("RGB", frame_size, (20, 20, 20))

    frame = np.array(draw_chord_legend(bg, labels, labels[0], test_font_path, frame_size=frame_size))

    max_y = int(frame_size[1] * _LEGEND_MAX_HEIGHT_FRAC)
    assert np.array_equal(frame[max_y:, :], np.array(bg)[max_y:, :])


@pytest.mark.parametrize("frame_size", [(1280, 720), (1920, 1080), (2560, 1440)])
def test_capo_badge_never_overlaps_a_two_row_legend(frame_size, test_font_path):
    bg = Image.new("RGB", frame_size, (20, 20, 20))
    plain = np.array(bg)
    seven = ["D", "A", "Bm", "G", "Em", "F#m", "E"]

    legend = np.array(draw_chord_legend(bg, seven, "D", test_font_path, frame_size=frame_size))
    badge = np.array(draw_capo_badge(bg, 2, test_font_path, frame_size=frame_size))

    legend_mask = (legend != plain).any(axis=-1)
    badge_mask = (badge != plain).any(axis=-1)
    assert badge_mask.any() and legend_mask.any()
    assert not (legend_mask & badge_mask).any()


def _old_full_frame_legend(frame, labels, current_label, font_path, frame_size, panel_alpha=235):
    """The pre-cache implementation: every diagram redrawn onto a full-frame overlay, whole-frame composite."""
    from lyricvideo.chord_diagram import _LEGEND_GAP_FRAC, _LEGEND_MARGIN_FRAC, _LEGEND_MAX_ROW_WIDTH_FRAC

    resolvable = [(label, get_chord_shape(label)) for label in labels if get_chord_shape(label) is not None]
    w, h = frame_size
    box_w, box_h = _legend_layout(len(resolvable), frame_size)
    gap = int(w * _LEGEND_GAP_FRAC)
    margin_x, margin_y = int(w * _LEGEND_MARGIN_FRAC), int(h * _LEGEND_MARGIN_FRAC)
    max_row_width = int(w * _LEGEND_MAX_ROW_WIDTH_FRAC)
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    x, y = margin_x, margin_y
    for label, shape in resolvable:
        if x != margin_x and x + box_w > margin_x + max_row_width:
            x, y = margin_x, y + box_h + gap
        diagram = draw_single_chord_diagram(
            shape, label, (box_w, box_h), font_path, highlighted=(label == current_label), panel_alpha=panel_alpha,
        )
        overlay.alpha_composite(diagram, (x, y))
        x += box_w + gap
    return Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB")


@pytest.mark.parametrize("n_chords", [1, 8, 14])
@pytest.mark.parametrize("current_label", ["G", None])
@pytest.mark.parametrize("panel_alpha", [128, 235])
def test_cached_legend_is_pixel_identical_to_the_full_frame_redraw(
    n_chords, current_label, panel_alpha, test_font_path,
):
    """Each (chords, highlighted chord) state is drawn once and only its own box is composited per frame -- this
    must never change a single pixel versus redrawing and compositing the whole frame every time."""
    frame = _textured_frame()
    labels = _MANY_CHORDS[:n_chords]
    expected = np.array(_old_full_frame_legend(frame, labels, current_label, test_font_path, (1920, 1080), panel_alpha))

    for _ in range(2):  # the second call is served from the cache
        got = np.array(draw_chord_legend(
            frame, labels, current_label, test_font_path, frame_size=(1920, 1080), panel_alpha=panel_alpha,
        ))
        assert np.array_equal(got, expected)


def test_cached_legend_follows_the_highlighted_chord(test_font_path):
    frame = _textured_frame()
    labels = ["G", "D", "Am"]

    on_g = np.array(draw_chord_legend(frame, labels, "G", test_font_path, frame_size=(1920, 1080)))
    on_d = np.array(draw_chord_legend(frame, labels, "D", test_font_path, frame_size=(1920, 1080)))
    back_on_g = np.array(draw_chord_legend(frame, labels, "G", test_font_path, frame_size=(1920, 1080)))

    assert not np.array_equal(on_g, on_d)
    assert np.array_equal(on_g, back_on_g)
    assert np.array_equal(on_d, np.array(_old_full_frame_legend(frame, labels, "D", test_font_path, (1920, 1080))))


@pytest.mark.parametrize("capo", [1, 3, 7])
def test_cached_capo_badge_is_pixel_identical_to_the_full_frame_draw(capo, test_font_path):
    from PIL import ImageDraw

    from lyricvideo.chord_diagram import (
        _CAPO_BADGE_GAP_FRAC, _CAPO_BADGE_PAD_X, _CAPO_BADGE_PAD_Y, _LEGEND_MARGIN_FRAC,
    )
    from lyricvideo.render import load_font

    frame = _textured_frame()
    w, h = frame.size
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(test_font_path, int(h * 0.022))
    x, y = int(w * _LEGEND_MARGIN_FRAC), int(h * _LEGEND_MAX_HEIGHT_FRAC) + int(h * _CAPO_BADGE_GAP_FRAC)
    label = f"CAPO {capo}"
    text_w = draw.textlength(label, font=font)
    box = (x, y, x + text_w + 2 * _CAPO_BADGE_PAD_X, y + font.size + 2 * _CAPO_BADGE_PAD_Y)
    draw.rounded_rectangle(
        box, radius=int(_CAPO_BADGE_PAD_Y * 0.8), fill=(11, 18, 32, 235), outline=(56, 189, 248, 255), width=2,
    )
    draw.text((x + _CAPO_BADGE_PAD_X, y + _CAPO_BADGE_PAD_Y), label, font=font, fill=(255, 255, 255, 255))
    expected = np.array(Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB"))

    for _ in range(2):
        got = np.array(draw_capo_badge(frame, capo, test_font_path, frame_size=(1920, 1080)))
        assert np.array_equal(got, expected)


def test_legend_accepts_list_colors_like_tuples(test_font_path):
    """Colors are cache-key parts; a caller passing lists must get the same picture, not a TypeError."""
    frame = _textured_frame()
    as_tuples = draw_chord_legend(
        frame, ["G", "D"], "G", test_font_path, frame_size=(1920, 1080), accent_color=(1, 2, 3),
    )
    as_lists = draw_chord_legend(
        frame, ["G", "D"], "G", test_font_path, frame_size=[1920, 1080], accent_color=[1, 2, 3],
    )
    assert np.array_equal(np.array(as_tuples), np.array(as_lists))
