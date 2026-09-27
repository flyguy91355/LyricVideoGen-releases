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
        "generate_easy_chord_versions": False,
        "youtube_auto_upload": False,
        "youtube_client_secrets_path": "",
        "youtube_privacy": "public",
        "youtube_category_id": "Education",
        "youtube_made_for_kids": False,
        "youtube_uploads_per_day": 5.0,
        "youtube_upload_times": "09:00,12:00,15:00,18:00,21:00",
        "youtube_max_uploads_per_day": 7.0,
        "youtube_quota_retry_hours": 24.0,
        "timing_pass_percent": 90.0,
        "use_image_library": False,
        "image_library_min_score": 0.34,
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


def test_values_to_settings_makes_the_timing_pass_mark_a_whole_number():
    raw = _raw_defaults()
    raw["timing_pass_percent"] = 95.0

    settings = values_to_settings(raw)

    assert settings.timing_pass_percent == 95 and isinstance(settings.timing_pass_percent, int)


def test_values_to_settings_rounds_the_library_score_to_three_places():
    raw = _raw_defaults()
    raw["use_image_library"] = True
    raw["image_library_min_score"] = 0.30000000000000004  # what a slider drag hands back

    s = values_to_settings(raw)

    assert s.use_image_library is True
    assert s.image_library_min_score == 0.3


def test_values_to_settings_keeps_a_multi_line_support_description_intact():
    raw = _raw_defaults()
    raw["support_description_text"] = "Line one.\nTips are never expected ☕\nhttps://ko-fi.com/x"

    assert values_to_settings(raw).support_description_text == "Line one.\nTips are never expected ☕\nhttps://ko-fi.com/x"



# --- typed values (issue #7 review) ---------------------------------------------------------------------------------------

def test_parse_clamped_float_reads_a_leading_dot_and_a_decimal_comma():
    """".3" used to read as 3 (the pattern needed a digit before the point) and clamp to the slider's maximum."""
    assert _parse_clamped_float(".5", lo=0.0, hi=1.5) == 0.5
    assert _parse_clamped_float(".3s", lo=0.2, hi=2.0) == 0.3
    assert _parse_clamped_float("-.5", lo=-1.0, hi=1.0) == -0.5
    assert _parse_clamped_float("0,5", lo=0.0, hi=1.5) == 0.5
    assert _parse_clamped_float("5.", lo=0.0, hi=10.0) == 5.0


import gc  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def panel_factory():
    ctk = pytest.importorskip("customtkinter")
    try:
        root = _new_ctk_root(ctk)
    except Exception as e:
        pytest.skip(f"no display available for a real window ({type(e).__name__}: {e})")
    root.withdraw()
    from lyricvideo.settings_panel import SettingsPanel

    yield lambda settings, **kw: SettingsPanel(root, settings, **kw)
    root.destroy()
    gc.collect()


def _type_into(panel, name, text):
    entry = panel._slider_entries[name]
    entry.delete(0, "end")
    entry.insert(0, text)
    panel._slider_commits[name]()          # what <Return> / <FocusOut> run


def test_leaving_an_untouched_slider_box_changes_nothing(panel_factory):
    """F077: <FocusOut> re-parsed the box's ROUNDED display -- an opacity's "58%" read back as 58 of 255 -- so clicking
    in and out of the box shrank the value every time (150 -> 58 -> 22 -> 8), and for "Maximum publish per day" the
    no-op write regenerated (reset) the owner's hand-edited publish times."""
    times = "09:30,12:00,15:00,18:00,21:00"
    panel = panel_factory(Settings(panel_alpha=150, chord_diagram_panel_alpha=235, youtube_upload_times=times,
                                   min_chord_seconds=0.55))
    for _ in range(3):
        for name in ("panel_alpha", "chord_diagram_panel_alpha", "youtube_uploads_per_day", "min_chord_seconds"):
            panel._slider_commits[name]()

    assert panel.vars["panel_alpha"].get() == 150 and panel.vars["chord_diagram_panel_alpha"].get() == 235
    assert panel.vars["min_chord_seconds"].get() == 0.55
    assert panel.vars["youtube_upload_times"].get() == times
    assert panel._dirty_fields() == {}


def test_a_typed_opacity_is_read_as_the_percent_the_box_shows(panel_factory):
    panel = panel_factory(Settings(panel_alpha=150))

    _type_into(panel, "panel_alpha", "80")
    assert panel.vars["panel_alpha"].get() == 204                 # 80% of 255
    _type_into(panel, "panel_alpha", "80%")
    assert panel.vars["panel_alpha"].get() == 204


