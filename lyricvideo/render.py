from __future__ import annotations

import hashlib
import threading
from functools import lru_cache
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

from .layout import Scene, SceneLine
from .models import ChordTrack, current_chord_at, next_chord_after

FRAME_SIZE = (1920, 1080)

CURRENT_LINE_UNSUNG_COLOR = (255, 255, 255)  # white -- default text_color
WORD_HIGHLIGHT_BG_COLOR = (46, 204, 113)     # bright green -- box behind already-sung words
WORD_HIGHLIGHT_TEXT_COLOR = (255, 255, 255)  # white text on top of the highlight box
TEXT_STROKE_COLOR = (0, 0, 0)                # black outline so text reads over any background
TEXT_STROKE_WIDTH = 3

# Chord bar geometry at the default FRAME_SIZE (landscape), ported from LyricChord's
# compute_layout()'s landscape branch (frames.py). Kept as module constants for the
# default (zero-plumbing, zero-recompute) path; compute_chord_bar_layout() below
# derives the same shape for any other frame_size.
_CHORD_BAR_MARGIN = 120
_CHORD_BAR_PAD = 20
CHORD_BOX = (
    _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.665),
    FRAME_SIZE[0] - _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.925),
)
_inner_h = CHORD_BOX[3] - CHORD_BOX[1] - 2 * _CHORD_BAR_PAD
NOW_BOX = (
    CHORD_BOX[0] + _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD,
    CHORD_BOX[0] + _CHORD_BAR_PAD + int(FRAME_SIZE[0] * 0.19), CHORD_BOX[3] - _CHORD_BAR_PAD,
)
NEXT_BOX = (
    NOW_BOX[2] + _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD + int(_inner_h * 0.15),
    NOW_BOX[2] + _CHORD_BAR_PAD + int(FRAME_SIZE[0] * 0.13), CHORD_BOX[3] - _CHORD_BAR_PAD - int(_inner_h * 0.15),
)
LANE_BOX = (
    NEXT_BOX[2] + 2 * _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD + int(_inner_h * 0.2),
    CHORD_BOX[2] - _CHORD_BAR_PAD, CHORD_BOX[3] - _CHORD_BAR_PAD - int(_inner_h * 0.2),
)
KEY_BPM_BADGE_XY = (FRAME_SIZE[0] - _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.06))

PANEL_ALPHA_DEFAULT = 150
BOX_FILL = (30, 41, 59, 235)       # NOW/NEXT/lane box fill (not owner-configurable)
ACCENT_COLOR = (56, 189, 248)      # current-chord highlight
DIM_TEXT_COLOR = (148, 163, 184)
LANE_BLOCK_COLOR = (51, 65, 85, 235)
TIMELINE_WINDOW_SECONDS = 12.0
_MIN_LANE_FONT_SIZE = 18  # never shrink a timeline-lane chord label below this
_COUNTDOWN_BOX_SIZE_FRAC = 0.12  # fraction of frame height -- modest, not "gaudy" (owner feedback)

# (start_x, start_y, end_x, end_y, zoom_start, zoom_end) -- x/y are 0..1
# fractions of the available pan range (0.5 = centered).
KEN_BURNS_PRESETS: list[tuple[float, float, float, float, float, float]] = [
    (0.0, 0.0, 1.0, 1.0, 1.0, 1.15),
    (1.0, 1.0, 0.0, 0.0, 1.0, 1.15),
    (0.0, 1.0, 1.0, 0.0, 1.0, 1.15),
    (1.0, 0.0, 0.0, 1.0, 1.0, 1.15),
    (0.0, 0.5, 1.0, 0.5, 1.0, 1.15),
    (1.0, 0.5, 0.0, 0.5, 1.0, 1.15),
    (0.5, 0.0, 0.5, 1.0, 1.0, 1.15),
    (0.5, 1.0, 0.5, 0.0, 1.0, 1.15),
    (0.5, 0.5, 0.5, 0.5, 1.18, 1.0),
]


_font_cache_local = threading.local()


def load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    """ImageFont.truetype(font_path, size), cached per (path, size) -- PER
    THREAD, since FreeType face objects aren't safe to share between the
    GUI thread (the live Settings preview) and a pipeline render running on
    a worker thread at the same time. Every draw_* helper here and in
    chord_diagram.py used to call ImageFont.truetype directly, re-reading
    the font file from disk and rebuilding the face for every font size on
    EVERY frame -- around 25 loads per frame with a typical chord legend,
    i.e. minutes of pure font loading over a full-length render (found by
    code review, 2026-09-14). The set of distinct sizes is tiny, so the
    cache stays tiny."""
    cache = getattr(_font_cache_local, "fonts", None)
    if cache is None:
        cache = _font_cache_local.fonts = {}
    key = (font_path, size)
    font = cache.get(key)
    if font is None:
        font = cache[key] = ImageFont.truetype(font_path, size)
    return font


def overlay_patch(overlay: Image.Image) -> tuple[Image.Image, tuple[int, int]] | None:
    """(the non-transparent part of an RGBA overlay, its top-left in the overlay), or None if nothing was drawn.
    The patch is what composite_patch() needs; cached patches must never be mutated by a caller."""
    bbox = overlay.getbbox()  # alpha channel only: a fully transparent pixel changes nothing when composited
    if bbox is None:
        return None
    return overlay.crop(bbox), (bbox[0], bbox[1])


