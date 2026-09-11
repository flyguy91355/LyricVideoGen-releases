"""CustomTkinter Settings panel, styled after LyricChord's own, bound to a Settings
object. Widget construction stays manually/visually verified (this project's
existing testing-constraint precedent for GUI code); values_to_settings() below is
the one piece of real logic here and is unit-tested directly."""

from __future__ import annotations

import re
import tkinter as tk
from collections.abc import Callable
from dataclasses import asdict
from tkinter import colorchooser, filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from .settings import ENCODERS, FPS_OPTIONS, RESOLUTIONS, Settings, hex_to_rgb

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_clamped_float(text: str, lo: float, hi: float) -> float:
    """Pulls a number out of user-typed entry-box text -- tolerating a stray
    unit suffix like '%' or 's' -- and clamps it into [lo, hi]. Falls back to
    lo on completely unparseable input rather than raising: a typo in the
    box must never crash the GUI."""
    match = _NUMBER_RE.search(text)
    if not match:
        return lo
    return min(max(float(match.group()), lo), hi)

_INT_FIELDS = {
    "fps", "crf", "countdown_beats", "lyric_size", "chord_now_size", "chord_next_size", "panel_alpha",
    "chord_legend_size", "chord_diagram_panel_alpha", "youtube_min_days_between_uploads",
    "youtube_preferred_upload_hour", "support_overlay_size",
}

_YOUTUBE_CATEGORY_IDS = {"Howto & Style": "26", "Education": "27", "Music": "10"}
_YOUTUBE_CATEGORY_LABELS = {v: k for k, v in _YOUTUBE_CATEGORY_IDS.items()}


def values_to_settings(raw: dict) -> Settings:
    """Turn a flat dict of raw widget values (as SettingsPanel.collect() gathers
    them straight from its Tk variables) into a real Settings object, coercing the
    fields CustomTkinter's slider/dropdown variables always hand back as str/float
    even though Settings itself declares them as int."""
    coerced = dict(raw)
    for name in _INT_FIELDS:
        if name in coerced:
            coerced[name] = int(float(coerced[name]))
    if "youtube_category_id" in coerced:
        coerced["youtube_category_id"] = _YOUTUBE_CATEGORY_IDS.get(
            coerced["youtube_category_id"], coerced["youtube_category_id"],
        )
    return Settings.from_dict(coerced)


def _contrast_text(hex_color: str) -> str:
    r, g, b = hex_to_rgb(hex_color)
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


class ColorButton(ctk.CTkButton):
    """Button that shows a colour swatch and opens the system colour chooser."""

    def __init__(self, master, variable: tk.StringVar, **kwargs):
        super().__init__(master, text="", width=100, command=self._pick, **kwargs)
        self.var = variable
        self.var.trace_add("write", lambda *_: self._refresh())
        self._refresh()

    def _refresh(self) -> None:
        color = self.var.get().strip() or "#000000"
        try:
            self.configure(fg_color=color, hover_color=color, text=color, text_color=_contrast_text(color))
        except tk.TclError:
            pass

    def _pick(self) -> None:
        result = colorchooser.askcolor(color=self.var.get() or None, parent=self)
        if result and result[1]:
            self.var.set(result[1])


