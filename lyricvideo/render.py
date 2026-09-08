from __future__ import annotations

import hashlib

from PIL import Image, ImageDraw, ImageFont

from .layout import Scene, SceneLine

FRAME_SIZE = (1920, 1080)

CURRENT_LINE_UNSUNG_COLOR = (255, 255, 255)  # white -- current line, not sung yet
WORD_HIGHLIGHT_BG_COLOR = (46, 204, 113)  # bright green -- box behind already-sung words
WORD_HIGHLIGHT_TEXT_COLOR = (255, 255, 255)  # white text on top of the highlight box
NEXT_LINE_COLOR = (255, 255, 255)  # white
CHORD_ACTIVE_COLOR = (255, 215, 0)  # gold -- steady color once a chord has settled
CHORD_FLASH_COLOR = (255, 255, 255)  # white -- peak brightness the instant a chord hits
CHORD_PENDING_COLOR = (170, 170, 170)  # dim -- shown ahead of time, not yet hit
TEXT_STROKE_COLOR = (0, 0, 0)  # black outline so text reads over any background
TEXT_STROKE_WIDTH = 3

# (start_x, start_y, end_x, end_y, zoom_start, zoom_end) -- x/y are 0..1
# fractions of the available pan range (0.5 = centered). Covers side-to-side,
# up-down, the four diagonals, a push-in, and a pull-out, so consecutive
# images don't all move the same way.
KEN_BURNS_PRESETS: list[tuple[float, float, float, float, float, float]] = [
    (0.0, 0.0, 1.0, 1.0, 1.0, 1.15),  # top-left -> bottom-right
    (1.0, 1.0, 0.0, 0.0, 1.0, 1.15),  # bottom-right -> top-left
    (0.0, 1.0, 1.0, 0.0, 1.0, 1.15),  # bottom-left -> top-right
    (1.0, 0.0, 0.0, 1.0, 1.0, 1.15),  # top-right -> bottom-left
    (0.0, 0.5, 1.0, 0.5, 1.0, 1.15),  # left -> right
    (1.0, 0.5, 0.0, 0.5, 1.0, 1.15),  # right -> left
    (0.5, 0.0, 0.5, 1.0, 1.0, 1.15),  # top -> bottom
    (0.5, 1.0, 0.5, 0.0, 1.0, 1.15),  # bottom -> top
    (0.5, 0.5, 0.5, 0.5, 1.18, 1.0),  # centered pull-out (zoom out)
]


def ken_burns_preset_for_key(image_key: str) -> tuple[float, float, float, float, float, float]:
    """Deterministic per-image pick (stable across a render, and identical for
    a reused cached image) rather than random-per-frame, which would jitter."""
    idx = int(hashlib.sha256(image_key.encode("utf-8")).hexdigest(), 16) % len(KEN_BURNS_PRESETS)
    return KEN_BURNS_PRESETS[idx]


def apply_ken_burns(
    image: Image.Image,
    progress: float,
    start_x: float = 0.0,
    start_y: float = 0.0,
    end_x: float = 1.0,
    end_y: float = 1.0,
    zoom_start: float = 1.0,
    zoom_end: float = 1.15,
) -> Image.Image:
    img = image.resize(FRAME_SIZE)
    zoom = zoom_start + (zoom_end - zoom_start) * progress
    w, h = img.size
    new_w, new_h = max(int(w * zoom), w), max(int(h * zoom), h)
    img = img.resize((new_w, new_h))
    max_dx, max_dy = new_w - w, new_h - h
    frac_x = min(max(start_x + (end_x - start_x) * progress, 0.0), 1.0)
    frac_y = min(max(start_y + (end_y - start_y) * progress, 0.0), 1.0)
    x = int(max_dx * frac_x)
    y = int(max_dy * frac_y)
    return img.crop((x, y, x + w, y + h))