def composite_patch(frame: Image.Image, *patches: tuple[Image.Image, tuple[int, int]] | None) -> Image.Image:
    """Copy-on-write alpha composite of overlay_patch() results onto `frame`, in order, returning an RGB image.

    Pixel-identical to the old per-frame `Image.alpha_composite(frame.convert("RGBA"), full_frame_overlay)
    .convert("RGB")` -- a fully transparent overlay pixel leaves the frame pixel exactly as it was -- but only each
    patch's own box is converted and composited. Four whole-frame RGB->RGBA->RGB round trips per frame (chord bar,
    legend, capo badge, support overlay) were ~25 ms of a ~55 ms 1080p frame (issue #7 review). Patches passed
    together must not overlap each other (they would otherwise stack, where one shared overlay would not)."""
    if frame.mode != "RGB":
        # Never hit by the renderer (its frames are RGB); keep the exact old semantics for anything else.
        full = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        for patch in patches:
            if patch is not None:
                full.alpha_composite(patch[0], patch[1])
        return Image.alpha_composite(frame.convert("RGBA"), full).convert("RGB")
    out = frame.copy()
    for patch in patches:
        if patch is None:
            continue
        image, (x, y) = patch
        left, top = max(0, x), max(0, y)
        right, bottom = min(frame.width, x + image.width), min(frame.height, y + image.height)
        if right <= left or bottom <= top:
            continue
        region = out.crop((left, top, right, bottom)).convert("RGBA")
        region.alpha_composite(image, (0, 0), (left - x, top - y, right - x, bottom - y))
        out.paste(region.convert("RGB"), (left, top))
    return out


_OVERLAY_CACHE_SIZE = 16


def clear_overlay_caches() -> None:
    """Drops the cached static overlay patches (Key/BPM badge, support overlay, countdown). assemble_video calls
    this when a render ends so a finished render's patches never stay in memory for the rest of the session."""
    _key_bpm_badge_patch.cache_clear()
    _support_overlay_patch.cache_clear()
    _countdown_patch.cache_clear()
    _like_subscribe_patch.cache_clear()


def crossfade_backgrounds(prev: Image.Image, current: Image.Image, blend: float) -> Image.Image:
    """Dissolves from `prev` to `current` -- blend=0.0 is fully prev, 1.0 is
    fully current. Both images must already be the same size (the caller
    applies Ken Burns to each before calling this)."""
    if blend >= 1.0:
        return current
    if blend <= 0.0:
        return prev
    return Image.blend(prev.convert("RGB"), current.convert("RGB"), blend)


def ken_burns_preset_for_key(image_key: str) -> tuple[float, float, float, float, float, float]:
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
    frame_size: tuple[int, int] = FRAME_SIZE,
) -> Image.Image:
    # assemble_video caches its backgrounds already frame-sized, so this
    # first resample is skipped on the hot per-frame path; any other caller
    # (the Settings preview, tests) still gets the same normalization.
    img = image if image.size == tuple(frame_size) else image.resize(frame_size)
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


_MAX_LINE_WIDTH_FRAC = 0.92  # a lyric line may use at most this fraction of the frame width
_ROW_GAP = 4                 # extra pixels between wrapped rows of one long lyric line
_LINE_SPACING_PAD = 20       # extra breathing room between the current/next line slots
                              # (matches the original single-row line_height's "+20")


def _split_line_into_rows(words: list, draw, font, max_width: float) -> list:
    """Groups `words` into one or more rows that each fit within max_width.
    Never shrinks the font -- a long line wraps onto more rows instead.
    Prefers splitting after a word ending in a comma (a real vocal pause);
    falls back to plain word-by-word wrapping for a comma-free clause (or
    comma-free line) that's still too wide on its own. A single word wider
    than max_width is never split or dropped -- it becomes its own row."""
    space_w = draw.textlength(" ", font=font)

    def width_of(ws: list) -> float:
        widths = [draw.textlength(w.text, font=font) for w in ws]
        return sum(widths) + space_w * max(len(ws) - 1, 0)

    if width_of(words) <= max_width:
        return [words]

    # Break into comma-delimited chunks -- the natural, preferred wrap points.
    chunks = []
    current: list = []
    for w in words:
        current.append(w)
        if w.text.endswith(","):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    # Greedily pack whole chunks onto each row; a chunk still too wide on its
    # own (no internal commas) falls back to word-by-word wrapping.
    rows: list = []
    row: list = []
    for chunk in chunks:
        if row and width_of(row + chunk) > max_width:
            rows.append(row)
            row = []
        if width_of(chunk) > max_width:
            if row:
                rows.append(row)
                row = []
            word_row: list = []
            for w in chunk:
                if word_row and width_of(word_row + [w]) > max_width:
                    rows.append(word_row)
                    word_row = [w]
                else:
                    word_row.append(w)
            if word_row:
                rows.append(word_row)
        else:
            row = row + chunk
    if row:
        rows.append(row)

    return rows


