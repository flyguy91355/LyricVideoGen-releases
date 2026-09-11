from lyricvideo.settings import Settings
from lyricvideo.settings_panel import _parse_clamped_float, values_to_settings


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
        "countdown_beats": 4.0,
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
        "chord_diagram_panel_alpha": 235.0,
        "youtube_auto_upload": False,
        "youtube_client_secrets_path": "",
        "youtube_privacy": "public",
        "youtube_category_id": "Education",
        "youtube_made_for_kids": False,
        "youtube_min_days_between_uploads": 2.0,
        "youtube_preferred_upload_hour": 15.0,
    }


def test_values_to_settings_produces_the_defaults_from_default_raw_values():
    assert values_to_settings(_raw_defaults()) == Settings()


def test_values_to_settings_translates_category_label_to_id():
    raw = _raw_defaults()
    raw["youtube_category_id"] = "Howto & Style"

    assert values_to_settings(raw).youtube_category_id == "26"


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


def test_values_to_settings_coerces_float_support_overlay_size_to_int():
    raw = _raw_defaults()
    raw["support_overlay_size"] = 150.0

    settings = values_to_settings(raw)

    assert settings.support_overlay_size == 150
    assert isinstance(settings.support_overlay_size, int)


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


def test_parse_clamped_float_reads_a_plain_number():
    assert _parse_clamped_float("70", lo=0.0, hi=100.0) == 70.0


def test_parse_clamped_float_strips_a_unit_suffix():
    assert _parse_clamped_float("70%", lo=0.0, hi=100.0) == 70.0
    assert _parse_clamped_float("3.5s", lo=0.0, hi=10.0) == 3.5


def test_parse_clamped_float_handles_a_negative_number():
    assert _parse_clamped_float("-2", lo=-5.0, hi=5.0) == -2.0


def test_parse_clamped_float_clamps_above_the_range():
    assert _parse_clamped_float("500", lo=0.0, hi=100.0) == 100.0


def test_parse_clamped_float_clamps_below_the_range():
    assert _parse_clamped_float("-50", lo=0.0, hi=100.0) == 0.0


def test_parse_clamped_float_falls_back_to_lo_on_unparseable_text():
    assert _parse_clamped_float("abc", lo=2.0, hi=100.0) == 2.0
    assert _parse_clamped_float("", lo=2.0, hi=100.0) == 2.0