def test_a_typed_value_still_takes_effect_and_the_box_snaps_to_it(panel_factory):
    panel = panel_factory(Settings())

    _type_into(panel, "min_chord_seconds", ".3")
    assert panel.vars["min_chord_seconds"].get() == 0.3            # not clamped to 2.0 any more
    _type_into(panel, "lyric_size", "500")
    assert panel.vars["lyric_size"].get() == 100 and panel._slider_entries["lyric_size"].get() == "100"


def test_moving_the_publish_count_still_regenerates_the_times(panel_factory):
    panel = panel_factory(Settings(youtube_upload_times="09:30,12:00,15:00,18:00,21:00", youtube_uploads_per_day=5))

    panel.vars["youtube_uploads_per_day"].set(3)

    assert len(panel.vars["youtube_upload_times"].get().split(",")) == 3


# --- dirty marks (F078) and parented confirmations (F138) -----------------------------------------------------------------

def test_a_change_relabels_only_the_fields_whose_dirty_mark_flipped(panel_factory, monkeypatch):
    """Every slider step or keystroke reconfigured all ~44 labels (each a re-layout): ~75 ms per event here."""
    panel = panel_factory(Settings())
    relabelled = []
    for name, widget in panel._field_widgets.items():
        real = widget.configure
        monkeypatch.setattr(widget, "configure", lambda _real=real, _name=name, **kw: relabelled.append(_name) or _real(**kw))

    panel.vars["lyric_size"].set(60)
    assert relabelled == ["lyric_size"] and panel._field_widgets["lyric_size"].cget("text").startswith("●")
    relabelled.clear()
    panel.vars["lyric_size"].set(61)                               # still dirty: nothing to redraw
    assert relabelled == []
    panel.vars["lyric_size"].set(Settings().lyric_size)            # back to the saved value: just that one again
    assert relabelled == ["lyric_size"] and not panel._field_widgets["lyric_size"].cget("text").startswith("●")


def test_the_panels_confirmations_are_asked_over_its_own_window(panel_factory, monkeypatch):
    """A parentless askyesno from the transient + grab_set Settings popup can open BEHIND it on some Linux window
    managers (the bug HISTORY 9-10 fixed for the Apply Update dialog)."""
    from lyricvideo import settings_panel
    asked = []
    monkeypatch.setattr(settings_panel.messagebox, "askyesno",
                        lambda title, message, **kw: asked.append((title, kw.get("parent"))) or False)
    panel = panel_factory(Settings())
    panel.vars["lyric_size"].set(60)

    panel._on_save_clicked()
    panel._on_discard_clicked()
    panel._on_reset_clicked()

    top = panel.winfo_toplevel()
    assert asked == [("Save settings", top), ("Discard changes", top), ("Reset settings", top)]


def test_a_save_that_fails_to_write_says_so_and_keeps_the_changes_unsaved(panel_factory, monkeypatch):
    """Settings.save() raises OSError on a failed write (disk full, no permission): the panel must show an error over its
    own window and keep the dirty markers and its baseline -- never look like the change was saved (wave-1 misc-a)."""
    from lyricvideo import settings_panel
    monkeypatch.setattr(settings_panel.messagebox, "askyesno", lambda title, message, **kw: True)
    errors = []
    monkeypatch.setattr(settings_panel.messagebox, "showerror",
                        lambda title, message, **kw: errors.append((title, message, kw.get("parent"))))

    def failing_save(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Settings, "save", failing_save)
    panel = panel_factory(Settings())
    baseline = panel._baseline
    panel.vars["lyric_size"].set(60)

    panel._on_save_clicked()                                   # must not raise out of the Tk callback

    assert len(errors) == 1 and errors[0][0] == "Save settings" and "No space left" in errors[0][1]
    assert errors[0][2] is panel.winfo_toplevel()
    assert "lyric_size" in panel._dirty_fields()               # still unsaved
    assert panel._baseline is baseline
    assert panel.save_button.cget("state") == "normal"


def _new_ctk_root(ctk):
    """A real CTk root. On this Windows box, creating one under pytest's output capture intermittently fails once with
    "Can't find a usable init.tcl/tk.tcl" (~1 in 40 roots) and succeeds on the very next try -- so retry a couple of times
    before calling the display unavailable (a skip would hide a real regression)."""
    for attempt in range(3):
        try:
            return ctk.CTk()
        except Exception:
            if attempt == 2:
                raise
