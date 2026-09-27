"""Owner-tunable settings, persisted per-user (not repo data). Ported in spirit from
LyricChord's config.py, trimmed to only the fields that map onto this program's own
pipeline -- see docs/superpowers/specs/2026-09-09-customtkinter-settings-gui-design.md
for the full owner-confirmed in/out-of-scope list."""

from __future__ import annotations

import json
import logging
import os
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
DEFAULT_RESOLUTION = "1080p (1920x1080)"
ENCODERS = ["libx264", "libx265"]
FPS_OPTIONS = [24, 30, 60]


@dataclass
class Settings:
    # --- Output ---------------------------------------------------------
    resolution: str = DEFAULT_RESOLUTION
    fps: int = 24
    encoder: str = "libx264"
    crf: int = 20
    countdown_beats: int = 4  # beat count-in before the song starts, tempo-synced to its own BPM; 0 disables it

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
    chord_diagram_panel_alpha: int = 235  # 0-255 -- near-opaque default, owner-tunable

    # --- Image pacing (layout.py / assemble.py) ---------------------------
    image_min_hold_seconds: float = 2.0  # instrumental stretch: a background image stays on
                                          # screen at least this long even if several chords
                                          # pass within that window -- swap points are still
                                          # always real chord onsets, never a fixed timer
    image_transition_seconds: float = 0.25  # crossfade length between any two background images
    lyric_preview_lead_seconds: float = 3.0  # no lyric text at all during an intro or a solo --
                                              # the current/next line only appears this many
                                              # seconds before vocals actually resume (owner
                                              # request, 2026-09-15: "in intro and solos no lyrics")

    # --- Support overlay/description (render.py / assemble.py / youtube_schedule.py) --
    # Deliberately two SEPARATE fields, not one shared string: the on-screen
    # overlay is never clickable, so its best wording is a short pointer
    # ("Support: link in description") -- but that same phrase in the
    # DESCRIPTION points at nothing, since the description IS where the
    # real, clickable URL needs to actually live (real mixup found live,
    # 2026-09-11: a shared field left 10 already-uploaded videos'
    # descriptions saying "link in description" with no real link anywhere).
    support_overlay_text: str = ""  # "" = the in-video overlay is disabled -- on-screen
                                     # wording only, e.g. "Support: link in description"
    support_overlay_size: int = 100  # percent -- 100 = default box size, matches chord_legend_size
    support_overlay_lead_seconds: float = 20.0  # the in-video overlay only shows during this
                                                 # many seconds before the song ends -- not the
                                                 # whole video (owner request, 2026-09-11: less
                                                 # intrusive, and catches a viewer near the end)
    support_description_text: str = ""  # "" = the YouTube description is left as the AI wrote it -- independent of
                                         # support_overlay_text. A small TEMPLATE (youtube_schedule.render_description):
                                         # text ABOVE the song description, the marker {description}, text BELOW;
                                         # with no marker the whole block goes below. Put the real https:// link in
                                         # it, e.g. "Tips: https://ko-fi.com/you\n{description}\nThank you!"

    # --- Chord detection (detect_chords.py) -------------------------------
    snap_chords_to_key: bool = True
    prefer_flats: bool = True
    include_seventh_chords: bool = False
    min_chord_seconds: float = 0.5

    # --- EASY CHORD capo videos (pipeline.build_capo_variant) -------------
    generate_easy_chord_versions: bool = False  # owner, 2026-09-23: when on, every future Generate/Redo/
                                                 # Batch run that lands in a hard key also builds a
                                                 # `<slug>-capo` EASY CHORD variant -- no extra AI/Replicate
                                                 # cost (images/audio are reused, only the render re-runs).
                                                 # Off by default, matching this app's existing convention
                                                 # for opt-in extra renders (e.g. Redo's "Generate new images").

    # --- Image library (library_session.py) --------------------------------
    use_image_library: bool = False  # owner, 2026-09-25: look in the shared library of already-bought images
                                      # before buying one from Replicate. Off until the owner has reviewed
                                      # scripts/preview_library_matches.py's contact sheet.
    image_library_min_score: float = 0.34  # CLIP text-to-image cosine similarity a library image needs to be
                                            # reused. PROVISIONAL -- 0.34 is where 3 songs' contact sheets looked
                                            # good (0.28 and below were clearly wrong); the owner's review sets it.

    # --- Quality check (timing_gate.py) -----------------------------------
    timing_pass_percent: int = 90            # a video is "good" when at least this share of its lyric lines start
                                              # within half a second of the singing; only good ones are offered for
                                              # upload (owner, 2026-09-20; raise it as the aligner improves)

    # --- YouTube (youtube_schedule.py / gui.py) --------------------------
    youtube_auto_upload: bool = False
    youtube_client_secrets_path: str = ""
    youtube_privacy: str = "public"          # "public" | "unlisted" | "private"
    youtube_category_id: str = "27"          # YouTube's own category id -- 27 = "Education" (owner
                                              # sets the "How-to" subcategory manually in Studio per
                                              # video -- that field only appears under Education, and
                                              # isn't reachable through the Data API at all)
    youtube_made_for_kids: bool = False      # COPPA declaration, required on every upload
    youtube_uploads_per_day: int = 5         # panel label "Maximum publish per day" -- drives the Settings
                                              # panel's auto-generated default times (see
                                              # youtube_schedule.evenly_spaced_upload_times)
    youtube_upload_times: str = "09:00,12:00,15:00,18:00,21:00"  # comma-separated 24h HH:MM local times,
                                              # panel label "Scheduled publish times" -- its own length IS
                                              # the videos-published-per-day count actually used at schedule
                                              # time; must match the default above (test_settings.py)
    youtube_max_uploads_per_day: int = 7     # panel label "Maximum uploads per day" -- (2026-09-18) a real enforced ceiling on raw
                                              # upload_video() calls per calendar day, to protect against
                                              # exhausting YouTube's daily quota -- see gui.py's
                                              # _uploads_remaining_today / youtube_upload_count_state.py.
                                              # Deliberately a SEPARATE field from youtube_uploads_per_day/
                                              # youtube_upload_times above, which control publish
                                              # scheduling only and are untouched by this cap -- a backlog
                                              # of already-uploaded, still-scheduled videos is fine.
    youtube_quota_retry_hours: int = 24      # once an upload hits YouTube's daily quota, how long to
                                              # wait before automatically retrying pending uploads

    def render_kwargs(self) -> dict:
        """The subset of these settings that draw_scene()/draw_chord_bar() (via
        assemble_video()) actually take as plain keyword arguments -- resolution
        resolved to a real (width, height) and every color hex-decoded to RGB.
        Shared by run_pipeline() and the Settings preview so the two never drift
        out of sync on how a Settings object becomes render arguments."""
        frame_size = RESOLUTIONS.get(self.resolution)
        if frame_size is None:
            # A hand-edited or stale settings.json label must never take down
            # every render AND the Settings window (which previews via this
            # same mapping) with a KeyError -- fall back to the default.
            log.warning("Unknown resolution %r in settings; using %s", self.resolution, DEFAULT_RESOLUTION)
            frame_size = RESOLUTIONS[DEFAULT_RESOLUTION]
        return {
            "frame_size": frame_size,
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
            "chord_diagram_panel_alpha": self.chord_diagram_panel_alpha,
            "min_hold_seconds": self.image_min_hold_seconds,
            "image_transition_seconds": self.image_transition_seconds,
            "lyric_preview_lead_seconds": self.lyric_preview_lead_seconds,
            "support_overlay_text": self.support_overlay_text,
            "support_overlay_scale": self.support_overlay_size / 100.0,
            "support_overlay_lead_seconds": self.support_overlay_lead_seconds,
        }

    def save(self, path: Path = CONFIG_FILE) -> None:
        """Writes settings.json ATOMICALLY: the JSON goes to a temp file in the same folder, is flushed to disk,
        then replaces the old file in one step -- a full disk or a crash mid-write leaves the previous
        settings.json untouched instead of empty (an empty file loads as all-defaults, silently losing every
        setting). RAISES OSError when the write fails, so the caller (Save Settings) can say so and keep its
        unsaved-changes markers instead of showing a save that never happened."""
        path = Path(path)
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(asdict(self), indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def from_dict(data: dict) -> "Settings":
        """Tolerant of missing/unknown keys AND of wrong-typed values -- shared by load() and by the
        SettingsPanel's own value-collection, so there is exactly one place that decides how a flat dict
        becomes a Settings object. A value that cannot be read as its field's type (a hand-edited
        `"lyric_size": null`, a string where a number belongs) falls back to that field's default with a
        warning, rather than reaching the Settings panel's Tk variables or a render as the wrong type."""
        if not isinstance(data, dict):
            log.warning("Settings data is not a JSON object (%s); using defaults", type(data).__name__)
            return Settings()
        defaults = Settings()
        values = {}
        for f in fields(Settings):
            if f.name not in data:
                continue
            value = _coerce_to_type_of(data[f.name], getattr(defaults, f.name))
            if value is _INVALID:
                log.warning("Ignoring settings value %s=%r (wrong type); using the default", f.name, data[f.name])
                continue
            values[f.name] = value
        return Settings(**values)

    @staticmethod
    def load(path: Path = CONFIG_FILE) -> "Settings":
        path = Path(path)
        if not path.exists():
            return Settings()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read settings (%s); using defaults", exc)
            return Settings()
        return Settings.from_dict(data)


_INVALID = object()


def _coerce_to_type_of(value, default):
    """`value` as the type of `default` (bool / int / float / str), or _INVALID. Lenient only where the meaning
    is unambiguous: an int for a float field, a whole-number float or numeric text for an int field, a number
    for a text field (e.g. a category id typed as 27), 0/1 for a checkbox."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        return _INVALID
    if isinstance(default, int):
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                return _INVALID
            return int(number) if number.is_integer() else _INVALID
        return _INVALID
    if isinstance(default, float):
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return _INVALID
        return _INVALID
    if isinstance(default, str):
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return _INVALID
    return value


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """'#38bdf8' or '38bdf8' -> (56, 189, 248). Falls back to white on anything
    unparseable rather than raising -- a color field must never crash a render."""
    v = (hex_color or "").strip().lstrip("#")
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except (ValueError, IndexError):
        return 255, 255, 255
