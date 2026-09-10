"""Draws guitar chord fingering diagrams (the box-with-dots chart from a
songbook) and lays them out as an upper-left legend of every chord in the song.
Takes plain arguments only, no Settings import -- same decoupling convention as
render.py/detect_chords.py."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .chord_shapes import ChordShape, get_chord_shape

ACCENT_COLOR_DEFAULT = (56, 189, 248)
TEXT_COLOR_DEFAULT = (255, 255, 255)
DIM_TEXT_COLOR_DEFAULT = (148, 163, 184)
PANEL_COLOR_DEFAULT = (11, 18, 32)
# Deliberately its OWN setting, separate from the chord bar's translucent
# panel_alpha -- originally fixed near-opaque (owner-reported 2026-09-09:
# fingering charts were hard to read against some backgrounds), then made
# owner-tunable (2026-09-10 request) since the right amount of transparency
# is a taste call, not a one-size-fits-all legibility fix. 235 preserves the
# original near-opaque behavior for anyone who never touches the new slider.
_PANEL_ALPHA = 235

_LABEL_HEIGHT_FRAC = 0.20       # fraction of diagram height reserved for the chord name
_MUTE_OPEN_HEIGHT_FRAC = 0.12   # fraction reserved for the X/O row above the nut
_GRID_SIDE_PAD_FRAC = 0.12      # fraction of diagram width padded on each side of the string grid

_LEGEND_MARGIN_FRAC = 0.03
_LEGEND_GAP_FRAC = 0.012
_LEGEND_MAX_ROW_WIDTH_FRAC = 0.5
_LEGEND_MAX_ROWS = 2            # never wrap further than this, however many chords a song has
_LEGEND_MAX_HEIGHT_FRAC = 0.35  # legend never draws below this fraction of frame height --
                                 # stays clear of both the lyric area and the chord bar below it
_DIAGRAM_WIDTH_FRAC = 0.075
_DIAGRAM_HEIGHT_FRAC = 0.16


def draw_single_chord_diagram(
    shape: ChordShape,
    label: str,
    box_size: tuple[int, int],
    font_path: str,
    *,
    highlighted: bool = False,
    accent_color: tuple[int, int, int] = ACCENT_COLOR_DEFAULT,
    text_color: tuple[int, int, int] = TEXT_COLOR_DEFAULT,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR_DEFAULT,
    panel_color: tuple[int, int, int] = PANEL_COLOR_DEFAULT,
    panel_alpha: int = _PANEL_ALPHA,
) -> Image.Image:
    """One small fingering diagram: chord name above, 6 vertical string lines,
    a nut line (or a base-fret label, if the shape doesn't start at the nut) +
    4 fret lines below it, filled dots with finger numbers for fretted strings,
    'X'/'O' above the nut for muted/open strings. Transparent background outside
    the rounded panel so it composites cleanly onto a video frame."""
    bw, bh = box_size
    img = Image.new("RGBA", box_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    panel_fill = (*panel_color, panel_alpha)
    draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=6, fill=panel_fill)
    if highlighted:
        draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=6, outline=(*accent_color, 255), width=3)

    label_h = int(bh * _LABEL_HEIGHT_FRAC)
    xo_h = int(bh * _MUTE_OPEN_HEIGHT_FRAC)
    grid_y0 = label_h + xo_h
    grid_h = bh - grid_y0 - 4
    side_pad = int(bw * _GRID_SIDE_PAD_FRAC)
    grid_x0 = side_pad
    grid_w = bw - 2 * side_pad

    label_font = ImageFont.truetype(font_path, max(10, label_h - 4))
    small_font = ImageFont.truetype(font_path, max(8, xo_h - 2))
    finger_font = ImageFont.truetype(font_path, max(8, int(grid_h / 4 * 0.5)))

    label_color = accent_color if highlighted else text_color
    label_w = draw.textlength(label, font=label_font)
    draw.text((bw / 2 - label_w / 2, 2), label, font=label_font, fill=(*label_color, 255))

    string_xs = [grid_x0 + i * (grid_w / 5) for i in range(6)]
    fret_ys = [grid_y0 + r * (grid_h / 4) for r in range(5)]

    for r, fy in enumerate(fret_ys):
        line_width = 3 if (r == 0 and shape.base_fret == 1) else 1
        draw.line([(string_xs[0], fy), (string_xs[-1], fy)], fill=(*dim_text_color, 255), width=line_width)
    for sx in string_xs:
        draw.line([(sx, fret_ys[0]), (sx, fret_ys[-1])], fill=(*dim_text_color, 255), width=1)

    if shape.base_fret != 1:
        tag = f"{shape.base_fret}fr"
        tag_w = draw.textlength(tag, font=small_font)
        draw.text(
            (min(bw - tag_w - 2, string_xs[-1] + 4), fret_ys[0] - small_font.size * 0.5),
            tag, font=small_font, fill=(*dim_text_color, 255),
        )

    xo_y = label_h + (xo_h - small_font.size) / 2
    dot_radius = min(grid_w / 5, grid_h / 4) * 0.32
    dot_color = accent_color if highlighted else dim_text_color
    for i in range(6):
        f = shape.frets[i]
        sx = string_xs[i]
        if f == -1:
            draw.text((sx - small_font.size / 4, xo_y), "X", font=small_font, fill=(*text_color, 255))
        elif f == 0:
            draw.text((sx - small_font.size / 4, xo_y), "O", font=small_font, fill=(*text_color, 255))
        elif f > 0:
            row_center_y = (fret_ys[f - 1] + fret_ys[f]) / 2
            draw.ellipse(
                (sx - dot_radius, row_center_y - dot_radius, sx + dot_radius, row_center_y + dot_radius),
                fill=(*dot_color, 255),
            )
            finger = shape.fingers[i]
            if finger:
                ftext = str(finger)
                fw = draw.textlength(ftext, font=finger_font)
                draw.text(
                    (sx - fw / 2, row_center_y - finger_font.size / 2),
                    ftext, font=finger_font, fill=(*panel_color, 255),
                )

    return img


def _legend_layout(n_chords: int, frame_size: tuple[int, int], size_scale: float = 1.0) -> tuple[int, int]:
    """Box (width, height) for each legend diagram. `size_scale` (from the
    owner's Settings slider) sets the preferred/maximum size; this then only
    ever shrinks further -- never grows past that -- by however much is needed
    so up to _LEGEND_MAX_ROWS rows of n_chords diagrams fit within both
    _LEGEND_MAX_ROW_WIDTH_FRAC of the frame width and _LEGEND_MAX_HEIGHT_FRAC
    of the frame height. This is what makes the legend safe for a song with
    many unique chords without knowing that count ahead of time (real
    2026-09-09 case: a 16-chord song wrapped to 4 rows and overlapped both the
    lyrics and the chord bar)."""
    w, h = frame_size
    default_box_w = w * _DIAGRAM_WIDTH_FRAC * size_scale
    default_box_h = h * _DIAGRAM_HEIGHT_FRAC * size_scale

    if n_chords <= 0:
        return int(default_box_w), int(default_box_h)

    gap = w * _LEGEND_GAP_FRAC
    max_row_width = w * _LEGEND_MAX_ROW_WIDTH_FRAC
    max_legend_height = h * _LEGEND_MAX_HEIGHT_FRAC

    default_per_row = max(1, int((max_row_width + gap) // (default_box_w + gap)))
    rows = min(_LEGEND_MAX_ROWS, max(1, -(-n_chords // default_per_row)))  # ceil division
    per_row = -(-n_chords // rows)  # ceil division

    width_scale = (max_row_width - (per_row - 1) * gap) / (per_row * default_box_w)
    height_scale = (max_legend_height - (rows - 1) * gap) / (rows * default_box_h)
    scale = min(1.0, width_scale, height_scale)

    return int(default_box_w * scale), int(default_box_h * scale)


def draw_chord_legend(
    frame: Image.Image,
    chord_labels: list[str],
    current_label: str | None,
    font_path: str,
    *,
    frame_size: tuple[int, int],
    show_chord_legend: bool = True,
    size_scale: float = 1.0,
    accent_color: tuple[int, int, int] = ACCENT_COLOR_DEFAULT,
    text_color: tuple[int, int, int] = TEXT_COLOR_DEFAULT,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR_DEFAULT,
    panel_color: tuple[int, int, int] = PANEL_COLOR_DEFAULT,
    panel_alpha: int = _PANEL_ALPHA,
) -> Image.Image:
    """Composites one draw_single_chord_diagram() per chord_labels entry into
    the upper-left corner of `frame`, left to right, wrapping to further rows
    as needed (see _legend_layout for the sizing/wrapping guarantee). A label
    get_chord_shape() can't resolve is skipped, not an error. Returns a new
    image; `frame` is not mutated (matches draw_scene/draw_chord_bar's own
    copy-on-write style)."""
    if not show_chord_legend or not chord_labels:
        return frame

    resolvable = [(label, get_chord_shape(label)) for label in chord_labels]
    resolvable = [(label, shape) for label, shape in resolvable if shape is not None]
    if not resolvable:
        return frame

    w, h = frame_size
    box_w, box_h = _legend_layout(len(resolvable), frame_size, size_scale)
    gap = int(w * _LEGEND_GAP_FRAC)
    margin_x = int(w * _LEGEND_MARGIN_FRAC)
    margin_y = int(h * _LEGEND_MARGIN_FRAC)
    max_row_width = int(w * _LEGEND_MAX_ROW_WIDTH_FRAC)

    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    row_start_x = margin_x
    x, y = margin_x, margin_y

    for label, shape in resolvable:
        if x != row_start_x and x + box_w > row_start_x + max_row_width:
            x = row_start_x
            y += box_h + gap
        diagram = draw_single_chord_diagram(
            shape, label, (box_w, box_h), font_path,
            highlighted=(label == current_label),
            accent_color=accent_color, text_color=text_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=panel_alpha,
        )
        overlay.alpha_composite(diagram, (x, y))
        x += box_w + gap

    composited = Image.alpha_composite(frame.convert("RGBA"), overlay)
    return composited.convert("RGB")
