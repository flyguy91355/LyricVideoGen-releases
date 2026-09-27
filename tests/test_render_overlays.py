"""Issue #7 review: per-frame overlays composite only their own box (and static ones are drawn once), and the support
overlay clears the Key/BPM badge at every output resolution."""

import numpy as np
import pytest
from PIL import Image, ImageDraw

from lyricvideo.models import ChordEvent, ChordTrack
from lyricvideo.render import (
    compute_chord_bar_layout, composite_patch, draw_chord_bar, draw_countdown, draw_support_overlay,
    key_bpm_badge_bottom, load_font, overlay_patch,
)
from lyricvideo.settings import RESOLUTIONS


def _textured_frame(size=(1920, 1080), seed=5):
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8), "RGB")


def _full_frame_composite(frame, overlay):
    """What every draw_* helper used to do on every frame."""
    return Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB")


def test_composite_patch_matches_a_full_frame_composite_exactly():
    """Including overlay pixels that are fully transparent but carry junk color, partial alpha, and a patch that
    touches the frame's edges."""
    rng = np.random.default_rng(11)
    frame = _textured_frame((320, 180))
    pixels = rng.integers(0, 256, (180, 320, 4), dtype=np.uint8)
    pixels[..., 3] = 0
    pixels[20:90, 0:150, 3] = rng.integers(0, 256, (70, 150), dtype=np.uint8)  # touches the left edge
    pixels[150:180, 250:320, 3] = 255                                          # the bottom-right corner
    overlay = Image.fromarray(pixels, "RGBA")

    expected = np.array(_full_frame_composite(frame, overlay))
    got = np.array(composite_patch(frame, overlay_patch(overlay)))

    assert np.array_equal(got, expected)
    assert np.array_equal(np.array(frame), np.array(_textured_frame((320, 180))))  # input untouched


def test_composite_patch_of_nothing_is_an_unchanged_copy():
    frame = _textured_frame((64, 36))
    empty = Image.new("RGBA", frame.size, (9, 9, 9, 0))

    assert overlay_patch(empty) is None
    out = composite_patch(frame, overlay_patch(empty))
    assert out is not frame and np.array_equal(np.array(out), np.array(frame))


def test_composite_patch_applies_several_disjoint_patches():
    frame = _textured_frame((200, 100))
    a = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    ImageDraw.Draw(a).rectangle((5, 5, 40, 30), fill=(200, 10, 10, 128))
    b = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    ImageDraw.Draw(b).rectangle((120, 60, 190, 95), fill=(10, 200, 10, 200))
    both = Image.alpha_composite(a, b)

    got = np.array(composite_patch(frame, overlay_patch(a), overlay_patch(b)))

    assert np.array_equal(got, np.array(_full_frame_composite(frame, both)))