def _rows_and_block_height(words: list, draw, font, max_width: float) -> tuple:
    """Wraps `words` (see _split_line_into_rows) and reports the total pixel
    height that block of rows will occupy once drawn. Shared by
    _draw_line_words (to center its stacked rows) and draw_scene (to space
    the current/next line slots far enough apart that a wrapped line's extra
    rows can never collide with the line below it -- real bug, 2026-09-09:
    the lyric-wrap fix let a long line take multiple rows without ever
    widening the fixed single-row gap between it and the next line preview,
    so the two rendered on top of each other)."""
    if not words:
        return [], 0.0
    rows = _split_line_into_rows(words, draw, font, max_width)
    ascent, descent = font.getmetrics()
    text_height = ascent + descent
    row_height = text_height + _ROW_GAP
    total_height = row_height * len(rows) - _ROW_GAP
    return rows, total_height


def _draw_line_words(draw, y, scene_line: SceneLine, font, is_current: bool, text_color, frame_width: int) -> None:
    words = scene_line.words
    if not words:
        return

    max_width = frame_width * _MAX_LINE_WIDTH_FRAC
    rows, total_height = _rows_and_block_height(words, draw, font, max_width)

    space_w = draw.textlength(" ", font=font)
    ascent, descent = font.getmetrics()
    text_height = ascent + descent
    row_height = text_height + _ROW_GAP
    start_y = y - (total_height - text_height) / 2  # center the stacked rows around the original y

    for row_idx, row in enumerate(rows):
        row_y = start_y + row_idx * row_height
        widths = [draw.textlength(w.text, font=font) for w in row]
        total_w = sum(widths) + space_w * max(len(row) - 1, 0)
        x = (frame_width - total_w) / 2

        for word, w_width in zip(row, widths):
            # Karaoke-style sweep: a highlight box appears behind each word the
            # instant it's actually sung (real per-word timing from forced
            # alignment), moving left to right through the line in sync with the
            # vocals -- not yet-sung words stay plain. Not owner-configurable
            # (already-tuned mechanism, see the 2026-09-09 GUI-design spec).
            if is_current and word.word_active:
                pad_x, pad_y = 6, 4
                draw.rectangle(
                    [x - pad_x, row_y - pad_y, x + w_width + pad_x, row_y + text_height + pad_y],
                    fill=WORD_HIGHLIGHT_BG_COLOR,
                )
                text_fill = WORD_HIGHLIGHT_TEXT_COLOR
            else:
                text_fill = text_color

            draw.text(
                (x, row_y), word.text, font=font, fill=text_fill,
                stroke_width=TEXT_STROKE_WIDTH, stroke_fill=TEXT_STROKE_COLOR,
            )
            x += w_width + space_w


def draw_scene(
    scene: Scene,
    background: Image.Image,
    font_path: str,
    font_size: int = 48,
    text_color: tuple[int, int, int] = CURRENT_LINE_UNSUNG_COLOR,
    frame_size: tuple[int, int] = FRAME_SIZE,
) -> Image.Image:
    frame = background.copy()
    draw = ImageDraw.Draw(frame)
    font = load_font(font_path, font_size)

    center_y = frame_size[1] // 2
    ascent, descent = font.getmetrics()
    single_row_height = ascent + descent
    max_width = frame_size[0] * _MAX_LINE_WIDTH_FRAC

    # The gap between the current-line slot and the next-line slot must widen
    # when either one wraps onto multiple rows (window=1 in this codebase, so
    # there are never more than these two lines at once) -- otherwise a long
    # wrapped line's lower rows collide with the line below it. Reduces to
    # exactly the original fixed single-row spacing when neither line wraps.
    block_heights = {
        sl.distance_from_current: _rows_and_block_height(sl.words, draw, font, max_width)[1]
        for sl in scene.lines
    }
    current_height = block_heights.get(0, single_row_height)
    next_height = block_heights.get(1, single_row_height)
    step = (current_height + next_height) / 2 + _LINE_SPACING_PAD

    for sl in scene.lines:
        # Continuous position, not a fixed per-line step: every line drifts
        # upward by scroll_progress (0->1 across the current line's own real
        # start->end window) so the transition to the next line is a smooth
        # scroll instead of a snap.
        y = center_y + (sl.distance_from_current - scene.scroll_progress) * step
        _draw_line_words(draw, y, sl, font, sl.is_current, text_color, frame_size[0])

    return frame


def _display_chord_label(label: str) -> str:
    return "N.C." if label == "N" else label


