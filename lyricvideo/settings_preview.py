"""A live, synthetic preview of what the current Settings will actually look like
in a rendered frame -- no real song, no network, no AI image. Rendered at the
owner's chosen output resolution (so absolute pixel sizes like chord_now_size stay
proportionally correct) and then downscaled to a small on-screen thumbnail."""

from __future__ import annotations

import customtkinter as ctk
from PIL import Image

from .chord_diagram import draw_chord_legend
from .layout import Scene, SceneLine, SceneWord
from .models import ChordEvent, ChordTrack, current_chord_at
from .pipeline import default_font, ordered_unique_chords
from .render import draw_chord_bar, draw_scene
from .settings import Settings

PREVIEW_FRAME_SIZE = (480, 270)  # 16:9, matches every RESOLUTIONS option's aspect

_PREVIEW_SCENE = Scene(
    lines=[
        SceneLine(
            words=[SceneWord(text="Sample", word_active=True), SceneWord(text="lyric", word_active=False)],
            is_current=True,
            distance_from_current=0,
        ),
    ],
    image_key="settings-preview",
    ken_burns_progress=0.3,
)

_PREVIEW_CHORD_TRACK = ChordTrack(
    events=[ChordEvent(0.0, 4.0, "G"), ChordEvent(4.0, 8.0, "D"), ChordEvent(8.0, 12.0, "Am")],
    key="G major",
    bpm=120.0,
)
_PREVIEW_TIME = 1.0  # inside the first event -- NOW=G, NEXT=D


def render_preview_frame(settings: Settings, preview_size: tuple[int, int] = PREVIEW_FRAME_SIZE) -> Image.Image:
    """Draws the synthetic sample scene/chord track at the resolution `settings`
    actually specifies (so font/box sizes are proportionally correct), then
    downscales to `preview_size` for on-screen display."""
    render_kwargs = settings.render_kwargs()
    frame_size = render_kwargs["frame_size"]
    font_path = settings.font_path or default_font()

    background = Image.new("RGB", frame_size, (30, 30, 40))  # same fallback color assemble.py uses
    frame = draw_scene(
        _PREVIEW_SCENE, background, font_path,
        font_size=render_kwargs["lyric_size"], text_color=render_kwargs["text_color"], frame_size=frame_size,
    )
    frame = draw_chord_bar(
        frame, _PREVIEW_CHORD_TRACK, _PREVIEW_TIME, font_path,
        frame_size=frame_size, accent_color=render_kwargs["accent_color"],
        dim_text_color=render_kwargs["dim_text_color"], panel_color=render_kwargs["panel_color"],
        panel_alpha=render_kwargs["panel_alpha"], chord_now_size=render_kwargs["chord_now_size"],
        chord_next_size=render_kwargs["chord_next_size"], show_chord_timeline=render_kwargs["show_chord_timeline"],
        show_key_bpm=render_kwargs["show_key_bpm"], timeline_window_sec=render_kwargs["timeline_window_sec"],
    )
    current = current_chord_at(_PREVIEW_CHORD_TRACK, _PREVIEW_TIME)
    frame = draw_chord_legend(
        frame, ordered_unique_chords(_PREVIEW_CHORD_TRACK), current.label if current is not None else None,
        font_path, frame_size=frame_size, show_chord_legend=render_kwargs["show_chord_legend"],
        size_scale=render_kwargs["chord_legend_scale"],
        accent_color=render_kwargs["accent_color"], text_color=render_kwargs["text_color"],
        dim_text_color=render_kwargs["dim_text_color"], panel_color=render_kwargs["panel_color"],
    )
    if frame.size != preview_size:
        frame = frame.resize(preview_size, Image.LANCZOS)
    return frame


class SettingsPreviewFrame(ctk.CTkFrame):
    """Fixed-size pane showing the live preview frame. Widget construction stays
    manually/visually verified (this project's existing testing-constraint
    precedent for GUI code) -- render_preview_frame() above carries the real,
    unit-tested logic."""

    def __init__(self, master, settings: Settings, **kwargs):
        super().__init__(master, **kwargs)
        self._image_label = ctk.CTkLabel(self, text="")
        self._image_label.pack(padx=8, pady=8)
        self._current_image: ctk.CTkImage | None = None  # kept alive -- CTkLabel holds no reference of its own
        self.update_preview(settings)

    def update_preview(self, settings: Settings) -> None:
        frame = render_preview_frame(settings)
        self._current_image = ctk.CTkImage(light_image=frame, dark_image=frame, size=frame.size)
        self._image_label.configure(image=self._current_image)
