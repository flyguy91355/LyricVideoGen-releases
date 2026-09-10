import json

from lyricvideo.settings import ENCODERS, FPS_OPTIONS, RESOLUTIONS, Settings


def test_settings_defaults_match_current_hardcoded_render_behavior():
    s = Settings()
    assert s.resolution == "1080p (1920x1080)"
    assert RESOLUTIONS[s.resolution] == (1920, 1080)
    assert s.fps == 24
    assert s.encoder == "libx264"
    assert s.crf == 20
    assert s.countdown_seconds == 3
    assert s.font_path == ""
    assert s.lyric_size == 48
    assert s.chord_now_size == 64
    assert s.chord_next_size == 32
    assert s.accent_color == "#38bdf8"
    assert s.text_color == "#ffffff"
    assert s.dim_text_color == "#94a3b8"
    assert s.panel_color == "#0b1220"
    assert s.panel_alpha == 150
    assert s.show_chord_timeline is True
    assert s.show_key_bpm is True
    assert s.timeline_window_sec == 12.0
    assert s.snap_chords_to_key is True
    assert s.prefer_flats is True
    assert s.include_seventh_chords is False
    assert s.min_chord_seconds == 0.5
    assert s.show_chord_legend is True
    assert s.chord_legend_size == 100
    assert s.chord_diagram_panel_alpha == 235


def test_youtube_settings_defaults():
    s = Settings()
    assert s.youtube_auto_upload is False
    assert s.youtube_client_secrets_path == ""
    assert s.youtube_privacy == "public"
    assert s.youtube_category_id == "27"
    assert s.youtube_made_for_kids is False
    assert s.youtube_min_days_between_uploads == 2
    assert s.youtube_preferred_upload_hour == 15


def test_resolutions_encoders_fps_options_are_nonempty():
    assert "1080p (1920x1080)" in RESOLUTIONS
    assert "720p (1280x720)" in RESOLUTIONS
    assert "1440p (2560x1440)" in RESOLUTIONS
    assert "libx264" in ENCODERS
    assert 24 in FPS_OPTIONS


def test_hex_to_rgb_parses_a_hex_color():
    from lyricvideo.settings import hex_to_rgb

    assert hex_to_rgb("#38bdf8") == (56, 189, 248)


def test_hex_to_rgb_tolerates_missing_hash_prefix():
    from lyricvideo.settings import hex_to_rgb

    assert hex_to_rgb("38bdf8") == (56, 189, 248)


def test_hex_to_rgb_falls_back_on_invalid_input():
    from lyricvideo.settings import hex_to_rgb

    assert hex_to_rgb("not-a-color") == (255, 255, 255)
    assert hex_to_rgb("") == (255, 255, 255)


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    original = Settings(accent_color="#ff0000", fps=30, show_key_bpm=False)

    original.save(path)
    restored = Settings.load(path)

    assert restored == original


def test_load_returns_defaults_when_file_missing(tmp_path):
    path = tmp_path / "does-not-exist.json"

    loaded = Settings.load(path)

    assert loaded == Settings()


def test_from_dict_tolerates_unknown_and_missing_keys():
    settings = Settings.from_dict({"accent_color": "#00ff00", "some_future_field": 123})

    assert settings.accent_color == "#00ff00"
    assert settings.fps == 24  # untouched fields keep their defaults


def test_load_tolerates_unknown_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"accent_color": "#00ff00", "some_future_field": 123}), encoding="utf-8")

    loaded = Settings.load(path)

    assert loaded.accent_color == "#00ff00"
    assert loaded.fps == 24  # untouched fields keep their defaults


def test_load_tolerates_missing_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"fps": 30}), encoding="utf-8")

    loaded = Settings.load(path)

    assert loaded.fps == 30
    assert loaded.accent_color == "#38bdf8"  # default, not crashed


def test_load_tolerates_corrupt_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")

    loaded = Settings.load(path)

    assert loaded == Settings()


def test_render_kwargs_maps_resolution_and_colors():
    settings = Settings(
        resolution="720p (1280x720)", lyric_size=52, text_color="#ff0000",
        accent_color="#00ff00", dim_text_color="#0000ff", panel_color="#111111",
        panel_alpha=100, chord_now_size=70, chord_next_size=36,
        show_chord_timeline=False, show_key_bpm=False, timeline_window_sec=8.0,
    )

    kwargs = settings.render_kwargs()

    assert kwargs == {
        "frame_size": (1280, 720),
        "lyric_size": 52,
        "text_color": (255, 0, 0),
        "accent_color": (0, 255, 0),
        "dim_text_color": (0, 0, 255),
        "panel_color": (17, 17, 17),
        "panel_alpha": 100,
        "chord_now_size": 70,
        "chord_next_size": 36,
        "show_chord_timeline": False,
        "show_key_bpm": False,
        "timeline_window_sec": 8.0,
        "show_chord_legend": True,
        "chord_legend_scale": 1.0,
        "chord_diagram_panel_alpha": 235,
    }


def test_render_kwargs_matches_defaults_when_settings_are_default():
    kwargs = Settings().render_kwargs()

    assert kwargs["frame_size"] == (1920, 1080)
    assert kwargs["text_color"] == (255, 255, 255)
    assert kwargs["accent_color"] == (56, 189, 248)


def test_render_kwargs_includes_show_chord_legend():
    assert Settings().render_kwargs()["show_chord_legend"] is True
    assert Settings(show_chord_legend=False).render_kwargs()["show_chord_legend"] is False


def test_render_kwargs_includes_chord_legend_scale():
    assert Settings().render_kwargs()["chord_legend_scale"] == 1.0
    assert Settings(chord_legend_size=50).render_kwargs()["chord_legend_scale"] == 0.5


def test_render_kwargs_includes_chord_diagram_panel_alpha():
    assert Settings().render_kwargs()["chord_diagram_panel_alpha"] == 235
    assert Settings(chord_diagram_panel_alpha=90).render_kwargs()["chord_diagram_panel_alpha"] == 90