def compute_chord_bar_layout(frame_size: tuple[int, int]) -> dict[str, tuple[int, int, int, int]]:
    """Chord-bar box geometry (chord_box/now_box/next_box/lane_box/badge_xy) for any
    frame_size, landscape-only (this program always renders landscape). At the
    default FRAME_SIZE this reproduces CHORD_BOX/NOW_BOX/NEXT_BOX/LANE_BOX exactly."""
    w, h = frame_size
    margin, pad = _CHORD_BAR_MARGIN, _CHORD_BAR_PAD
    chord_box = (margin, int(h * 0.665), w - margin, int(h * 0.925))
    inner_h = chord_box[3] - chord_box[1] - 2 * pad
    now_box = (
        chord_box[0] + pad, chord_box[1] + pad,
        chord_box[0] + pad + int(w * 0.19), chord_box[3] - pad,
    )
    next_box = (
        now_box[2] + pad, chord_box[1] + pad + int(inner_h * 0.15),
        now_box[2] + pad + int(w * 0.13), chord_box[3] - pad - int(inner_h * 0.15),
    )
    lane_box = (
        next_box[2] + 2 * pad, chord_box[1] + pad + int(inner_h * 0.2),
        chord_box[2] - pad, chord_box[3] - pad - int(inner_h * 0.2),
    )
    badge_xy = (w - margin, int(h * 0.06))
    return {"chord_box": chord_box, "now_box": now_box, "next_box": next_box,
            "lane_box": lane_box, "badge_xy": badge_xy}


def _lane_label_font(label: str, draw, base_font, font_path: str, available_width: float, min_size: int):
    """The font to draw one timeline-lane chord label with: base_font as-is
    if the label already fits available_width, otherwise shrunk down
    proportionally -- but never below min_size. Real owner-reported issue,
    2026-09-09: a short chord's box was too narrow for its label at the
    default size, so the label was skipped entirely (a blank colored box).
    Shrinking still can't guarantee a fit for a razor-thin segment -- see
    _lane_label_visible, which the caller uses to decide whether to draw
    this font's label at all."""
    label_w = draw.textlength(label, font=base_font)
    if label_w <= 0 or label_w + 16 <= available_width:
        return base_font
    scale = max(available_width - 4, 1.0) / label_w
    shrunk_size = max(min_size, int(base_font.size * scale))
    if shrunk_size >= base_font.size:
        return base_font
    return load_font(font_path, shrunk_size)


def _lane_label_visible(label: str, draw, font, available_width: float) -> bool:
    """Whether a timeline-lane label actually fits its own segment box at
    `font` (already shrunk to the floor size by _lane_label_font). Real bug,
    2026-09-10 ("Wish You Were Here"): a spurious 0.51-second chord segment
    was too narrow for even the floor-size label, so the label overflowed
    into the NEIGHBORING segment's own label -- garbling both together into
    one unreadable smear. The 2026-09-09 fix's "let it overflow rather than
    disappear" tradeoff was meant for a single moderately-narrow segment; it
    doesn't hold up once real chord-detection noise produces several
    razor-thin segments in a row. The segment's own colored block is still
    drawn either way -- a chord change stays visible -- only the label text
    is skipped when it can't actually fit."""
    return draw.textlength(label, font=font) <= available_width


