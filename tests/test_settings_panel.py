from lyricvideo.settings import Settings
from lyricvideo.settings_panel import values_to_settings


def _raw_defaults() -> dict:
    """Mimics exactly what SettingsPanel.collect() gathers from its Tk variables:
    resolution/encoder/colors as str, fps/crf/sizes as str-or-float (CTkOptionMenu
    and CTkSlider both back onto non-int variable types), toggles as bool,
    fractional-seconds fields as float."""
    return {
        "resolution": "1080p (1920x1080)",
        "fps": "24",
        "encoder": "libx264",
        "crf": 20.0,
        "font_path": "",
        "lyric_size": 48.0,
        "chord_now_size": 64.0,
        "chord_next_size": 32.0,
        "accent_color": "#38bdf8",
        "text_color": "#ffffff",
        "dim_text_color": "#94a3b8",
        "panel_color": "#0b1220",
        "panel_alpha": 150.0,
        "show_chord_timeline": True,
        "show_key_bpm": True,
        "timeline_window_sec": 12.0,
        "snap_chords_to_key": True,
        "prefer_flats": True,
        "include_seventh_chords": False,
        "min_chord_seconds": 0.5,
        "show_chord_legend": True,
        "chord_legend_size": 100.0,
        "youtube_auto_upload": False,
        "youtube_client_secrets_path": "",
        "youtube_privacy": "public",
        "youtube_category_id": "Howto & Style",
        "youtube_made_for_kids": False,
        "youtube_min_days_between_uploads": 2.0,
        "youtube_preferred_upload_hour": 15.0,
    }


def test_values_to_settings_produces_the_defaults_from_default_raw_values():
    assert values_to_settings(_raw_defaults()) == Settings()


def test_values_to_settings_translates_category_label_to_id():
    raw = _raw_defaults()
    raw["youtube_category_id"] = "Education"

    assert values_to_settings(raw).youtube_category_id == "27"


def test_values_to_settings_coerces_string_fps_to_int():
    raw = _raw_defaults()
    raw["fps"] = "30"

    settings = values_to_settings(raw)

    assert settings.fps == 30
    assert isinstance(settings.fps, int)


def test_values_to_settings_coerces_float_sizes_to_int():
    raw = _raw_defaults()
    raw["lyric_size"] = 52.0
    raw["chord_now_size"] = 70.0

    settings = values_to_settings(raw)

    assert settings.lyric_size == 52
    assert isinstance(settings.lyric_size, int)
    assert settings.chord_now_size == 70


def test_values_to_settings_keeps_fractional_seconds_as_float():
    raw = _raw_defaults()
    raw["timeline_window_sec"] = 8.0
    raw["min_chord_seconds"] = 1.25

    settings = values_to_settings(raw)

    assert settings.timeline_window_sec == 8.0
    assert settings.min_chord_seconds == 1.25


def test_values_to_settings_preserves_toggles_and_colors():
    raw = _raw_defaults()
    raw["show_chord_timeline"] = False
    raw["accent_color"] = "#ff0000"

    settings = values_to_settings(raw)

    assert settings.show_chord_timeline is False
    assert settings.accent_color == "#ff0000"