def _old_countdown(frame, seconds_remaining, font_path, frame_size, accent_color=(56, 189, 248)):
    from lyricvideo.render import _COUNTDOWN_BOX_SIZE_FRAC, BOX_FILL

    w, h = frame_size
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    box_side = int(h * _COUNTDOWN_BOX_SIZE_FRAC)
    cx, cy = w // 2, h // 2
    box = (cx - box_side // 2, cy - box_side // 2, cx + box_side // 2, cy + box_side // 2)
    draw.rounded_rectangle(box, radius=int(box_side * 0.18), fill=BOX_FILL, outline=(*accent_color, 255), width=3)
    font = load_font(font_path, int(box_side * 0.5))
    label = str(seconds_remaining)
    label_w = draw.textlength(label, font=font)
    draw.text((cx - label_w / 2, cy - box_side * 0.28), label, font=font, fill=(*accent_color, 255))
    return _full_frame_composite(frame, overlay)


@pytest.mark.parametrize("beats", [4, 3, 1])
def test_cached_countdown_is_pixel_identical_to_the_full_frame_draw(beats, test_font_path):
    frame = _textured_frame()
    expected = np.array(_old_countdown(frame, beats, test_font_path, (1920, 1080)))
    for _ in range(2):
        assert np.array_equal(np.array(draw_countdown(frame, beats, test_font_path)), expected)


def test_cached_support_overlay_is_pixel_identical_to_the_full_frame_draw_at_1080p(test_font_path):
    """1080p (and 720p) keep the old fixed 110 px top, so their output must not move by a pixel."""
    from lyricvideo.render import (
        _SUPPORT_OVERLAY_ALPHA, _SUPPORT_OVERLAY_BASE_FONT_SIZE, _SUPPORT_OVERLAY_MARGIN_X, _SUPPORT_OVERLAY_PAD_X,
        _SUPPORT_OVERLAY_PAD_Y,
    )

    frame = _textured_frame()
    text = "Support this channel"
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(test_font_path, _SUPPORT_OVERLAY_BASE_FONT_SIZE)
    text_w = draw.textlength(text, font=font)
    ascent, descent = font.getmetrics()
    box_right = 1920 - _SUPPORT_OVERLAY_MARGIN_X
    box_left = box_right - (text_w + 2 * _SUPPORT_OVERLAY_PAD_X)
    box = (box_left, 110, box_right, 110 + ascent + descent + 2 * _SUPPORT_OVERLAY_PAD_Y)
    draw.rounded_rectangle(box, radius=int(_SUPPORT_OVERLAY_PAD_Y * 1.2), fill=(11, 18, 32, _SUPPORT_OVERLAY_ALPHA))
    draw.text((box_left + _SUPPORT_OVERLAY_PAD_X, 110 + _SUPPORT_OVERLAY_PAD_Y), text, font=font,
              fill=(56, 189, 248, 255))
    expected = np.array(_full_frame_composite(frame, overlay))

    for _ in range(2):
        assert np.array_equal(np.array(draw_support_overlay(frame, text, test_font_path)), expected)


def _changed_rows(before, after, region=None):
    diff = (np.array(before) != np.array(after)).any(axis=-1)
    if region is not None:
        x0, y0, x1, y1 = region
        mask = np.zeros_like(diff)
        mask[y0:y1, x0:x1] = True
        diff &= mask
    return np.where(diff.any(axis=1))[0]


@pytest.mark.parametrize("frame_size", sorted(RESOLUTIONS.values()))
def test_support_overlay_sits_below_the_key_bpm_badge_at_every_resolution(frame_size, test_font_path):
    """At 1440p the badge's panel reached row 118 while the overlay's fixed top was 110 px, so the overlay's panel
    covered the bottom of the badge's in the last 20 s of every such video."""
    w, h = frame_size
    bg = Image.new("RGB", frame_size, (0, 0, 0))
    track = ChordTrack(events=[ChordEvent(0.0, 4.0, "C#m")], key="C# minor", bpm=120.0)
    upper_right = (w // 2, 0, w, int(h * 0.4))

    badge_rows = _changed_rows(bg, draw_chord_bar(bg, track, 1.0, test_font_path, frame_size=frame_size), upper_right)
    overlay_rows = _changed_rows(
        bg, draw_support_overlay(bg, "Support this channel", test_font_path, frame_size=frame_size), upper_right,
    )

    assert len(badge_rows) and len(overlay_rows)
    assert badge_rows.max() <= key_bpm_badge_bottom(frame_size)
    assert overlay_rows.min() > badge_rows.max()


@pytest.mark.parametrize("frame_size", sorted(RESOLUTIONS.values()) + [(3840, 2160)])
def test_chord_bar_and_its_key_badge_never_touch(frame_size, test_font_path):
    """draw_chord_bar composites the bar and the badge as two separate patches, which is only the same picture as
    one shared overlay while the two never overlap."""
    bg = Image.new("RGB", frame_size, (0, 0, 0))
    track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C#m"), ChordEvent(2.0, 4.0, "G#m")], key="C# minor", bpm=120.0)

    out = draw_chord_bar(bg, track, 1.0, test_font_path, frame_size=frame_size, chord_now_size=120, chord_next_size=60)

    changed = _changed_rows(bg, out)
    gap = range(key_bpm_badge_bottom(frame_size) + 1, compute_chord_bar_layout(frame_size)["chord_box"][1])
    assert len(gap) > 0
    assert not set(changed) & set(gap)