def _lerp_color(
    color_a: tuple[int, int, int], color_b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    t = min(max(t, 0.0), 1.0)
    return tuple(int(a + (b - a) * t) for a, b in zip(color_a, color_b))


def _draw_line_with_chords(draw, y, scene_line: SceneLine, font, chord_font, is_current: bool):
    words = scene_line.words
    if not words:
        return
    space_w = draw.textlength(" ", font=font)
    widths = [draw.textlength(w.text, font=font) for w in words]
    total_w = sum(widths) + space_w * max(len(words) - 1, 0)
    x = (FRAME_SIZE[0] - total_w) / 2
    ascent, descent = font.getmetrics()
    text_height = ascent + descent

    for word, w_width in zip(words, widths):
        if word.chord:
            chord_color = (
                _lerp_color(CHORD_ACTIVE_COLOR, CHORD_FLASH_COLOR, word.chord_flash)
                if word.chord_active
                else CHORD_PENDING_COLOR
            )
            chord_w = draw.textlength(word.chord, font=chord_font)
            chord_x = x + (w_width - chord_w) / 2
            draw.text(
                (chord_x, y - chord_font.size - 10), word.chord, font=chord_font, fill=chord_color,
                stroke_width=TEXT_STROKE_WIDTH, stroke_fill=TEXT_STROKE_COLOR,
            )

        # Karaoke-style sweep: a highlight box appears behind each word the
        # instant it's actually sung (real per-word timing, same data the
        # chord reveal above already uses), moving left to right through the
        # line in sync with the vocals -- not yet-sung words stay plain.
        if is_current and word.word_active:
            pad_x, pad_y = 6, 4
            draw.rectangle(
                [x - pad_x, y - pad_y, x + w_width + pad_x, y + text_height + pad_y],
                fill=WORD_HIGHLIGHT_BG_COLOR,
            )
            text_fill = WORD_HIGHLIGHT_TEXT_COLOR
        else:
            text_fill = CURRENT_LINE_UNSUNG_COLOR if is_current else NEXT_LINE_COLOR

        draw.text(
            (x, y), word.text, font=font, fill=text_fill,
            stroke_width=TEXT_STROKE_WIDTH, stroke_fill=TEXT_STROKE_COLOR,
        )
        x += w_width + space_w


def _draw_instrumental_chord(draw, chord: str, chord_font, flash: float):
    color = _lerp_color(CHORD_ACTIVE_COLOR, CHORD_FLASH_COLOR, flash)
    chord_w = draw.textlength(chord, font=chord_font)
    x = (FRAME_SIZE[0] - chord_w) / 2
    y = FRAME_SIZE[1] // 2 - chord_font.size // 2
    draw.text(
        (x, y), chord, font=chord_font, fill=color,
        stroke_width=TEXT_STROKE_WIDTH, stroke_fill=TEXT_STROKE_COLOR,
    )


def draw_scene(
    scene: Scene,
    background: Image.Image,
    font_path: str,
    font_size: int = 48,
    chord_font_size: int = 40,
    instrumental_chord_font_size: int = 80,
) -> Image.Image:
    frame = background.copy()
    draw = ImageDraw.Draw(frame)
    font = ImageFont.truetype(font_path, font_size)
    chord_font = ImageFont.truetype(font_path, chord_font_size)

    if scene.instrumental_chord:
        # No line is being sung right now -- show the instrumental chord large and
        # centered instead of the (stale) current/next lyric lines.
        instrumental_font = ImageFont.truetype(font_path, instrumental_chord_font_size)
        _draw_instrumental_chord(draw, scene.instrumental_chord, instrumental_font, scene.instrumental_chord_flash)
        return frame

    center_y = FRAME_SIZE[1] // 2
    # Enough room for a line's own word text PLUS the next line's chord label
    # floating above it (chord_font_size + its 10px gap, from
    # _draw_line_with_chords) plus a clear buffer -- otherwise the next
    # line's chords overlap the current line's word descenders/highlight box.
    ascent, descent = font.getmetrics()
    line_height = (ascent + descent) + (chord_font_size + 10) + 20

    for sl in scene.lines:
        # Continuous position, not a fixed per-line step: every line drifts
        # upward by scroll_progress (0->1 across the current line's own real
        # start->end window) so the transition to the next line is a smooth
        # scroll instead of a snap, and lands exactly on the next line's
        # position the instant it becomes current.
        y = center_y + (sl.distance_from_current - scene.scroll_progress) * line_height
        _draw_line_with_chords(draw, y, sl, font, chord_font, sl.is_current)

    return frame
