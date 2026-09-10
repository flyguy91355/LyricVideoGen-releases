import numpy as np
from PIL import Image

from lyricvideo.settings import Settings
from lyricvideo.settings_preview import PREVIEW_FRAME_SIZE, render_preview_frame


def test_render_preview_frame_returns_the_expected_size():
    frame = render_preview_frame(Settings())

    assert frame.size == PREVIEW_FRAME_SIZE


def test_render_preview_frame_is_a_real_image_not_blank():
    frame = render_preview_frame(Settings())

    assert frame.getextrema() != ((0, 0), (0, 0), (0, 0))


def test_render_preview_frame_respects_custom_accent_color():
    default_frame = np.array(render_preview_frame(Settings()))
    red_frame = np.array(render_preview_frame(Settings(accent_color="#ff0000")))

    assert not np.array_equal(default_frame, red_frame)


def test_render_preview_frame_respects_custom_text_color():
    default_frame = np.array(render_preview_frame(Settings()))
    red_frame = np.array(render_preview_frame(Settings(text_color="#ff0000")))

    assert not np.array_equal(default_frame, red_frame)


def test_render_preview_frame_respects_panel_alpha():
    transparent = np.array(render_preview_frame(Settings(panel_alpha=0)))
    opaque = np.array(render_preview_frame(Settings(panel_alpha=255)))

    assert not np.array_equal(transparent, opaque)


def test_render_preview_frame_hides_chord_timeline_when_disabled():
    shown = np.array(render_preview_frame(Settings(show_chord_timeline=True)))
    hidden = np.array(render_preview_frame(Settings(show_chord_timeline=False)))

    assert not np.array_equal(shown, hidden)


def test_render_preview_frame_hides_key_bpm_badge_when_disabled():
    shown = np.array(render_preview_frame(Settings(show_key_bpm=True)))
    hidden = np.array(render_preview_frame(Settings(show_key_bpm=False)))

    assert not np.array_equal(shown, hidden)


def test_render_preview_frame_ignores_chord_detection_settings():
    """Chord-detection tuning (snap_chords_to_key etc.) has nothing to do with
    how a frame is drawn -- the synthetic preview chord track is fixed, so
    these fields must never change the rendered pixels."""
    default_frame = np.array(render_preview_frame(Settings()))
    detection_tweaked = np.array(render_preview_frame(
        Settings(snap_chords_to_key=False, prefer_flats=False,
                 include_seventh_chords=True, min_chord_seconds=2.0)
    ))

    assert np.array_equal(default_frame, detection_tweaked)


def test_render_preview_frame_shows_the_chord_legend():
    shown = np.array(render_preview_frame(Settings(show_chord_legend=True)))
    hidden = np.array(render_preview_frame(Settings(show_chord_legend=False)))

    assert not np.array_equal(shown, hidden)