def draw_chord_bar(
    frame: Image.Image,
    chord_track: ChordTrack,
    t: float,
    font_path: str,
    *,
    frame_size: tuple[int, int] = FRAME_SIZE,
    accent_color: tuple[int, int, int] = ACCENT_COLOR,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR,
    panel_color: tuple[int, int, int] = (11, 18, 32),
    panel_alpha: int = PANEL_ALPHA_DEFAULT,
    chord_now_size: int = 64,
    chord_next_size: int = 32,
    show_chord_timeline: bool = True,
    show_key_bpm: bool = True,
    timeline_window_sec: float = TIMELINE_WINDOW_SECONDS,
    key_label: str | None = None,
) -> Image.Image:
    """Composites the NOW/NEXT/timeline chord bar and the Key/BPM badge onto
    `frame`, ported from LyricChord's FrameComposer._draw_chords + its header
    badge. Independent of which lyric line/word is on screen -- looked up
    directly from chord_track by playback time t. Returns a new image; `frame`
    is not mutated (matches draw_scene's own copy-on-write style)."""
    layout = CHORD_BOX_LAYOUT_DEFAULT if frame_size == FRAME_SIZE else compute_chord_bar_layout(frame_size)
    chord_box, now_box, next_box, lane_box, badge_xy = (
        layout["chord_box"], layout["now_box"], layout["next_box"], layout["lane_box"], layout["badge_xy"],
    )

    label_font = load_font(font_path, 20)
    now_font = load_font(font_path, chord_now_size)
    next_font = load_font(font_path, chord_next_size)
    small_font = load_font(font_path, _KEY_BPM_BADGE_FONT_SIZE)
    lane_font = load_font(font_path, 30)

    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    panel_fill = (*panel_color, panel_alpha)
    draw.rounded_rectangle(chord_box, radius=24, fill=panel_fill)
    draw.rounded_rectangle(now_box, radius=18, fill=BOX_FILL)
    draw.rounded_rectangle(next_box, radius=16, fill=BOX_FILL)
    if show_chord_timeline:
        draw.rounded_rectangle(lane_box, radius=12, fill=BOX_FILL)

    draw.text((now_box[0] + 16, now_box[1] + 10), "NOW", font=label_font, fill=dim_text_color)
    draw.text((next_box[0] + 14, next_box[1] + 8), "NEXT", font=label_font, fill=dim_text_color)

    current = current_chord_at(chord_track, t)
    next_event = next_chord_after(chord_track, t)

    now_label = _display_chord_label(current.label) if current else "—"
    now_color = accent_color if current else dim_text_color
    now_w = draw.textlength(now_label, font=now_font)
    draw.text(
        ((now_box[0] + now_box[2]) / 2 - now_w / 2, (now_box[1] + now_box[3]) / 2 - chord_now_size / 2),
        now_label, font=now_font, fill=now_color,
    )

    if next_event is not None:
        next_label = _display_chord_label(next_event.label)
        next_w = draw.textlength(next_label, font=next_font)
        draw.text(
            ((next_box[0] + next_box[2]) / 2 - next_w / 2, next_box[1] + 34),
            next_label, font=next_font, fill=(248, 250, 252, 255),
        )
        eta = f"in {max(0.0, next_event.start - t):.1f}s"
        eta_w = draw.textlength(eta, font=small_font)
        draw.text(
            ((next_box[0] + next_box[2]) / 2 - eta_w / 2, next_box[3] - 30),
            eta, font=small_font, fill=dim_text_color,
        )

    if show_chord_timeline:
        lx0, ly0, lx1, ly1 = lane_box
        pps = (lx1 - lx0) / timeline_window_sec
        for event in chord_track.events:
            if event.start >= t + timeline_window_sec:
                break
            if event.end <= t:
                continue
            bx0 = lx0 + max(0.0, event.start - t) * pps
            bx1 = lx0 + min(timeline_window_sec, event.end - t) * pps
            if bx1 - bx0 < 2:
                continue
            is_current = current is not None and event is current
            fill = (*accent_color, 255) if is_current else LANE_BLOCK_COLOR
            draw.rounded_rectangle((int(bx0) + 1, ly0 + 6, int(bx1) - 1, ly1 - 6), radius=10, fill=fill)
            label = _display_chord_label(event.label)
            text_color = (11, 18, 32, 255) if is_current else (248, 250, 252, 255)
            seg_font = _lane_label_font(label, draw, lane_font, font_path, bx1 - bx0, _MIN_LANE_FONT_SIZE)
            if _lane_label_visible(label, draw, seg_font, bx1 - bx0):
                label_w = draw.textlength(label, font=seg_font)
                draw.text(
                    ((bx0 + bx1) / 2 - label_w / 2, (ly0 + ly1) / 2 - seg_font.size / 2),
                    label, font=seg_font, fill=text_color,
                )

    # `key_label` (owner, 2026-09-23): an EASY CHORD (capo) video's chord_track.key is the SHAPE key it frets,
    # but a capo never changes the song's real key -- that video passes its original key here instead.
    key = key_label or chord_track.key
    badge_patch = None
    if show_key_bpm and (key or chord_track.bpm):
        parts = []
        if key:
            parts.append(f"Key: {key}")
        if chord_track.bpm:
            parts.append(f"{int(round(chord_track.bpm))} BPM")
        # The badge sits far above the bar (upper right), never overlapping it, and never changes during a song,
        # so it is its own cached patch instead of widening the bar's per-frame composite to most of the frame.
        badge_patch = _key_bpm_badge_patch(
            "   ·   ".join(parts), font_path, frame.size, tuple(badge_xy), tuple(panel_fill), tuple(accent_color),
        )

    return composite_patch(frame, overlay_patch(overlay), badge_patch)


_KEY_BPM_BADGE_FONT_SIZE = 24
_KEY_BPM_BADGE_PAD_X = 14
_KEY_BPM_BADGE_PAD_Y = 8


def key_bpm_badge_bottom(frame_size: tuple[int, int]) -> int:
    """Lowest pixel row of the Key/BPM badge's panel at this frame size (draw_chord_bar's own geometry)."""
    layout = CHORD_BOX_LAYOUT_DEFAULT if frame_size == FRAME_SIZE else compute_chord_bar_layout(frame_size)
    return layout["badge_xy"][1] + _KEY_BPM_BADGE_FONT_SIZE + _KEY_BPM_BADGE_PAD_Y


@lru_cache(maxsize=_OVERLAY_CACHE_SIZE)
def _key_bpm_badge_patch(
    badge: str,
    font_path: str,
    canvas_size: tuple[int, int],
    badge_xy: tuple[int, int],
    panel_fill: tuple[int, int, int, int],
    accent_color: tuple[int, int, int],
) -> tuple[Image.Image, tuple[int, int]] | None:
    overlay = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    small_font = load_font(font_path, _KEY_BPM_BADGE_FONT_SIZE)
    bx, by = badge_xy
    badge_w = draw.textlength(badge, font=small_font)
    # Real owner complaint, 2026-09-23: "too faint... needs to be just a little bit
    # brighter" -- unlike every other piece of chord-bar text, this badge was drawn
    # straight onto the video frame with no panel behind it, so it could wash out
    # against a bright background image. Same panel_fill/rounded-rectangle language
    # as the rest of this bar restores guaranteed contrast regardless of what's
    # playing behind it.
    pad_x, pad_y = _KEY_BPM_BADGE_PAD_X, _KEY_BPM_BADGE_PAD_Y
    badge_box = (bx - badge_w - pad_x, by - pad_y, bx + pad_x, by + small_font.size + pad_y)
    draw.rounded_rectangle(badge_box, radius=10, fill=panel_fill)
    draw.text((bx - badge_w, by), badge, font=small_font, fill=(*accent_color, 255))
    return overlay_patch(overlay)


