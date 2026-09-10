"""Owner-tunable settings, persisted per-user (not repo data). Ported in spirit from
LyricChord's config.py, trimmed to only the fields that map onto this program's own
pipeline -- see docs/superpowers/specs/2026-09-09-customtkinter-settings-gui-design.md
for the full owner-confirmed in/out-of-scope list."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger("playalongvideoproduction")

CONFIG_DIR = Path.home() / ".playalongvideoproduction"
CONFIG_FILE = CONFIG_DIR / "settings.json"

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "720p (1280x720)": (1280, 720),
    "1080p (1920x1080)": (1920, 1080),
    "1440p (2560x1440)": (2560, 1440),
}
ENCODERS = ["libx264", "libx265"]
FPS_OPTIONS = [24, 30, 60]


@dataclass
class Settings:
    # --- Output ---------------------------------------------------------
    resolution: str = "1080p (1920x1080)"
    fps: int = 24
    encoder: str = "libx264"
    crf: int = 20

    # --- Typography & colors (render.py) --------------------------------
    font_path: str = ""  # "" = auto-detect (today's pipeline.default_font())
    lyric_size: int = 48
    chord_now_size: int = 64
    chord_next_size: int = 32
    accent_color: str = "#38bdf8"
    text_color: str = "#ffffff"
    dim_text_color: str = "#94a3b8"
    panel_color: str = "#0b1220"
    panel_alpha: int = 150

    # --- Chord bar (render.py) -------------------------------------------
    show_chord_timeline: bool = True
    show_key_bpm: bool = True
    timeline_window_sec: float = 12.0
    show_chord_legend: bool = True
    chord_legend_size: int = 100  # percent -- 100 = default box size, scaled/shrunk from there

    # --- Chord detection (detect_chords.py) -------------------------------
    snap_chords_to_key: bool = True
    prefer_flats: bool = True
    include_seventh_chords: bool = False
    min_chord_seconds: float = 0.5

    # --- YouTube (youtube_schedule.py / gui.py) --------------------------
    youtube_auto_upload: bool = False
    youtube_client_secrets_path: str = ""
    youtube_privacy: str = "public"          # "public" | "unlisted" | "private"
    youtube_category_id: str = "26"          # YouTube's own category id -- 26 = "Howto & Style"
    youtube_made_for_kids: bool = False      # COPPA declaration, required on every upload
    youtube_min_days_between_uploads: int = 2
    youtube_preferred_upload_hour: int = 15  # 24h local time (0-23); 3 PM matches research
                                              # on peak engagement windows

    def render_kwargs(self) -> dict:
        """The subset of these settings that draw_scene()/draw_chord_bar() (via
        assemble_video()) actually take as plain keyword arguments -- resolution
        resolved to a real (width, height) and every color hex-decoded to RGB.
        Shared by run_pipeline() and the Settings preview so the two never drift
        out of sync on how a Settings object becomes render arguments."""
        return {
            "frame_size": RESOLUTIONS[self.resolution],
            "lyric_size": self.lyric_size,
            "text_color": hex_to_rgb(self.text_color),
            "accent_color": hex_to_rgb(self.accent_color),
            "dim_text_color": hex_to_rgb(self.dim_text_color),
            "panel_color": hex_to_rgb(self.panel_color),
            "panel_alpha": self.panel_alpha,
            "chord_now_size": self.chord_now_size,
            "chord_next_size": self.chord_next_size,
            "show_chord_timeline": self.show_chord_timeline,
            "show_key_bpm": self.show_key_bpm,
            "timeline_window_sec": self.timeline_window_sec,
            "show_chord_legend": self.show_chord_legend,
            "chord_legend_scale": self.chord_legend_size / 100.0,
        }

    def save(self, path: Path = CONFIG_FILE) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk issues
            log.warning("Could not save settings: %s", exc)

    @staticmethod
    def from_dict(data: dict) -> "Settings":
        """Tolerant of missing/unknown keys -- shared by load() and by the
        SettingsPanel's own value-collection, so there is exactly one place that
        decides how a flat dict becomes a Settings object."""
        valid = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in data.items() if k in valid})

    @staticmethod
    def load(path: Path = CONFIG_FILE) -> "Settings":
        if not path.exists():
            return Settings()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read settings (%s); using defaults", exc)
            return Settings()
        return Settings.from_dict(data)


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """'#38bdf8' or '38bdf8' -> (56, 189, 248). Falls back to white on anything
    unparseable rather than raising -- a color field must never crash a render."""
    v = (hex_color or "").strip().lstrip("#")
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except (ValueError, IndexError):
        return 255, 255, 255