class SettingsPanel(ctk.CTkScrollableFrame):
    """Sectioned, scrollable settings controls bound to a Settings object."""

    def __init__(self, master, settings: Settings, on_change: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(master, label_text="Settings", **kwargs)
        self.on_change = on_change
        self.vars: dict[str, tk.Variable] = {}
        self._row = 0
        self._suppress_change = False
        # The settings actually on disk right now -- NOT necessarily what the
        # widgets currently show. Everything below is dirty-tracking against
        # this baseline; it only moves forward on an explicit Save (or a
        # Discard, which just reloads it). Nothing else in this class writes
        # to disk. See "Save Settings" below: an accidental slider drag must
        # never become permanent on its own (real owner incident, 2026-09-10).
        self._baseline = settings
        self._field_labels: dict[str, str] = {}
        self._field_widgets: dict[str, ctk.CTkBaseClass] = {}
        self._field_default_color: dict[str, object] = {}
        self._field_default_font: dict[str, object] = {}
        self._field_formatters: dict[str, Callable] = {}
        self.grid_columnconfigure(1, weight=1)
        self._build()
        self.load_from(settings)

    def _var(self, name: str, kind) -> tk.Variable:
        v = kind()
        v.trace_add("write", lambda *_: self._changed())
        self.vars[name] = v
        return v

    def _changed(self) -> None:
        if self._suppress_change:
            return
        self._refresh_dirty_indicators()
        if self.on_change is not None:
            self.on_change()

    def _section(self, title: str) -> None:
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(weight="bold")).grid(
            row=self._row, column=0, columnspan=3, sticky="w", padx=6, pady=(14, 4))
        self._row += 1

    def _default_text(self, name: str) -> str:
        return f"default: {self._format_value(name, getattr(Settings(), name))}"

    def _add_default_label(self, name: str, row: int) -> None:
        ctk.CTkLabel(
            self, text=self._default_text(name), text_color="gray50", anchor="w", font=ctk.CTkFont(size=11),
        ).grid(row=row, column=2, sticky="w", padx=(8, 6), pady=3)

    def _add(self, name: str, label: str, widget) -> None:
        # Keep `label` short (well under ~40 chars, matching every existing
        # field here) -- real bug found live, 2026-09-11: a long label on
        # ONE field made grid_columnconfigure's shared column-0 width blow
        # out for the WHOLE panel (Tkinter grid computes one width per
        # column across every row sharing it), squeezing columns 1/2 to
        # nothing for every OTHER field too, not just the long-labeled one.
        # Verified by rendering an actual composite before/after -- a code
        # read alone would never have caught this.
        lbl = ctk.CTkLabel(self, text=label, anchor="w")
        lbl.grid(row=self._row, column=0, sticky="w", padx=(6, 10), pady=3)
        widget.grid(row=self._row, column=1, sticky="ew", pady=3)
        self._add_default_label(name, self._row)
        self._row += 1
        self._register_field_label(name, label, lbl)

    def _register_field_label(self, name: str, label: str, widget) -> None:
        self._field_labels[name] = label
        self._field_widgets[name] = widget
        self._field_default_color[name] = widget.cget("text_color")
        self._field_default_font[name] = widget.cget("font")

    def _option(self, name: str, label: str, values: list) -> None:
        var = self._var(name, tk.StringVar)
        menu = ctk.CTkOptionMenu(self, values=[str(v) for v in values], variable=var)
        self._add(name, label, menu)

    def _check(self, name: str, label: str) -> None:
        var = self._var(name, tk.BooleanVar)
        check = ctk.CTkCheckBox(self, text=label, variable=var)
        check.grid(row=self._row, column=0, columnspan=2, sticky="w", padx=6, pady=3)
        self._add_default_label(name, self._row)
        self._row += 1
        self._register_field_label(name, label, check)

    def _slider(self, name: str, label: str, lo: float, hi: float, steps: int, fmt) -> None:
        # Registered before _add() below so _default_text() can already use
        # this field's real formatter (e.g. "2.0s") instead of a plain str().
        self._field_formatters[name] = fmt
        var = self._var(name, tk.DoubleVar)
        frame = ctk.CTkFrame(self, fg_color="transparent")
        entry_var = tk.StringVar()

        def _refresh_entry_text(*_args) -> None:
            entry_var.set(fmt(float(var.get())))

        # A trace on the VARIABLE itself, not CTkSlider's own `command=` --
        # command only fires from a live mouse drag (confirmed by reading
        # CTkSlider's source: its variable-change path calls plain set(),
        # never self._command), so relying on it left the entry box showing
        # 0/off instead of the real loaded value the instant this panel
        # opened (found via an actual screenshot). A variable trace instead
        # covers every path that can change the value -- a drag, load_from()
        # on open, Reset to Defaults, AND the typed-entry commit below --
        # through the one mechanism, rather than needing each path to
        # remember to refresh the display itself.
        var.trace_add("write", _refresh_entry_text)

        def _on_entry_commit(_event=None) -> None:
            # Typing an exact value (owner request) -- tolerates a stray unit
            # suffix like '%'/'s' and clamps into this slider's own range,
            # then re-formats the box so a sloppy typed value (e.g. "500")
            # visibly snaps to what actually took effect (e.g. "100%").
            var.set(_parse_clamped_float(entry_var.get(), lo, hi))

        # The fixed-width entry must be packed FIRST, pinned to the right --
        # packing the expand=True slider first claims the whole frame before
        # the entry is ever considered, squeezing it to zero width (also
        # found via screenshot: the entry existed in the widget tree but
        # never appeared on screen).
        entry = ctk.CTkEntry(frame, textvariable=entry_var, width=64, justify="right")
        entry.bind("<Return>", _on_entry_commit)
        entry.bind("<FocusOut>", _on_entry_commit)
        entry.pack(side="right")
        slider = ctk.CTkSlider(frame, from_=lo, to=hi, number_of_steps=steps, variable=var)
        slider.pack(side="left", fill="x", expand=True, padx=(0, 8))
        _refresh_entry_text()
        self._add(name, label, frame)

    def _color(self, name: str, label: str) -> None:
        var = self._var(name, tk.StringVar)
        self._add(name, label, ColorButton(self, var))

    def _text(self, name: str, label: str) -> None:
        var = self._var(name, tk.StringVar)
        entry = ctk.CTkEntry(self, textvariable=var)
        self._add(name, label, entry)

    def _browse_font(self) -> None:
        f = filedialog.askopenfilename(parent=self, title="Choose a font",
                                       filetypes=[("Fonts", "*.ttf *.otf *.ttc"), ("All files", "*.*")])
        if f:
            self.vars["font_path"].set(f)

    def _browse_youtube_secrets(self) -> None:
        f = filedialog.askopenfilename(
            parent=self, title="Choose your client_secret_*.json file",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if f:
            self.vars["youtube_client_secrets_path"].set(f)

    def _on_reset_clicked(self) -> None:
        if messagebox.askyesno(
            "Reset settings",
            "Reset all settings to their defaults? This cannot be undone.",
        ):
            self.load_from(Settings())

    def _format_value(self, name: str, value) -> str:
        if name == "youtube_category_id":
            return _YOUTUBE_CATEGORY_LABELS.get(value, str(value))
        if isinstance(value, bool):
            return "On" if value else "Off"
        if name in self._field_formatters:
            return self._field_formatters[name](value)
        if isinstance(value, str) and not value.strip():
            return "(none)"
        return str(value)

    def _dirty_fields(self) -> dict:
        """Every field where the live widgets disagree with the settings
        actually on disk (self._baseline) -- name -> (old, new) raw values."""
        baseline = asdict(self._baseline)
        current = asdict(self.collect())
        return {name: (baseline[name], current[name]) for name in current if current[name] != baseline[name]}

    def has_unsaved_changes(self) -> bool:
        """Whether anything differs from the settings actually on disk right
        now -- used by the popup window's own close handler to decide
        whether closing needs a discard confirmation."""
        return bool(self._dirty_fields())

    def _refresh_dirty_indicators(self) -> None:
        """Marks each changed field's own label/checkbox with a small bold,
        colored dot so a stray slider move is visible just by scrolling past
        it -- the first of two chances to notice, the second being the
        itemized confirmation Save Settings shows before writing anything to
        disk."""
        dirty = self._dirty_fields()
        for name, widget in self._field_widgets.items():
            base_text = self._field_labels[name]
            if name in dirty:
                widget.configure(
                    text=f"● {base_text}", text_color="#f0a339",
                    font=ctk.CTkFont(weight="bold"),
                )
            else:
                widget.configure(
                    text=base_text, text_color=self._field_default_color[name],
                    font=self._field_default_font[name],
                )
        state = "normal" if dirty else "disabled"
        self.save_button.configure(state=state)
        self.discard_button.configure(state=state)

    def _on_save_clicked(self) -> None:
        dirty = self._dirty_fields()
        if not dirty:
            return
        lines = [
            f"{self._field_labels.get(name, name)}: "
            f"{self._format_value(name, old)} → {self._format_value(name, new)}"
            for name, (old, new) in dirty.items()
        ]
        if not messagebox.askyesno("Save settings", "Save these changes?\n\n" + "\n".join(lines)):
            return
        settings = self.collect()
        settings.save()
        self._baseline = settings
        self._refresh_dirty_indicators()

    def _on_discard_clicked(self) -> None:
        if not self._dirty_fields():
            return
        if messagebox.askyesno("Discard changes", "Discard all unsaved changes and reload the last saved settings?"):
            self.load_from(self._baseline)

    def _build(self) -> None:
        self._section("Output")
        save_row = ctk.CTkFrame(self, fg_color="transparent")
        save_row.grid(row=self._row, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 4))
        save_row.grid_columnconfigure(0, weight=1)
        save_row.grid_columnconfigure(1, weight=1)
        self.save_button = ctk.CTkButton(save_row, text="Save Settings", command=self._on_save_clicked, state="disabled")
        self.save_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.discard_button = ctk.CTkButton(
            save_row, text="Discard changes", command=self._on_discard_clicked, state="disabled",
            fg_color="gray30", hover_color="gray20",
        )
        self.discard_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self._row += 1
        ctk.CTkButton(
            self, text="Reset to Defaults", command=self._on_reset_clicked, fg_color="gray30", hover_color="gray20",
        ).grid(row=self._row, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 10))
        self._row += 1
        self._option("resolution", "Resolution", list(RESOLUTIONS))
        self._option("fps", "Frame rate", FPS_OPTIONS)
        self._option("encoder", "Encoder", ENCODERS)
        self._slider("crf", "Quality (CRF, lower = better)", 14, 32, 18, lambda v: f"{int(v)}")
        self._slider("countdown_beats", "Countdown before song starts (beats)", 0, 8, 8,
                     lambda v: "off" if int(v) == 0 else f"{int(v)} beats")

        self._section("Typography & colors")
        self.vars["font_path"] = tk.StringVar()
        self.vars["font_path"].trace_add("write", lambda *_: self._changed())
        self._add("font_path", "Font (blank = auto)",
                   ctk.CTkButton(self, text="Browse font...", command=self._browse_font))
        self._slider("lyric_size", "Lyric size", 30, 100, 70, lambda v: f"{int(v)}")
        self._slider("chord_now_size", "Chord (NOW) size", 40, 120, 80, lambda v: f"{int(v)}")
        self._slider("chord_next_size", "Chord (NEXT) size", 20, 60, 40, lambda v: f"{int(v)}")
        self._color("accent_color", "Accent (current chord)")
        self._color("text_color", "Lyric text")
        self._color("dim_text_color", "Dim labels")
        self._color("panel_color", "Panel background")
        self._slider("panel_alpha", "Panel opacity", 0, 255, 51, lambda v: f"{int(v / 255 * 100)}%")

        self._section("Chord bar")
        self._check("show_chord_timeline", "Show scrolling chord timeline")
        self._check("show_key_bpm", "Show key and BPM")
        self._slider("timeline_window_sec", "Timeline look-ahead", 4, 30, 26, lambda v: f"{v:.0f}s")
        self._check("show_chord_legend", "Show chord fingering chart (upper-left)")
        self._slider("chord_legend_size", "Chord chart size", 40, 150, 22, lambda v: f"{int(v)}%")
        self._slider("chord_diagram_panel_alpha", "Chord chart background opacity", 0, 255, 51,
                     lambda v: f"{int(v / 255 * 100)}%")

        self._section("Image pacing")
        self._slider("image_min_hold_seconds", "Minimum image hold (instrumental sections)", 0.5, 8.0, 15,
                     lambda v: f"{v:.1f}s")
        self._slider("image_transition_seconds", "Crossfade length", 0.0, 1.5, 30, lambda v: f"{v:.2f}s")

        self._section("Support overlay & description")
        self._text("support_overlay_text", "Overlay text (blank = off)")
        self._slider("support_overlay_size", "Overlay size", 50, 200, 30, lambda v: f"{int(v)}%")
        self._slider("support_overlay_lead_seconds", "Show during the last...", 5, 60, 55, lambda v: f"{int(v)}s")
        self._text("support_description_text", "Description text (blank = off)")

        self._section("Chord detection")
        self._check("snap_chords_to_key", "Bias detected chords toward the song key")
        self._check("prefer_flats", "Use flats in flat keys (Bb instead of A#)")
        self._check("include_seventh_chords", "Detect 7th chords (7, m7, maj7)")
        self._slider("min_chord_seconds", "Minimum chord length", 0.2, 2.0, 18, lambda v: f"{v:.1f}s")

        self._section("YouTube")
        self.vars["youtube_client_secrets_path"] = tk.StringVar()
        self.vars["youtube_client_secrets_path"].trace_add("write", lambda *_: self._changed())
        self._add("youtube_client_secrets_path", "Client secrets file",
                   ctk.CTkButton(self, text="Browse client secrets file...", command=self._browse_youtube_secrets))
        self._check("youtube_auto_upload", "Auto-upload finished videos to YouTube")
        self._option("youtube_privacy", "Privacy", ["public", "unlisted", "private"])
        self._option("youtube_category_id", "Category", list(_YOUTUBE_CATEGORY_IDS.keys()))
        self._check("youtube_made_for_kids", "Made for kids")
        self._slider("youtube_min_days_between_uploads", "Minimum days between uploads", 1, 14, 13,
                     lambda v: f"{int(v)}d")
        self._slider("youtube_preferred_upload_hour", "Preferred upload hour", 0, 23, 23,
                     lambda v: f"{int(v) % 12 or 12}{'AM' if int(v) < 12 else 'PM'}")

    def load_from(self, settings: Settings) -> None:
        # Suppressed while populating every var individually -- each .set() below
        # fires its own trace_add callback, so without this a 20-field load (the
        # initial load AND the Reset-to-Defaults button) would fire on_change
        # (a save + a preview redraw) once per field instead of once total.
        self._suppress_change = True
        try:
            for name, value in asdict(settings).items():
                if name == "youtube_category_id":
                    value = _YOUTUBE_CATEGORY_LABELS.get(value, value)
                if name in self.vars:
                    self.vars[name].set(value)
        finally:
            self._suppress_change = False
        self._changed()

    def collect(self) -> Settings:
        raw = {name: var.get() for name, var in self.vars.items()}
        return values_to_settings(raw)