def draw_countdown(
    frame: Image.Image,
    seconds_remaining: int,
    font_path: str,
    *,
    frame_size: tuple[int, int] = FRAME_SIZE,
    accent_color: tuple[int, int, int] = ACCENT_COLOR,
) -> Image.Image:
    """Composites a small rounded countdown-number panel centered on `frame`
    -- same BOX_FILL/rounded-rectangle/accent-color language as the chord
    bar's own NOW/NEXT boxes, so the lead-in doesn't look like a different
    visual style bolted onto the video (owner feedback, 2026-09-10: keep it
    modest, not "gaudy"). Returns a new image; `frame` is not mutated
    (matches draw_scene/draw_chord_bar's own copy-on-write style)."""
    patch = _countdown_patch(seconds_remaining, font_path, frame.size, tuple(frame_size), tuple(accent_color))
    return composite_patch(frame, patch)


@lru_cache(maxsize=_OVERLAY_CACHE_SIZE)
def _countdown_patch(
    seconds_remaining: int,
    font_path: str,
    canvas_size: tuple[int, int],
    frame_size: tuple[int, int],
    accent_color: tuple[int, int, int],
) -> tuple[Image.Image, tuple[int, int]] | None:
    w, h = frame_size
    overlay = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    box_side = int(h * _COUNTDOWN_BOX_SIZE_FRAC)
    cx, cy = w // 2, h // 2
    box = (cx - box_side // 2, cy - box_side // 2, cx + box_side // 2, cy + box_side // 2)
    draw.rounded_rectangle(box, radius=int(box_side * 0.18), fill=BOX_FILL, outline=(*accent_color, 255), width=3)

    font = load_font(font_path, int(box_side * 0.5))
    label = str(seconds_remaining)
    label_w = draw.textlength(label, font=font)
    draw.text((cx - label_w / 2, cy - box_side * 0.28), label, font=font, fill=(*accent_color, 255))
    return overlay_patch(overlay)


_SUPPORT_OVERLAY_MARGIN_X = 40  # from the right edge
_SUPPORT_OVERLAY_MARGIN_TOP = 110  # the top at 720p/1080p -- clears the Key/BPM badge (drawn by
                                    # draw_chord_bar, anchored at frame height * 0.06); taller frames
                                    # move it further down, see _support_overlay_top
_SUPPORT_OVERLAY_BADGE_GAP = 14  # clear rows between the badge's panel and this one (1080p's own gap)
_SUPPORT_OVERLAY_BASE_FONT_SIZE = 28
_SUPPORT_OVERLAY_PAD_X = 16
_SUPPORT_OVERLAY_PAD_Y = 10
_SUPPORT_OVERLAY_ALPHA = 180  # semi-transparent -- a reminder, not a competing focal point


def draw_support_overlay(
    frame: Image.Image,
    text: str,
    font_path: str,
    *,
    frame_size: tuple[int, int] = FRAME_SIZE,
    accent_color: tuple[int, int, int] = ACCENT_COLOR,
    panel_color: tuple[int, int, int] = (11, 18, 32),
    scale: float = 1.0,
) -> Image.Image:
    """Small, unobtrusive upper-RIGHT watermark pointing viewers at an
    external support link (owner request, 2026-09-11) -- same rounded-box/
    accent-color language as the chord bar and countdown panel, semi-
    transparent so it never competes with the real content. Right-aligned
    and positioned below the Key/BPM badge (also upper-right, drawn by
    draw_chord_bar) -- NOT upper-left, which is where the chord fingering
    legend lives (real bug caught by the owner asking to see a render before
    trusting it: the original upper-left placement directly covered the
    first two chord diagrams' labels). NOT clickable -- no region of a
    rendered video frame can be; this is purely a visual pointer to the
    real, clickable link in the description. A no-op (`frame` returned
    untouched) when `text` is blank, so a disabled overlay costs nothing.
    Copy-on-write, matching every other draw_* here."""
    if not text.strip():
        return frame
    patch = _support_overlay_patch(
        text, font_path, frame.size, tuple(frame_size), tuple(accent_color), tuple(panel_color), scale,
    )
    return composite_patch(frame, patch)


def _support_overlay_top(frame_size: tuple[int, int]) -> int:
    """The overlay's top row: the old fixed 110 px wherever that already clears the Key/BPM badge (720p/1080p,
    unchanged), else just below the badge. The badge is anchored at 6% of the frame height with a fixed-size
    font, so at 1440p its panel reached row 118 and the fixed 110 px top drew over it."""
    return max(_SUPPORT_OVERLAY_MARGIN_TOP, key_bpm_badge_bottom(frame_size) + _SUPPORT_OVERLAY_BADGE_GAP)


@lru_cache(maxsize=_OVERLAY_CACHE_SIZE)
def _support_overlay_patch(
    text: str,
    font_path: str,
    canvas_size: tuple[int, int],
    frame_size: tuple[int, int],
    accent_color: tuple[int, int, int],
    panel_color: tuple[int, int, int],
    scale: float,
) -> tuple[Image.Image, tuple[int, int]] | None:
    font_size = max(1, int(_SUPPORT_OVERLAY_BASE_FONT_SIZE * scale))
    pad_x = int(_SUPPORT_OVERLAY_PAD_X * scale)
    pad_y = int(_SUPPORT_OVERLAY_PAD_Y * scale)
    margin_x = int(_SUPPORT_OVERLAY_MARGIN_X * scale)
    margin_top = _support_overlay_top(frame_size)

    overlay = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(font_path, font_size)

    text_w = draw.textlength(text, font=font)
    ascent, descent = font.getmetrics()
    text_h = ascent + descent

    box_right = frame_size[0] - margin_x
    box_left = box_right - (text_w + 2 * pad_x)
    box_top = margin_top
    box_bottom = margin_top + text_h + 2 * pad_y

    box = (box_left, box_top, box_right, box_bottom)
    draw.rounded_rectangle(box, radius=int(pad_y * 1.2), fill=(*panel_color, _SUPPORT_OVERLAY_ALPHA))
    draw.text((box_left + pad_x, box_top + pad_y), text, font=font, fill=(*accent_color, 255))
    return overlay_patch(overlay)


# --- Like / Subscribe call to action (owner, 2026-10-04) ---------------------------------------------------------
# Like pill + red Subscribe button + bell, the standard look, with a white cursor that clicks Subscribe (-> SUBSCRIBED,
# the bell rings) and a short benefit line under it. Shown in the count-in (donate label's spot) and the last N seconds
# (under the donate label). Burned in, so -- like the donate label -- not clickable.
_LS_BASE_H = 64
_LS_GAP = 12
_LS_LIKE_W, _LS_SUB_W, _LS_BELL_W = 150, 250, 70
_LS_FONT = 30
_LS_BENEFIT_FONT = 26
_LS_RED = (204, 0, 0)
_LS_RED_PRESSED = (160, 0, 0)
_LS_GREY = (150, 150, 150)
_LS_CLICK_SECONDS = 4.0      # the click animation always plays over this long; a shorter window (a fast count-in) is sped up


class LikeSubscribeState(NamedTuple):
    alpha: float                 # 0..1 fade of the whole call to action
    cursor: float | None         # 0..1 along the cursor's path to the button, None = no cursor yet
    pressed: bool
    subscribed: bool
    ring: bool                   # the bell's rays are showing


def like_subscribe_state(t: float, window: float) -> LikeSubscribeState:
    """Where the animation stands `t` seconds into a `window`-second showing: fade in (0.4 s), cursor glides to Subscribe
    (0.3-1.0 s), press (1.0-1.25 s), SUBSCRIBED + the bell rings for a second, hold, fade out. Pure, for tests."""
    if window <= 0 or t < 0 or t >= window:
        return LikeSubscribeState(0.0, None, False, False, False)
    speed = max(1.0, _LS_CLICK_SECONDS / window)
    u, total = t * speed, window * speed
    fade = min(0.4, total * 0.12)
    alpha = max(0.0, min(1.0, u / fade, (total - u) / fade))
    cursor = None
    if u >= 0.3:
        x = min(1.0, (u - 0.3) / 0.7)
        cursor = 1.0 - (1.0 - x) ** 3
    subscribed = u >= 1.25
    ring = subscribed and u < 2.45 and int((u - 1.25) / 0.1) % 2 == 0
    return LikeSubscribeState(alpha, cursor, 1.0 <= u < 1.25, subscribed, ring)


def draw_like_subscribe(
    frame: Image.Image,
    t: float,
    window: float,
    font_path: str,
    *,
    frame_size: tuple[int, int] = FRAME_SIZE,
    benefit_text: str = "",
    panel_color: tuple[int, int, int] = (11, 18, 32),
    scale: float = 1.0,
    support_text: str = "",
) -> Image.Image:
    """The Like / Subscribe call to action `t` seconds into a `window`-second showing, upper-right, in the donate label's
    column: at the label's own spot, or -- when `support_text` is not blank (the label is up) -- directly under it.
    Returns `frame` untouched outside the window. Copy-on-write like every draw_* here."""
    state = like_subscribe_state(t, window)
    if state.alpha <= 0.0:
        return frame
    top = _support_overlay_top(tuple(frame_size))
    if support_text.strip():
        font = load_font(font_path, max(1, int(_SUPPORT_OVERLAY_BASE_FONT_SIZE * scale)))
        ascent, descent = font.getmetrics()
        top += ascent + descent + int(2 * _SUPPORT_OVERLAY_PAD_Y * scale) + int(10 * scale)
    patch = _like_subscribe_patch(
        round(state.alpha * 16), None if state.cursor is None else round(state.cursor * 24), state.pressed,
        state.subscribed, state.ring, benefit_text.strip(), font_path, frame.size, tuple(frame_size),
        tuple(panel_color), scale, top,
    )
    return composite_patch(frame, patch)


def _draw_thumb(draw, x, y, s, fill) -> None:
    draw.rounded_rectangle([x, y + s * .42, x + s * .22, y + s * .95], radius=max(1, s * .04), fill=fill)
    draw.polygon([(x + s * .30, y + s * .46), (x + s * .48, y + s * .10), (x + s * .62, y + s * .10),
                  (x + s * .58, y + s * .38), (x + s * .95, y + s * .38), (x + s * .90, y + s * .92),
                  (x + s * .30, y + s * .92)], fill=fill)


def _draw_bell(draw, x, y, s, fill, ring: bool) -> None:
    draw.pieslice([x + s * .15, y + s * .05, x + s * .85, y + s * .80], 180, 360, fill=fill)
    draw.rectangle([x + s * .15, y + s * .42, x + s * .85, y + s * .78], fill=fill)
    draw.rounded_rectangle([x + s * .05, y + s * .74, x + s * .95, y + s * .84], radius=max(1, s * .05), fill=fill)
    draw.ellipse([x + s * .40, y + s * .86, x + s * .60, y + s * 1.0], fill=fill)
    if ring:
        width = max(2, int(s * .08))
        for side in (-1, 1):
            for k, r in enumerate((.18, .30)):
                cx = x + s * .5 + side * s * (.62 + k * .2)
                draw.line([(cx, y + s * (.35 - r)), (cx + side * s * .10, y + s * .35)], fill=fill, width=width)
                draw.line([(cx + side * s * .10, y + s * .35), (cx, y + s * (.35 + r))], fill=fill, width=width)


@lru_cache(maxsize=_OVERLAY_CACHE_SIZE)
def _like_subscribe_patch(
    alpha_q: int, cursor_q: int | None, pressed: bool, subscribed: bool, ring: bool, benefit_text: str,
    font_path: str, canvas_size: tuple[int, int], frame_size: tuple[int, int], panel_color: tuple[int, int, int],
    scale: float, top: int,
) -> tuple[Image.Image, tuple[int, int]] | None:
    sc = lambda v: max(1, int(v * scale))
    h, gap = sc(_LS_BASE_H), sc(_LS_GAP)
    w_like, w_sub, w_bell = sc(_LS_LIKE_W), sc(_LS_SUB_W), sc(_LS_BELL_W)
    right = frame_size[0] - int(_SUPPORT_OVERLAY_MARGIN_X * scale)
    left = right - (w_like + w_sub + w_bell + 2 * gap)
    overlay = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    white, panel = (255, 255, 255, 255), (*panel_color, 200)
    font = load_font(font_path, sc(_LS_FONT))
    radius = sc(12)

    draw.rounded_rectangle([left, top, left + w_like, top + h], radius=radius, fill=panel, outline=(255, 255, 255, 200), width=2)
    _draw_thumb(draw, left + sc(16), top + sc(12), sc(40), white)
    draw.text((left + sc(68), top + h / 2), "Like", font=font, fill=white, anchor="lm")

    x2 = left + w_like + gap
    fill = _LS_GREY if subscribed else (_LS_RED_PRESSED if pressed else _LS_RED)
    draw.rounded_rectangle([x2, top, x2 + w_sub, top + h], radius=radius, fill=(*fill, 235))
    label_font = load_font(font_path, sc(_LS_FONT - 5)) if subscribed else font
    draw.text((x2 + w_sub / 2, top + h / 2), "SUBSCRIBED" if subscribed else "Subscribe", font=label_font, fill=white, anchor="mm")

    x3 = x2 + w_sub + gap
    draw.rounded_rectangle([x3, top, x3 + w_bell, top + h], radius=radius, fill=panel, outline=(255, 255, 255, 200), width=2)
    _draw_bell(draw, x3 + sc(17), top + sc(12), sc(36), white, ring)

    if benefit_text:
        bfont = load_font(font_path, sc(_LS_BENEFIT_FONT))
        text_w = draw.textlength(benefit_text, font=bfont)
        pad_x, pad_y = sc(18), sc(10)
        asc, desc = bfont.getmetrics()
        box_top = top + h + gap
        draw.rounded_rectangle([right - text_w - 2 * pad_x, box_top, right, box_top + asc + desc + 2 * pad_y],
                               radius=sc(10), fill=(*panel_color, 180))
        draw.text((right - pad_x - text_w, box_top + pad_y), benefit_text, font=bfont, fill=white)

    if cursor_q is not None:
        progress = cursor_q / 24
        end_x, end_y = x2 + w_sub * 0.91, top + h / 2 - sc(10)     # right end of the button: clear of the label
        cx = end_x + (1 - progress) * sc(170)
        cy = end_y + (1 - progress) * sc(60)
        pts = [(0, 0), (0, 34), (9, 26), (16, 40), (22, 37), (15, 24), (27, 24)]
        draw.polygon([(cx + px * scale, cy + py * scale) for px, py in pts], fill=white, outline=(0, 0, 0, 255))

    if alpha_q < 16:
        overlay.putalpha(overlay.getchannel("A").point(lambda v: int(v * alpha_q / 16)))
    return overlay_patch(overlay)


CHORD_BOX_LAYOUT_DEFAULT = {
    "chord_box": CHORD_BOX, "now_box": NOW_BOX, "next_box": NEXT_BOX,
    "lane_box": LANE_BOX, "badge_xy": KEY_BPM_BADGE_XY,
}
