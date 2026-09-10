"""CustomTkinter Settings panel, styled after LyricChord's own, bound to a Settings
object. Widget construction stays manually/visually verified (this project's
existing testing-constraint precedent for GUI code); values_to_settings() below is
the one piece of real logic here and is unit-tested directly."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import asdict
from tkinter import colorchooser, filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from .settings import ENCODERS, FPS_OPTIONS, RESOLUTIONS, Settings, hex_to_rgb

_INT_FIELDS = {
    "fps", "crf", "lyric_size", "chord_now_size", "chord_next_size", "panel_alpha", "chord_legend_size",
}


def values_to_settings(raw: dict) -> Settings:
    """Turn a flat dict of raw widget values (as SettingsPanel.collect() gathers
    them straight from its Tk variables) into a real Settings object, coercing the
    fields CustomTkinter's slider/dropdown variables always hand back as str/float
    even though Settings itself declares them as int."""
    coerced = dict(raw)
    for name in _INT_FIELDS:
        if name in coerced:
            coerced[name] = int(float(coerced[name]))
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
        if self.on_change is not None:
            self.on_change()

    def _section(self, title: str) -> None:
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(weight="bold")).grid(
            row=self._row, column=0, columnspan=3, sticky="w", padx=6, pady=(14, 4))
        self._row += 1

    def _add(self, label: str, widget) -> None:
        ctk.CTkLabel(self, text=label, anchor="w").grid(row=self._row, column=0, sticky="w", padx=(6, 10), pady=3)
        widget.grid(row=self._row, column=1, sticky="ew", pady=3)
        self._row += 1

    def _option(self, name: str, label: str, values: list) -> None:
        var = self._var(name, tk.StringVar)
        menu = ctk.CTkOptionMenu(self, values=[str(v) for v in values], variable=var)
        self._add(label, menu)

    def _check(self, name: str, label: str) -> None:
        var = self._var(name, tk.BooleanVar)
        check = ctk.CTkCheckBox(self, text=label, variable=var)
        check.grid(row=self._row, column=0, columnspan=2, sticky="w", padx=6, pady=3)
        self._row += 1

    def _slider(self, name: str, label: str, lo: float, hi: float, steps: int, fmt) -> None:
        var = self._var(name, tk.DoubleVar)
        frame = ctk.CTkFrame(self, fg_color="transparent")
        value_label = ctk.CTkLabel(frame, text="", width=56, anchor="e")

        def _on_move(v):
            value_label.configure(text=fmt(float(v)))

        slider = ctk.CTkSlider(frame, from_=lo, to=hi, number_of_steps=steps, variable=var, command=_on_move)
        slider.pack(side="left", fill="x", expand=True)
        value_label.pack(side="left", padx=(8, 0))
        _on_move(var.get())
        self._add(label, frame)

    def _color(self, name: str, label: str) -> None:
        var = self._var(name, tk.StringVar)
        self._add(label, ColorButton(self, var))

    def _browse_font(self) -> None:
        f = filedialog.askopenfilename(parent=self, title="Choose a font",
                                       filetypes=[("Fonts", "*.ttf *.otf *.ttc"), ("All files", "*.*")])
        if f:
            self.vars["font_path"].set(f)

    def _on_reset_clicked(self) -> None:
        if messagebox.askyesno(
            "Reset settings",
            "Reset all settings to their defaults? This cannot be undone.",
        ):
            self.load_from(Settings())

    def _build(self) -> None:
        self._section("Output")
        ctk.CTkButton(
            self, text="Reset to Defaults", command=self._on_reset_clicked, fg_color="gray30", hover_color="gray20",
        ).grid(row=self._row, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 10))
        self._row += 1
        self._option("resolution", "Resolution", list(RESOLUTIONS))
        self._option("fps", "Frame rate", FPS_OPTIONS)
        self._option("encoder", "Encoder", ENCODERS)
        self._slider("crf", "Quality (CRF, lower = better)", 14, 32, 18, lambda v: f"{int(v)}")

        self._section("Typography & colors")
        self.vars["font_path"] = tk.StringVar()
        self.vars["font_path"].trace_add("write", lambda *_: self._changed())
        self._add("Font (blank = auto)", ctk.CTkButton(self, text="Browse font...", command=self._browse_font))
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

        self._section("Chord detection")
        self._check("snap_chords_to_key", "Bias detected chords toward the song key")
        self._check("prefer_flats", "Use flats in flat keys (Bb instead of A#)")
        self._check("include_seventh_chords", "Detect 7th chords (7, m7, maj7)")
        self._slider("min_chord_seconds", "Minimum chord length", 0.2, 2.0, 18, lambda v: f"{v:.1f}s")

    def load_from(self, settings: Settings) -> None:
        # Suppressed while populating every var individually -- each .set() below
        # fires its own trace_add callback, so without this a 20-field load (the
        # initial load AND the Reset-to-Defaults button) would fire on_change
        # (a save + a preview redraw) once per field instead of once total.
        self._suppress_change = True
        try:
            for name, value in asdict(settings).items():
                if name in self.vars:
                    self.vars[name].set(value)
        finally:
            self._suppress_change = False
        self._changed()

    def collect(self) -> Settings:
        raw = {name: var.get() for name, var in self.vars.items()}
        return values_to_settings(raw)
