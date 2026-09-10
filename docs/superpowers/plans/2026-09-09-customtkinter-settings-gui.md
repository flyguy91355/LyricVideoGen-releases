# CustomTkinter GUI + Settings Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild PlayAlongVideoProduction's GUI on CustomTkinter with a Settings
panel matching LyricChord's visual style, wired to a new `Settings` dataclass that
plumbs real tunables (chord-bar colors/sizes/toggles, chord-detection knobs, output
resolution/fps/encoder/crf) into the existing render/detect/assemble/pipeline code.

**Architecture:** `settings.py` holds the dataclass + JSON persistence
(`~/.playalongvideoproduction/settings.json`). `render.py`, `detect_chords.py`, and
`assemble_video()` each gain plain keyword parameters (not a `Settings` object, to
stay decoupled/independently testable) defaulting to today's exact hardcoded values.
`assemble_video()` and `run_pipeline()` are the two integration points that accept a
real `Settings` object and unpack it into those keyword arguments. `gui.py` is
rebuilt on CustomTkinter: left column is the existing single-song form restyled,
right column is a new scrollable `SettingsPanel` bound to `Settings`, plus a new
GUI-side generation-progress bar.

**Tech Stack:** Python 3.12, `customtkinter` (new dependency), existing PIL/moviepy
render stack, pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-customtkinter-settings-gui-design.md`

## Global Constraints

- Every new parameter on `draw_scene`/`draw_chord_bar`/`detect_chords`/
  `assemble_video`/`run_pipeline` defaults to today's exact hardcoded value — calling
  any of them with no new arguments must reproduce identical behavior to before this
  feature (verified by tests, not just believed).
- `render.py`/`detect_chords.py` take plain keyword arguments (colors as `(r,g,b)`
  tuples, sizes as `int`, toggles as `bool`) — they must NOT import from
  `settings.py`. Only `assemble_video()`/`run_pipeline()` accept a `Settings` object.
- `Settings.load()` must tolerate a missing file (return defaults) and a file with
  unknown/missing keys (same forward-compatibility discipline as
  `models.py`'s `_song_from_dict` from the previous merge).
- Every `Path.read_text()`/`.write_text()` call passes `encoding="utf-8"` explicitly
  (existing codebase convention).
- Out of scope, do not implement: LyricChord's Background section (solid/gradient/
  loop-video), batch-queue fields (`skip_existing`, `scan_subfolders`, drag-drop file
  queue), `lyrics_offset_ms`, `use_online_chords`, `acoustid_api_key`, `karaoke_fill`,
  a video-baked playback progress bar, and theme presets/light-dark appearance toggle.
- Run `cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/ -v`
  after every task; all tests must pass before committing. This repo's pre-commit
  hook blocks any commit touching a `.py`/`.sh` file unless `CLAUDE.md` is also
  staged with a real change — see the note in Task 9, but a minimal accurate
  in-progress note must accompany every task's commit, same as the previous merge.

---

## Task 1: Add the customtkinter dependency

**Files:**
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `customtkinter` importable in the venv for later tasks.

- [ ] **Step 1: Add the dependency**

Add this line to `requirements.txt` (near the top, in its own small group):

```
customtkinter>=5.2
```

- [ ] **Step 2: Install into the venv**

Run: `cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pip install -r requirements.txt`
Expected: `customtkinter` installs successfully. Confirm with:

```bash
.venv/bin/python -c "import customtkinter; print('ok')"
```

Expected output: `ok`

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all 204 tests still pass.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Add customtkinter for the GUI rebuild"
```

(This commit touches no `.py` file, so the CLAUDE.md pre-commit hook does not
trigger — confirm with `git commit` directly; if it somehow does trigger, see
Task 9's pattern for the minimal accompanying note.)

---

## Task 2: Settings dataclass and persistence

**Files:**
- Create: `lyricvideo/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `Settings` dataclass (fields listed below) with `.save(path=CONFIG_FILE)`,
  `Settings.load(path=CONFIG_FILE) -> Settings`, and `Settings.from_dict(data: dict)
  -> Settings` (all staticmethods — `from_dict` does the actual tolerant-of-missing/
  unknown-keys construction; `load()` delegates to it; Task 7's `SettingsPanel` calls
  it directly when collecting values from its widgets, so there is exactly one place
  that decides how a flat dict becomes a `Settings`), module constants `CONFIG_DIR`,
  `CONFIG_FILE`, `RESOLUTIONS: dict[str, tuple[int,int]]`, `ENCODERS: list[str]`,
  `FPS_OPTIONS: list[int]`, and `hex_to_rgb(hex_color: str) -> tuple[int,int,int]`
  (a `Settings` color field is stored as a hex string — matching what a CustomTkinter
  color-picker returns and LyricChord's own convention — but `render.py`'s functions
  take RGB tuples; this is the one conversion point, used by Task 6's `run_pipeline`).
- Consumes: nothing from this codebase (`json`, `dataclasses`, `pathlib` only).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_settings.py`:

```python
import json

from lyricvideo.settings import ENCODERS, FPS_OPTIONS, RESOLUTIONS, Settings


def test_settings_defaults_match_current_hardcoded_render_behavior():
    s = Settings()
    assert s.resolution == "1080p (1920x1080)"
    assert RESOLUTIONS[s.resolution] == (1920, 1080)
    assert s.fps == 24
    assert s.encoder == "libx264"
    assert s.crf == 20
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.settings'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/settings.py`:

```python
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
    font_path: str = ""  # "" = auto-detect (today's _default_font())
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

    # --- Chord detection (detect_chords.py) -------------------------------
    snap_chords_to_key: bool = True
    prefer_flats: bool = True
    include_seventh_chords: bool = False
    min_chord_seconds: float = 0.5

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_settings.py -v`
Expected: 11 passed

- [ ] **Step 5: Add a minimal CLAUDE.md note and commit**

Add this sentence to CLAUDE.md's existing "Pipeline stages" section intro (or any
accurate current-state spot) — a small, real, incremental note (the same
"in-progress tracking" pattern used throughout the previous merge, replaced by a
full rewrite in Task 9):

```
`lyricvideo/settings.py` (new) holds an owner-tunable `Settings` dataclass,
persisted to `~/.playalongvideoproduction/settings.json` -- not yet wired into
render.py/detect_chords.py/assemble.py/pipeline.py/gui.py (in progress; see
docs/superpowers/plans/2026-09-09-customtkinter-settings-gui.md).
```

```bash
git add lyricvideo/settings.py tests/test_settings.py CLAUDE.md
git commit -m "Add Settings dataclass with JSON persistence"
```

---

## Task 3: render.py — configurable colors, sizes, and resolution-aware geometry

Today, `FRAME_SIZE = (1920, 1080)` and every color/size (`ACCENT_COLOR`,
`DIM_TEXT_COLOR`, `PANEL_FILL`, `CHORD_BOX`/`NOW_BOX`/`NEXT_BOX`/`LANE_BOX`, etc.) are
module-level constants computed once at import time. This task adds a
`frame_size`/color/size parameter to each public function (default = today's exact
constant), and extracts the chord-bar box geometry into a `compute_chord_bar_layout()`
function so a non-default resolution gets correct geometry too — the *default* path
still uses the pre-computed module constants directly (zero behavior change,
zero extra per-frame computation, when no `Settings` override is in play).

**Files:**
- Modify: `lyricvideo/render.py`
- Modify: `tests/test_render.py`

**Interfaces:**
- Produces: `compute_chord_bar_layout(frame_size: tuple[int,int]) -> dict[str, tuple[int,int,int,int]]`
  (keys `"chord_box"`, `"now_box"`, `"next_box"`, `"lane_box"`, `"badge_xy"`);
  `apply_ken_burns(image, progress, ..., frame_size=FRAME_SIZE)`;
  `draw_scene(scene, background, font_path, font_size=48, text_color=(255,255,255), frame_size=FRAME_SIZE)`;
  `draw_chord_bar(frame, chord_track, t, font_path, *, frame_size=FRAME_SIZE, accent_color=ACCENT_COLOR, dim_text_color=DIM_TEXT_COLOR, panel_color=(11,18,32), panel_alpha=150, chord_now_size=64, chord_next_size=32, show_chord_timeline=True, show_key_bpm=True, timeline_window_sec=12.0)`.
- Consumes: `lyricvideo.layout.Scene`/`SceneLine` (unchanged),
  `lyricvideo.models.ChordTrack`/`current_chord_at`/`next_chord_after` (unchanged).

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_render.py` in full:

```python
import numpy as np
from PIL import Image

from lyricvideo.render import (
    CHORD_BOX, FRAME_SIZE, KEN_BURNS_PRESETS,
    apply_ken_burns, compute_chord_bar_layout, draw_chord_bar, draw_scene,
    ken_burns_preset_for_key,
)
from lyricvideo.layout import Scene, SceneLine, SceneWord
from lyricvideo.models import ChordEvent, ChordTrack


def test_apply_ken_burns_returns_frame_sized_image():
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    out_start = apply_ken_burns(img, progress=0.0)
    out_end = apply_ken_burns(img, progress=1.0)
    assert out_start.size == FRAME_SIZE
    assert out_end.size == FRAME_SIZE


def test_apply_ken_burns_respects_custom_frame_size():
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    out = apply_ken_burns(img, progress=0.5, frame_size=(1280, 720))
    assert out.size == (1280, 720)


def test_apply_ken_burns_pans_toward_end_position():
    img = Image.new("RGB", (800, 600))
    for x in range(800):
        for y in range(0, 600, 50):
            img.paste((x % 256, 0, 0), (x, y, x + 1, y + 50))

    start = np.array(apply_ken_burns(img, progress=0.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    end = np.array(apply_ken_burns(img, progress=1.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    assert not np.array_equal(start, end)


def test_apply_ken_burns_can_zoom_out():
    img = Image.new("RGB", (800, 600), (50, 50, 50))
    tight = apply_ken_burns(img, progress=0.0, zoom_start=1.2, zoom_end=1.0)
    wide = apply_ken_burns(img, progress=1.0, zoom_start=1.2, zoom_end=1.0)
    assert tight.size == wide.size == FRAME_SIZE


def test_ken_burns_preset_for_key_is_deterministic():
    assert ken_burns_preset_for_key("same-key") == ken_burns_preset_for_key("same-key")


def test_ken_burns_preset_for_key_varies_across_keys():
    presets_seen = {ken_burns_preset_for_key(f"key-{i}") for i in range(len(KEN_BURNS_PRESETS) * 3)}
    assert len(presets_seen) > 1


def test_draw_scene_renders_without_error_and_draws_text(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    scene = Scene(
        lines=[
            SceneLine(
                words=[SceneWord(text="hello"), SceneWord(text="there")],
                is_current=True,
                distance_from_current=0,
            ),
        ],
        image_key="abc123",
        ken_burns_progress=0.5,
    )

    frame = draw_scene(scene, bg, test_font_path)

    assert frame.size == FRAME_SIZE
    assert frame.getextrema() != ((0, 0), (0, 0), (0, 0))


def test_draw_scene_scroll_progress_shifts_line_position(test_font_path):
    def make_scene(scroll_progress):
        return Scene(
            lines=[
                SceneLine(words=[SceneWord(text="hello")], is_current=True, distance_from_current=0),
            ],
            image_key="abc123",
            ken_burns_progress=0.0,
            scroll_progress=scroll_progress,
        )

    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    frame_at_start = np.array(draw_scene(make_scene(0.0), bg, test_font_path))
    frame_at_mid = np.array(draw_scene(make_scene(0.5), bg, test_font_path))

    assert not np.array_equal(frame_at_start, frame_at_mid)


def test_draw_scene_respects_custom_text_color(test_font_path):
    scene = Scene(
        lines=[SceneLine(words=[SceneWord(text="hi")], is_current=True, distance_from_current=0)],
        image_key="k", ken_burns_progress=0.0,
    )
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))

    default_frame = np.array(draw_scene(scene, bg, test_font_path))
    red_frame = np.array(draw_scene(scene, bg, test_font_path, text_color=(255, 0, 0)))

    assert not np.array_equal(default_frame, red_frame)


def test_draw_scene_respects_custom_frame_size(test_font_path):
    scene = Scene(
        lines=[SceneLine(words=[SceneWord(text="hi")], is_current=True, distance_from_current=0)],
        image_key="k", ken_burns_progress=0.0,
    )
    bg = Image.new("RGB", (1280, 720), (0, 0, 0))

    frame = draw_scene(scene, bg, test_font_path, frame_size=(1280, 720))

    assert frame.size == (1280, 720)


def test_compute_chord_bar_layout_scales_with_frame_size():
    default_layout = compute_chord_bar_layout(FRAME_SIZE)
    small_layout = compute_chord_bar_layout((1280, 720))

    assert default_layout["chord_box"] == CHORD_BOX
    assert small_layout["chord_box"] != CHORD_BOX
    assert all(0 <= c <= 1280 for c in (small_layout["chord_box"][0], small_layout["chord_box"][2]))


def test_draw_chord_bar_renders_without_error(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G")], key="C major", bpm=120.0)

    frame = draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path)

    assert frame.size == FRAME_SIZE
    assert frame.crop(CHORD_BOX).getextrema() != ((20, 20), (20, 20), (20, 20))


def test_draw_chord_bar_shows_dash_when_no_current_chord(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(5.0, 7.0, "C")])

    frame = draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path)

    assert frame.size == FRAME_SIZE


def test_draw_chord_bar_does_not_mutate_input_frame(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")])

    draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path)

    assert bg.getextrema() == ((20, 20), (20, 20), (20, 20))


def test_draw_chord_bar_respects_custom_accent_color(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")])

    default_frame = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path))
    red_frame = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, accent_color=(255, 0, 0)))

    assert not np.array_equal(default_frame, red_frame)


def test_draw_chord_bar_hides_timeline_lane_when_disabled(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 20.0, "G")])

    shown = draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_chord_timeline=True)
    hidden = draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_chord_timeline=False)

    layout = compute_chord_bar_layout(FRAME_SIZE)
    lane_box = layout["lane_box"]
    # With the lane hidden, that region must still show the plain panel background,
    # not a chord segment block -- so it must differ from the shown-lane version.
    assert not np.array_equal(np.array(shown.crop(lane_box)), np.array(hidden.crop(lane_box)))


def test_draw_chord_bar_hides_key_bpm_badge_when_disabled(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")], key="C major", bpm=120.0)

    shown = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_key_bpm=True))
    hidden = np.array(draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path, show_key_bpm=False))

    assert not np.array_equal(shown, hidden)


def test_draw_chord_bar_respects_custom_timeline_window(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    # A chord far enough out that only a wide window includes any of it.
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G"), ChordEvent(20.0, 22.0, "Am")])

    narrow = np.array(draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path, timeline_window_sec=3.0))
    wide = np.array(draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path, timeline_window_sec=25.0))

    assert not np.array_equal(narrow, wide)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_render.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_chord_bar_layout' from 'lyricvideo.render'`

- [ ] **Step 3: Write the implementation**

Replace `lyricvideo/render.py` in full:

```python
from __future__ import annotations

import hashlib

from PIL import Image, ImageDraw, ImageFont

from .layout import Scene, SceneLine
from .models import ChordTrack, current_chord_at, next_chord_after

FRAME_SIZE = (1920, 1080)

CURRENT_LINE_UNSUNG_COLOR = (255, 255, 255)  # white -- default text_color
WORD_HIGHLIGHT_BG_COLOR = (46, 204, 113)     # bright green -- box behind already-sung words
WORD_HIGHLIGHT_TEXT_COLOR = (255, 255, 255)  # white text on top of the highlight box
TEXT_STROKE_COLOR = (0, 0, 0)                # black outline so text reads over any background
TEXT_STROKE_WIDTH = 3

# Chord bar geometry at the default FRAME_SIZE (landscape), ported from LyricChord's
# compute_layout()'s landscape branch (frames.py). Kept as module constants for the
# default (zero-plumbing, zero-recompute) path; compute_chord_bar_layout() below
# derives the same shape for any other frame_size.
_CHORD_BAR_MARGIN = 120
_CHORD_BAR_PAD = 20
CHORD_BOX = (
    _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.665),
    FRAME_SIZE[0] - _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.925),
)
_inner_h = CHORD_BOX[3] - CHORD_BOX[1] - 2 * _CHORD_BAR_PAD
NOW_BOX = (
    CHORD_BOX[0] + _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD,
    CHORD_BOX[0] + _CHORD_BAR_PAD + int(FRAME_SIZE[0] * 0.19), CHORD_BOX[3] - _CHORD_BAR_PAD,
)
NEXT_BOX = (
    NOW_BOX[2] + _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD + int(_inner_h * 0.15),
    NOW_BOX[2] + _CHORD_BAR_PAD + int(FRAME_SIZE[0] * 0.13), CHORD_BOX[3] - _CHORD_BAR_PAD - int(_inner_h * 0.15),
)
LANE_BOX = (
    NEXT_BOX[2] + 2 * _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD + int(_inner_h * 0.2),
    CHORD_BOX[2] - _CHORD_BAR_PAD, CHORD_BOX[3] - _CHORD_BAR_PAD - int(_inner_h * 0.2),
)
KEY_BPM_BADGE_XY = (FRAME_SIZE[0] - _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.06))

PANEL_ALPHA_DEFAULT = 150
BOX_FILL = (30, 41, 59, 235)       # NOW/NEXT/lane box fill (not owner-configurable)
ACCENT_COLOR = (56, 189, 248)      # current-chord highlight
DIM_TEXT_COLOR = (148, 163, 184)
LANE_BLOCK_COLOR = (51, 65, 85, 235)
TIMELINE_WINDOW_SECONDS = 12.0

# (start_x, start_y, end_x, end_y, zoom_start, zoom_end) -- x/y are 0..1
# fractions of the available pan range (0.5 = centered).
KEN_BURNS_PRESETS: list[tuple[float, float, float, float, float, float]] = [
    (0.0, 0.0, 1.0, 1.0, 1.0, 1.15),
    (1.0, 1.0, 0.0, 0.0, 1.0, 1.15),
    (0.0, 1.0, 1.0, 0.0, 1.0, 1.15),
    (1.0, 0.0, 0.0, 1.0, 1.0, 1.15),
    (0.0, 0.5, 1.0, 0.5, 1.0, 1.15),
    (1.0, 0.5, 0.0, 0.5, 1.0, 1.15),
    (0.5, 0.0, 0.5, 1.0, 1.0, 1.15),
    (0.5, 1.0, 0.5, 0.0, 1.0, 1.15),
    (0.5, 0.5, 0.5, 0.5, 1.18, 1.0),
]


def ken_burns_preset_for_key(image_key: str) -> tuple[float, float, float, float, float, float]:
    idx = int(hashlib.sha256(image_key.encode("utf-8")).hexdigest(), 16) % len(KEN_BURNS_PRESETS)
    return KEN_BURNS_PRESETS[idx]


def apply_ken_burns(
    image: Image.Image,
    progress: float,
    start_x: float = 0.0,
    start_y: float = 0.0,
    end_x: float = 1.0,
    end_y: float = 1.0,
    zoom_start: float = 1.0,
    zoom_end: float = 1.15,
    frame_size: tuple[int, int] = FRAME_SIZE,
) -> Image.Image:
    img = image.resize(frame_size)
    zoom = zoom_start + (zoom_end - zoom_start) * progress
    w, h = img.size
    new_w, new_h = max(int(w * zoom), w), max(int(h * zoom), h)
    img = img.resize((new_w, new_h))
    max_dx, max_dy = new_w - w, new_h - h
    frac_x = min(max(start_x + (end_x - start_x) * progress, 0.0), 1.0)
    frac_y = min(max(start_y + (end_y - start_y) * progress, 0.0), 1.0)
    x = int(max_dx * frac_x)
    y = int(max_dy * frac_y)
    return img.crop((x, y, x + w, y + h))


def _draw_line_words(draw, y, scene_line: SceneLine, font, is_current: bool, text_color) -> None:
    words = scene_line.words
    if not words:
        return
    space_w = draw.textlength(" ", font=font)
    widths = [draw.textlength(w.text, font=font) for w in words]
    total_w = sum(widths) + space_w * max(len(words) - 1, 0)
    x = (draw.im.size[0] - total_w) / 2
    ascent, descent = font.getmetrics()
    text_height = ascent + descent

    for word, w_width in zip(words, widths):
        # Karaoke-style sweep: a highlight box appears behind each word the
        # instant it's actually sung (real per-word timing from forced
        # alignment), moving left to right through the line in sync with the
        # vocals -- not yet-sung words stay plain. Not owner-configurable
        # (already-tuned mechanism, see the 2026-09-09 GUI-design spec).
        if is_current and word.word_active:
            pad_x, pad_y = 6, 4
            draw.rectangle(
                [x - pad_x, y - pad_y, x + w_width + pad_x, y + text_height + pad_y],
                fill=WORD_HIGHLIGHT_BG_COLOR,
            )
            text_fill = WORD_HIGHLIGHT_TEXT_COLOR
        else:
            text_fill = text_color

        draw.text(
            (x, y), word.text, font=font, fill=text_fill,
            stroke_width=TEXT_STROKE_WIDTH, stroke_fill=TEXT_STROKE_COLOR,
        )
        x += w_width + space_w


def draw_scene(
    scene: Scene,
    background: Image.Image,
    font_path: str,
    font_size: int = 48,
    text_color: tuple[int, int, int] = CURRENT_LINE_UNSUNG_COLOR,
    frame_size: tuple[int, int] = FRAME_SIZE,
) -> Image.Image:
    frame = background.copy()
    draw = ImageDraw.Draw(frame)
    font = ImageFont.truetype(font_path, font_size)

    center_y = frame_size[1] // 2
    ascent, descent = font.getmetrics()
    line_height = (ascent + descent) + 20

    for sl in scene.lines:
        # Continuous position, not a fixed per-line step: every line drifts
        # upward by scroll_progress (0->1 across the current line's own real
        # start->end window) so the transition to the next line is a smooth
        # scroll instead of a snap.
        y = center_y + (sl.distance_from_current - scene.scroll_progress) * line_height
        _draw_line_words(draw, y, sl, font, sl.is_current, text_color)

    return frame


def _display_chord_label(label: str) -> str:
    return "N.C." if label == "N" else label


def compute_chord_bar_layout(frame_size: tuple[int, int]) -> dict[str, tuple[int, int, int, int]]:
    """Chord-bar box geometry (chord_box/now_box/next_box/lane_box/badge_xy) for any
    frame_size, landscape-only (this program always renders landscape). At the
    default FRAME_SIZE this reproduces CHORD_BOX/NOW_BOX/NEXT_BOX/LANE_BOX exactly."""
    w, h = frame_size
    margin, pad = _CHORD_BAR_MARGIN, _CHORD_BAR_PAD
    chord_box = (margin, int(h * 0.665), w - margin, int(h * 0.925))
    inner_h = chord_box[3] - chord_box[1] - 2 * pad
    now_box = (
        chord_box[0] + pad, chord_box[1] + pad,
        chord_box[0] + pad + int(w * 0.19), chord_box[3] - pad,
    )
    next_box = (
        now_box[2] + pad, chord_box[1] + pad + int(inner_h * 0.15),
        now_box[2] + pad + int(w * 0.13), chord_box[3] - pad - int(inner_h * 0.15),
    )
    lane_box = (
        next_box[2] + 2 * pad, chord_box[1] + pad + int(inner_h * 0.2),
        chord_box[2] - pad, chord_box[3] - pad - int(inner_h * 0.2),
    )
    badge_xy = (w - margin, int(h * 0.06))
    return {"chord_box": chord_box, "now_box": now_box, "next_box": next_box,
            "lane_box": lane_box, "badge_xy": badge_xy}


def draw_chord_bar(
    frame: Image.Image,
    chord_track: ChordTrack,
    t: float,
    font_path: str,
    *,
    frame_size: tuple[int, int] = FRAME_SIZE,
    accent_color: tuple[int, int, int] = ACCENT_COLOR,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR,
    panel_color: tuple[int, int, int] = (11, 18, 32),
    panel_alpha: int = PANEL_ALPHA_DEFAULT,
    chord_now_size: int = 64,
    chord_next_size: int = 32,
    show_chord_timeline: bool = True,
    show_key_bpm: bool = True,
    timeline_window_sec: float = TIMELINE_WINDOW_SECONDS,
) -> Image.Image:
    """Composites the NOW/NEXT/timeline chord bar and the Key/BPM badge onto
    `frame`, ported from LyricChord's FrameComposer._draw_chords + its header
    badge. Independent of which lyric line/word is on screen -- looked up
    directly from chord_track by playback time t. Returns a new image; `frame`
    is not mutated (matches draw_scene's own copy-on-write style)."""
    layout = CHORD_BOX_LAYOUT_DEFAULT if frame_size == FRAME_SIZE else compute_chord_bar_layout(frame_size)
    chord_box, now_box, next_box, lane_box, badge_xy = (
        layout["chord_box"], layout["now_box"], layout["next_box"], layout["lane_box"], layout["badge_xy"],
    )

    label_font = ImageFont.truetype(font_path, 20)
    now_font = ImageFont.truetype(font_path, chord_now_size)
    next_font = ImageFont.truetype(font_path, chord_next_size)
    small_font = ImageFont.truetype(font_path, 24)
    lane_font = ImageFont.truetype(font_path, 30)

    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    panel_fill = (*panel_color, panel_alpha)
    draw.rounded_rectangle(chord_box, radius=24, fill=panel_fill)
    draw.rounded_rectangle(now_box, radius=18, fill=BOX_FILL)
    draw.rounded_rectangle(next_box, radius=16, fill=BOX_FILL)
    if show_chord_timeline:
        draw.rounded_rectangle(lane_box, radius=12, fill=BOX_FILL)

    draw.text((now_box[0] + 16, now_box[1] + 10), "NOW", font=label_font, fill=dim_text_color)
    draw.text((next_box[0] + 14, next_box[1] + 8), "NEXT", font=label_font, fill=dim_text_color)

    current = current_chord_at(chord_track, t)
    next_event = next_chord_after(chord_track, t)

    now_label = _display_chord_label(current.label) if current else "—"
    now_color = accent_color if current else dim_text_color
    now_w = draw.textlength(now_label, font=now_font)
    draw.text(
        ((now_box[0] + now_box[2]) / 2 - now_w / 2, (now_box[1] + now_box[3]) / 2 - chord_now_size / 2),
        now_label, font=now_font, fill=now_color,
    )

    if next_event is not None:
        next_label = _display_chord_label(next_event.label)
        next_w = draw.textlength(next_label, font=next_font)
        draw.text(
            ((next_box[0] + next_box[2]) / 2 - next_w / 2, next_box[1] + 34),
            next_label, font=next_font, fill=(248, 250, 252, 255),
        )
        eta = f"in {max(0.0, next_event.start - t):.1f}s"
        eta_w = draw.textlength(eta, font=small_font)
        draw.text(
            ((next_box[0] + next_box[2]) / 2 - eta_w / 2, next_box[3] - 30),
            eta, font=small_font, fill=dim_text_color,
        )

    if show_chord_timeline:
        lx0, ly0, lx1, ly1 = lane_box
        pps = (lx1 - lx0) / timeline_window_sec
        for event in chord_track.events:
            if event.start >= t + timeline_window_sec:
                break
            if event.end <= t:
                continue
            bx0 = lx0 + max(0.0, event.start - t) * pps
            bx1 = lx0 + min(timeline_window_sec, event.end - t) * pps
            if bx1 - bx0 < 2:
                continue
            is_current = current is not None and event is current
            fill = (*accent_color, 255) if is_current else LANE_BLOCK_COLOR
            draw.rounded_rectangle((int(bx0) + 1, ly0 + 6, int(bx1) - 1, ly1 - 6), radius=10, fill=fill)
            label = _display_chord_label(event.label)
            text_color = (11, 18, 32, 255) if is_current else (248, 250, 252, 255)
            label_w = draw.textlength(label, font=lane_font)
            if label_w + 16 <= (bx1 - bx0):
                draw.text(((bx0 + bx1) / 2 - label_w / 2, (ly0 + ly1) / 2 - 15), label, font=lane_font, fill=text_color)

    if show_key_bpm and (chord_track.key or chord_track.bpm):
        parts = []
        if chord_track.key:
            parts.append(f"Key: {chord_track.key}")
        if chord_track.bpm:
            parts.append(f"{int(round(chord_track.bpm))} BPM")
        badge = "   ·   ".join(parts)
        bx, by = badge_xy
        badge_w = draw.textlength(badge, font=small_font)
        draw.text((bx - badge_w, by), badge, font=small_font, fill=(*accent_color, 255))

    composited = Image.alpha_composite(frame.convert("RGBA"), overlay)
    return composited.convert("RGB")


CHORD_BOX_LAYOUT_DEFAULT = {
    "chord_box": CHORD_BOX, "now_box": NOW_BOX, "next_box": NEXT_BOX,
    "lane_box": LANE_BOX, "badge_xy": KEY_BPM_BADGE_XY,
}
```

Note two deliberate details: `_draw_line_words` now reads the frame width from
`draw.im.size[0]` instead of the module-level `FRAME_SIZE[0]`, so it centers text
correctly under any `frame_size` passed to `draw_scene` without needing its own
extra parameter. `CHORD_BOX_LAYOUT_DEFAULT` is defined at the bottom (after
`CHORD_BOX`/`NOW_BOX`/etc. already exist) purely so `draw_chord_bar` can reference
one dict either way without an `if/else` branch scattered through the function body.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_render.py -v`
Expected: 17 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/render.py tests/test_render.py CLAUDE.md
git commit -m "render.py: configurable colors/sizes/toggles and resolution-aware geometry"
```

(Append one accurate sentence to the same in-progress note from Task 2 first —
"`render.py`'s `draw_scene`/`draw_chord_bar` now accept color/size/toggle/frame_size
overrides; `assemble.py`/`detect_chords.py`/`pipeline.py`/`gui.py` still on old
wiring." — before staging CLAUDE.md, to satisfy the pre-commit hook honestly.)

---

## Task 4: detect_chords.py — configurable chord-detection tuning

**Files:**
- Modify: `lyricvideo/detect_chords.py`
- Modify: `tests/test_detect_chords.py`

**Interfaces:**
- Produces: `detect_chords(path, *, snap_chords_to_key=True, prefer_flats=True, include_seventh_chords=False, min_chord_seconds=0.5) -> ChordTrack`
  (all four defaults exactly match today's module constants).

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_detect_chords.py` (keep the existing 5 tests, add
these after them):

```python
def test_detect_chords_include_seventh_chords_can_produce_seventh_labels(tmp_path):
    # A synthesized dominant-7th-ish chord: root + major third + fifth + minor
    # seventh, all mixed together. With sevenths disabled the detector can only
    # ever output a plain triad label; with them enabled a "7"/"m7"/"maj7"
    # suffix becomes possible. This test only asserts the flag is actually wired
    # through (no crash, real ChordTrack back either way) -- exact chord-guessing
    # accuracy on synthetic audio is not what's being tested here.
    wav_path = tmp_path / "chord.wav"
    import subprocess

    from lyricvideo.audio_decode import find_ffmpeg

    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=220:duration=6", "-ar", "22050", str(wav_path)],
        check=True,
    )

    from lyricvideo.detect_chords import detect_chords

    with_sevenths = detect_chords(wav_path, include_seventh_chords=True)
    without_sevenths = detect_chords(wav_path, include_seventh_chords=False)

    assert isinstance(with_sevenths, ChordTrack)
    assert isinstance(without_sevenths, ChordTrack)


def test_detect_chords_min_chord_seconds_merges_short_segments(tmp_path):
    import subprocess

    from lyricvideo.audio_decode import find_ffmpeg

    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-ar", "22050", str(wav_path)],
        check=True,
    )

    from lyricvideo.detect_chords import detect_chords

    loose = detect_chords(wav_path, min_chord_seconds=0.0)
    strict = detect_chords(wav_path, min_chord_seconds=5.0)

    # A near-total merge threshold (5s on a 6s clip) can only ever produce the
    # same or fewer segments than no merging at all.
    assert len(strict.events) <= len(loose.events)


def test_detect_chords_defaults_match_module_constants(tmp_path):
    """Calling with no keyword overrides must behave exactly like the hardcoded
    pre-Settings version -- this is the backward-compatibility contract the whole
    feature depends on."""
    import subprocess

    from lyricvideo.audio_decode import find_ffmpeg
    from lyricvideo.detect_chords import (
        INCLUDE_SEVENTH_CHORDS, MIN_CHORD_SECONDS, PREFER_FLATS, SNAP_CHORDS_TO_KEY, detect_chords,
    )

    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-ar", "22050", str(wav_path)],
        check=True,
    )

    default_call = detect_chords(wav_path)
    explicit_call = detect_chords(
        wav_path, snap_chords_to_key=SNAP_CHORDS_TO_KEY, prefer_flats=PREFER_FLATS,
        include_seventh_chords=INCLUDE_SEVENTH_CHORDS, min_chord_seconds=MIN_CHORD_SECONDS,
    )

    assert default_call == explicit_call
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_detect_chords.py -v`
Expected: FAIL — `TypeError: detect_chords() got an unexpected keyword argument
'include_seventh_chords'`

- [ ] **Step 3: Update the implementation**

In `lyricvideo/detect_chords.py`, replace the `detect_chords` function signature and
its three usages of the now-parameterized module constants. Find:

```python
def detect_chords(path: Path) -> ChordTrack:
    """Analyse an audio file and return its ChordTrack."""
    import librosa  # imported lazily: slow to import, only needed here
```

Replace with:

```python
def detect_chords(
    path: Path,
    *,
    snap_chords_to_key: bool = SNAP_CHORDS_TO_KEY,
    prefer_flats: bool = PREFER_FLATS,
    include_seventh_chords: bool = INCLUDE_SEVENTH_CHORDS,
    min_chord_seconds: float = MIN_CHORD_SECONDS,
) -> ChordTrack:
    """Analyse an audio file and return its ChordTrack."""
    import librosa  # imported lazily: slow to import, only needed here
```

Find:

```python
    # 4) Template matching.
    qualities = theory.TRIADS + (theory.SEVENTHS if INCLUDE_SEVENTH_CHORDS else [])
    chords, templates = theory.build_templates(qualities)
    sim = templates @ seg_chroma

    tonic, mode, _key_conf = theory.estimate_key(chroma.mean(axis=1))
    if SNAP_CHORDS_TO_KEY:
        diatonic = theory.diatonic_chords(tonic, mode, include_sevenths=True)
        prior = np.array([1.0 if c in diatonic else NON_DIATONIC_PENALTY for c in chords])
        sim = sim * prior[:, None]
```

Replace with:

```python
    # 4) Template matching.
    qualities = theory.TRIADS + (theory.SEVENTHS if include_seventh_chords else [])
    chords, templates = theory.build_templates(qualities)
    sim = templates @ seg_chroma

    tonic, mode, _key_conf = theory.estimate_key(chroma.mean(axis=1))
    if snap_chords_to_key:
        diatonic = theory.diatonic_chords(tonic, mode, include_sevenths=True)
        prior = np.array([1.0 if c in diatonic else NON_DIATONIC_PENALTY for c in chords])
        sim = sim * prior[:, None]
```

Find:

```python
    use_flats = PREFER_FLATS and theory.key_uses_flats(tonic, mode)
```

Replace with:

```python
    use_flats = prefer_flats and theory.key_uses_flats(tonic, mode)
```

Find:

```python
    events = _merge_short_events(events, MIN_CHORD_SECONDS)
```

Replace with:

```python
    events = _merge_short_events(events, min_chord_seconds)
```

The module constants `SNAP_CHORDS_TO_KEY`/`PREFER_FLATS`/`INCLUDE_SEVENTH_CHORDS`/
`MIN_CHORD_SECONDS` stay exactly where they are (unchanged) — they now serve only as
the function's default values, plus the backward-compatibility test above imports
them directly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_detect_chords.py -v`
Expected: 8 passed (takes longer than before — three more real librosa analyses).

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/detect_chords.py tests/test_detect_chords.py CLAUDE.md
git commit -m "detect_chords.py: configurable snap/flats/sevenths/min-chord-length"
```

---

## Task 5: assemble.py — resolution/fps/encoder/crf and render-style passthrough

**Files:**
- Modify: `lyricvideo/assemble.py`
- Modify: `tests/test_assemble.py`

**Interfaces:**
- Consumes: `lyricvideo.render.FRAME_SIZE`/`ACCENT_COLOR`/`DIM_TEXT_COLOR` (Task 3),
  `draw_scene(..., font_size, text_color, frame_size)` and
  `draw_chord_bar(..., frame_size, accent_color, dim_text_color, panel_color,
  panel_alpha, chord_now_size, chord_next_size, show_chord_timeline, show_key_bpm,
  timeline_window_sec)` (Task 3's exact signatures).
- Produces: `assemble_video(lines, chord_track, image_dir, audio_path, out_path,
  font_path, fallback_color=(30,30,40), *, frame_size=FRAME_SIZE, fps=24,
  encoder="libx264", crf=20, lyric_size=48, text_color=(255,255,255),
  accent_color=ACCENT_COLOR, dim_text_color=DIM_TEXT_COLOR,
  panel_color=(11,18,32), panel_alpha=150, chord_now_size=64, chord_next_size=32,
  show_chord_timeline=True, show_key_bpm=True, timeline_window_sec=12.0) -> None`.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_assemble.py` in full:

```python
from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word


def _fake_clips(calls):
    class _FakeAudioClip:
        duration = 2.0

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame
            calls["duration"] = duration

        def set_audio(self, audio_clip):
            calls["audio_clip"] = audio_clip
            return self

        def write_videofile(self, path, fps, codec, audio_codec, ffmpeg_params=None):
            calls["write_path"] = path
            calls["fps"] = fps
            calls["codec"] = codec
            calls["ffmpeg_params"] = ffmpeg_params
            Path(path).write_bytes(b"fake-mp4")

    return _FakeAudioClip, _FakeVideoClip


def test_assemble_video_invokes_write_videofile(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path)

    assert calls["duration"] == 2.0
    assert calls["fps"] == 24
    assert calls["codec"] == "libx264"
    assert calls["ffmpeg_params"] == ["-crf", "20"]
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (1080, 1920)


def test_assemble_video_threads_chord_track_into_scene(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    real_build_scene = assemble_module.build_scene

    def spying_build_scene(lines, t, chord_track=None, **kwargs):
        calls["chord_track"] = chord_track
        return real_build_scene(lines, t, chord_track=chord_track, **kwargs)

    monkeypatch.setattr(assemble_module, "build_scene", spying_build_scene)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    chord_track = ChordTrack(events=[ChordEvent(1.0, 2.0, "Em7")])
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](1.5)

    assert calls["chord_track"] == chord_track


def test_assemble_video_draws_chord_bar_on_every_frame(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_chord_bar = assemble_module.draw_chord_bar

    def spying_draw_chord_bar(frame, chord_track, t, font_path, **kwargs):
        draw_calls.append(t)
        return real_draw_chord_bar(frame, chord_track, t, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_bar", spying_draw_chord_bar)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](0.5)

    assert draw_calls == [0.5]


def test_assemble_video_respects_custom_resolution_fps_encoder_crf(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        frame_size=(1280, 720), fps=30, encoder="libx265", crf=24,
    )

    assert calls["fps"] == 30
    assert calls["codec"] == "libx265"
    assert calls["ffmpeg_params"] == ["-crf", "24"]
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (720, 1280)


def test_assemble_video_passes_render_style_kwargs_through_to_draw_scene_and_chord_bar(
    tmp_path, monkeypatch, test_font_path
):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_scene = assemble_module.draw_scene
    real_draw_chord_bar = assemble_module.draw_chord_bar

    def spying_draw_scene(scene, background, font_path, **kwargs):
        captured["draw_scene_kwargs"] = kwargs
        return real_draw_scene(scene, background, font_path, **kwargs)

    def spying_draw_chord_bar(frame, chord_track, t, font_path, **kwargs):
        captured["draw_chord_bar_kwargs"] = kwargs
        return real_draw_chord_bar(frame, chord_track, t, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_scene", spying_draw_scene)
    monkeypatch.setattr(assemble_module, "draw_chord_bar", spying_draw_chord_bar)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        lyric_size=52, text_color=(1, 2, 3), accent_color=(4, 5, 6), show_key_bpm=False,
    )
    calls["make_frame"](0.5)

    assert captured["draw_scene_kwargs"]["font_size"] == 52
    assert captured["draw_scene_kwargs"]["text_color"] == (1, 2, 3)
    assert captured["draw_chord_bar_kwargs"]["accent_color"] == (4, 5, 6)
    assert captured["draw_chord_bar_kwargs"]["show_key_bpm"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_assemble.py -v`
Expected: FAIL — `TypeError: write_videofile() got an unexpected keyword argument
'ffmpeg_params'` (the old fake didn't accept it) or `assemble_video() got an
unexpected keyword argument 'frame_size'`, depending on which test collects first.

- [ ] **Step 3: Update the implementation**

Replace `lyricvideo/assemble.py` in full:

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

try:
    from moviepy.editor import AudioFileClip, VideoClip  # moviepy < 2.0
except ImportError:
    from moviepy import AudioFileClip, VideoClip  # moviepy >= 2.0 dropped .editor

from .layout import build_scene
from .models import ChordTrack, LyricLine
from .render import (
    ACCENT_COLOR, DIM_TEXT_COLOR, FRAME_SIZE, apply_ken_burns, draw_chord_bar, draw_scene,
    ken_burns_preset_for_key,
)

FPS = 24


def assemble_video(
    lines: list[LyricLine],
    chord_track: ChordTrack,
    image_dir: Path,
    audio_path: Path,
    out_path: Path,
    font_path: str,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
    *,
    frame_size: tuple[int, int] = FRAME_SIZE,
    fps: int = FPS,
    encoder: str = "libx264",
    crf: int = 20,
    lyric_size: int = 48,
    text_color: tuple[int, int, int] = (255, 255, 255),
    accent_color: tuple[int, int, int] = ACCENT_COLOR,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR,
    panel_color: tuple[int, int, int] = (11, 18, 32),
    panel_alpha: int = 150,
    chord_now_size: int = 64,
    chord_next_size: int = 32,
    show_chord_timeline: bool = True,
    show_key_bpm: bool = True,
    timeline_window_sec: float = 12.0,
) -> None:
    image_cache: dict[str, Image.Image] = {}
    audio_clip = AudioFileClip(str(audio_path))
    duration = audio_clip.duration

    def get_image(key: str) -> Image.Image:
        if key not in image_cache:
            path = image_dir / f"{key}.png"
            if path.exists():
                image_cache[key] = Image.open(path).convert("RGB")
            else:
                image_cache[key] = Image.new("RGB", frame_size, fallback_color)
        return image_cache[key]

    def make_frame(t: float):
        scene = build_scene(lines, t, chord_track=chord_track, audio_duration=duration)
        start_x, start_y, end_x, end_y, zoom_start, zoom_end = ken_burns_preset_for_key(scene.image_key)
        bg = apply_ken_burns(
            get_image(scene.image_key), scene.ken_burns_progress,
            start_x, start_y, end_x, end_y, zoom_start, zoom_end,
            frame_size=frame_size,
        )
        frame = draw_scene(
            scene, bg, font_path, font_size=lyric_size, text_color=text_color, frame_size=frame_size,
        )
        frame = draw_chord_bar(
            frame, chord_track, t, font_path,
            frame_size=frame_size, accent_color=accent_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=panel_alpha, chord_now_size=chord_now_size,
            chord_next_size=chord_next_size, show_chord_timeline=show_chord_timeline,
            show_key_bpm=show_key_bpm, timeline_window_sec=timeline_window_sec,
        )
        return np.array(frame)

    video_clip = VideoClip(make_frame, duration=duration).set_audio(audio_clip)
    video_clip.write_videofile(
        str(out_path), fps=fps, codec=encoder, audio_codec="aac",
        ffmpeg_params=["-crf", str(crf)],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_assemble.py -v`
Expected: 5 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/assemble.py tests/test_assemble.py CLAUDE.md
git commit -m "assemble_video: resolution/fps/encoder/crf and render-style passthrough"
```

---

## Task 6: pipeline.py — thread a Settings object through detect_chords/assemble_video

This is the one place in the codebase that imports `settings.py` — `detect_chords.py`/
`render.py`/`assemble.py` stay decoupled (Global Constraints).

**Files:**
- Modify: `lyricvideo/pipeline.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `lyricvideo.settings.Settings`/`RESOLUTIONS`/`hex_to_rgb` (Task 2),
  `detect_chords(..., snap_chords_to_key, prefer_flats, include_seventh_chords,
  min_chord_seconds)` (Task 4), `assemble_video(..., frame_size, fps, encoder, crf,
  lyric_size, text_color, accent_color, dim_text_color, panel_color, panel_alpha,
  chord_now_size, chord_next_size, show_chord_timeline, show_key_bpm,
  timeline_window_sec)` (Task 5).
- Produces: `run_pipeline(audio_path, work_dir, title=None, start_stage="identify",
  font_path=None, settings: Settings | None = None, progress_callback=None) -> Path`.

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_pipeline.py` (keep the existing 19, add after them;
add `from lyricvideo.settings import Settings` to the top imports):

```python
def test_run_pipeline_no_settings_argument_uses_all_defaults(tmp_path, monkeypatch):
    """Backward-compatibility contract: omitting settings entirely must call
    detect_chords/assemble_video with exactly the same values as before this
    feature existed."""
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_detect_chords(path, **kwargs):
        captured["detect_chords_kwargs"] = kwargs
        return ChordTrack(events=[ChordEvent(0.0, 1.0, "C")])

    def spying_assemble_video(*args, **kwargs):
        captured["assemble_video_kwargs"] = kwargs

    monkeypatch.setattr("lyricvideo.pipeline.detect_chords", spying_detect_chords)
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", spying_assemble_video)

    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)

    assert captured["detect_chords_kwargs"] == {}
    assert captured["assemble_video_kwargs"] == {}


def test_run_pipeline_settings_reach_detect_chords(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_detect_chords(path, **kwargs):
        captured["kwargs"] = kwargs
        return ChordTrack(events=[ChordEvent(0.0, 1.0, "C")])

    monkeypatch.setattr("lyricvideo.pipeline.detect_chords", spying_detect_chords)

    settings = Settings(snap_chords_to_key=False, prefer_flats=False,
                        include_seventh_chords=True, min_chord_seconds=1.0)
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir, settings=settings)

    assert captured["kwargs"] == {
        "snap_chords_to_key": False, "prefer_flats": False,
        "include_seventh_chords": True, "min_chord_seconds": 1.0,
    }


def test_run_pipeline_settings_reach_assemble_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_assemble_video(*args, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", spying_assemble_video)

    settings = Settings(
        resolution="720p (1280x720)", fps=30, encoder="libx265", crf=24,
        lyric_size=52, text_color="#ff0000", accent_color="#00ff00",
        dim_text_color="#0000ff", panel_color="#111111", panel_alpha=100,
        chord_now_size=70, chord_next_size=36, show_chord_timeline=False,
        show_key_bpm=False, timeline_window_sec=8.0,
    )
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir, settings=settings)

    assert captured["kwargs"]["frame_size"] == (1280, 720)
    assert captured["kwargs"]["fps"] == 30
    assert captured["kwargs"]["encoder"] == "libx265"
    assert captured["kwargs"]["crf"] == 24
    assert captured["kwargs"]["lyric_size"] == 52
    assert captured["kwargs"]["text_color"] == (255, 0, 0)
    assert captured["kwargs"]["accent_color"] == (0, 255, 0)
    assert captured["kwargs"]["dim_text_color"] == (0, 0, 255)
    assert captured["kwargs"]["panel_color"] == (17, 17, 17)
    assert captured["kwargs"]["panel_alpha"] == 100
    assert captured["kwargs"]["chord_now_size"] == 70
    assert captured["kwargs"]["chord_next_size"] == 36
    assert captured["kwargs"]["show_chord_timeline"] is False
    assert captured["kwargs"]["show_key_bpm"] is False
    assert captured["kwargs"]["timeline_window_sec"] == 8.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v -k settings`
Expected: FAIL — `TypeError: run_pipeline() got an unexpected keyword argument
'settings'`

- [ ] **Step 3: Update the implementation**

In `lyricvideo/pipeline.py`, add the import. Find:

```python
from .detect_chords import detect_chords
from .fetch_lyrics import fetch_lyric_lines
from .identify import extract_metadata
from .imagery import get_or_generate_image, summarize_song_gist
from .layout import _in_a_line
from .models import ChordTrack, LyricLine, Song, Word, load_song, save_song
from .separate import separate_vocals
```

Replace with:

```python
from .detect_chords import detect_chords
from .fetch_lyrics import fetch_lyric_lines
from .identify import extract_metadata
from .imagery import get_or_generate_image, summarize_song_gist
from .layout import _in_a_line
from .models import ChordTrack, LyricLine, Song, Word, load_song, save_song
from .separate import separate_vocals
from .settings import RESOLUTIONS, Settings, hex_to_rgb
```

Update the function signature. Find:

```python
def run_pipeline(
    audio_path: Path,
    work_dir: Path,
    title: str | None = None,
    start_stage: str = "identify",
    font_path: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    start_idx = STAGES.index(start_stage)
```

Replace with:

```python
def run_pipeline(
    audio_path: Path,
    work_dir: Path,
    title: str | None = None,
    start_stage: str = "identify",
    font_path: str | None = None,
    settings: Settings | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    start_idx = STAGES.index(start_stage)
    # None (the CLI's default, and every call before this feature existed) means
    # "use every one of Settings' own defaults" -- which are themselves exactly
    # today's hardcoded values, so this is a no-op for anyone not using the GUI's
    # new Settings panel.
    detect_kwargs: dict = {}
    assemble_kwargs: dict = {}
    if settings is not None:
        detect_kwargs = {
            "snap_chords_to_key": settings.snap_chords_to_key,
            "prefer_flats": settings.prefer_flats,
            "include_seventh_chords": settings.include_seventh_chords,
            "min_chord_seconds": settings.min_chord_seconds,
        }
        assemble_kwargs = {
            "frame_size": RESOLUTIONS[settings.resolution],
            "fps": settings.fps,
            "encoder": settings.encoder,
            "crf": settings.crf,
            "lyric_size": settings.lyric_size,
            "text_color": hex_to_rgb(settings.text_color),
            "accent_color": hex_to_rgb(settings.accent_color),
            "dim_text_color": hex_to_rgb(settings.dim_text_color),
            "panel_color": hex_to_rgb(settings.panel_color),
            "panel_alpha": settings.panel_alpha,
            "chord_now_size": settings.chord_now_size,
            "chord_next_size": settings.chord_next_size,
            "show_chord_timeline": settings.show_chord_timeline,
            "show_key_bpm": settings.show_key_bpm,
            "timeline_window_sec": settings.timeline_window_sec,
        }
```

Wire `detect_kwargs` into the detect_chords call. Find:

```python
    if start_idx <= STAGES.index("detect_chords"):
        report("detect_chords")
        song.chord_track = detect_chords(instrumental_stem_path)
        save_song(song, timed_path)
```

Replace with:

```python
    if start_idx <= STAGES.index("detect_chords"):
        report("detect_chords")
        song.chord_track = detect_chords(instrumental_stem_path, **detect_kwargs)
        save_song(song, timed_path)
```

Wire `assemble_kwargs` into the render call. Find:

```python
    if start_idx <= STAGES.index("render"):
        report("render")
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            font_path or _default_font(),
        )
```

Replace with:

```python
    if start_idx <= STAGES.index("render"):
        report("render")
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            font_path or _default_font(),
            **assemble_kwargs,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: 22 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/pipeline.py tests/test_pipeline.py CLAUDE.md
git commit -m "run_pipeline: thread an optional Settings object through the pipeline"
```

---

## Task 7: SettingsPanel — CustomTkinter widget bound to Settings

Widget construction itself stays manually/visually verified (this project's existing
testing-constraint precedent for GUI code — see `test_on_deck_cache.py`'s AST-
extraction pattern and `gui.py`'s own `_split_log_text`, both pure logic pulled out
of GUI code specifically so it's unit-testable). This task follows the same
approach: the only genuinely testable piece — turning a flat dict of raw widget
values into a real `Settings` object, handling the type coercion CustomTkinter's Tk
variables need (`CTkSlider`/`CTkOptionMenu` always back onto `str`/`float`
variables, never `int`/`bool` directly) — is a standalone pure function.

**Files:**
- Create: `lyricvideo/settings_panel.py`
- Test: `tests/test_settings_panel.py`

**Interfaces:**
- Consumes: `lyricvideo.settings.Settings`/`RESOLUTIONS`/`ENCODERS`/`FPS_OPTIONS`/
  `hex_to_rgb` (Task 2), `customtkinter` (Task 1).
- Produces: `values_to_settings(raw: dict) -> Settings` (pure, tested);
  `SettingsPanel(master, settings: Settings, on_change: Callable[[], None] | None = None)`
  — a `ctk.CTkScrollableFrame` with `.load_from(settings)` and `.collect() -> Settings`
  (manually verified only, per the testing-constraint precedent above).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_settings_panel.py`:

```python
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
    }


def test_values_to_settings_produces_the_defaults_from_default_raw_values():
    assert values_to_settings(_raw_defaults()) == Settings()


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_settings_panel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.settings_panel'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/settings_panel.py`:

```python
"""CustomTkinter Settings panel, styled after LyricChord's own, bound to a Settings
object. Widget construction stays manually/visually verified (this project's
existing testing-constraint precedent for GUI code); values_to_settings() below is
the one piece of real logic here and is unit-tested directly."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import asdict
from tkinter import colorchooser, filedialog
from typing import Optional

import customtkinter as ctk

from .settings import ENCODERS, FPS_OPTIONS, RESOLUTIONS, Settings, hex_to_rgb

_INT_FIELDS = {"fps", "crf", "lyric_size", "chord_now_size", "chord_next_size", "panel_alpha"}


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
        self.grid_columnconfigure(1, weight=1)
        self._build()
        self.load_from(settings)

    def _var(self, name: str, kind) -> tk.Variable:
        v = kind()
        v.trace_add("write", lambda *_: self._changed())
        self.vars[name] = v
        return v

    def _changed(self) -> None:
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

    def _build(self) -> None:
        self._section("Output")
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

        self._section("Chord detection")
        self._check("snap_chords_to_key", "Bias detected chords toward the song key")
        self._check("prefer_flats", "Use flats in flat keys (Bb instead of A#)")
        self._check("include_seventh_chords", "Detect 7th chords (7, m7, maj7)")
        self._slider("min_chord_seconds", "Minimum chord length", 0.2, 2.0, 18, lambda v: f"{v:.1f}s")

    def load_from(self, settings: Settings) -> None:
        for name, value in asdict(settings).items():
            if name in self.vars:
                self.vars[name].set(value)

    def collect(self) -> Settings:
        raw = {name: var.get() for name, var in self.vars.items()}
        return values_to_settings(raw)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_settings_panel.py -v`
Expected: 5 passed

- [ ] **Step 5: Manually smoke-test the panel renders**

```bash
cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -c "
import customtkinter as ctk
from lyricvideo.settings import Settings
from lyricvideo.settings_panel import SettingsPanel

root = ctk.CTk()
root.geometry('500x700')
panel = SettingsPanel(root, Settings())
panel.pack(fill='both', expand=True)
root.after(2000, root.destroy)
root.mainloop()
"
```

Expected: a window opens showing all four sections (Output, Typography & colors,
Chord bar, Chord detection) with working sliders/dropdowns/color swatches/
checkboxes, then closes itself after 2 seconds. Confirm no traceback.

- [ ] **Step 6: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/settings_panel.py tests/test_settings_panel.py CLAUDE.md
git commit -m "Add CustomTkinter SettingsPanel bound to Settings"
```

---

## Task 8: gui.py — full CustomTkinter rebuild

This replaces every `tk`/`ttk` widget constructor with its `customtkinter`
equivalent and restructures the layout into LyricChord's two-column shape (left =
the single-song form + Redo section, right = the new `SettingsPanel`), adds the new
GUI-side generation progress bar, and wires Settings persistence (load on launch,
save on every change via `SettingsPanel`'s `on_change` callback). Every existing
behavior is preserved exactly: title auto-fill, API-key check, the Update Available
flow (banner/dialog/apply/relaunch), Generate, **Redo** (dropdown + "Generate new
images" checkbox + confirmation dialog + backup, resuming at `"fetch_lyrics"`), the
`\r`/`\n`-aware log console. `_slugify`/`_split_log_text` (the two pure functions
already unit-tested in `test_gui.py`) are untouched — that test file needs no
changes and must still pass unmodified.

**Files:**
- Modify: `lyricvideo/gui.py`

**Interfaces:**
- Consumes: `lyricvideo.settings.Settings` (Task 2), `lyricvideo.settings_panel.SettingsPanel`
  (Task 7), `lyricvideo.pipeline.run_pipeline`'s `settings` parameter (Task 6),
  `lyricvideo.pipeline.STAGES` (existing) for the new progress bar's fraction
  calculation.
- Produces: no new public functions — `LyricVideoGUI`'s public behavior (what
  `main()` launches) is what the manual smoke test in Step 4 verifies.

This task has no new automated tests of its own (matches this project's existing
testing-constraint precedent for GUI widget-building code — see the spec's Testing
section) — `tests/test_gui.py` must still pass unmodified, and Step 4 below is a
mandatory real manual smoke test, not optional.

- [ ] **Step 1: Confirm test_gui.py needs no changes**

Run: `.venv/bin/python -m pytest tests/test_gui.py -v`
Expected: 11 passed (these test only `_slugify`/`_split_log_text`, both untouched by
this task — this is a sanity check to run again after Step 3, not just before).

- [ ] **Step 2: Note the CustomTkinter API differences from ttk this rewrite depends on**

- `ctk.CTk()` replaces `tk.Tk()` as the root window (required for CTk widgets'
  theming to apply).
- `ctk.CTkFrame`/`CTkLabel`/`CTkEntry`/`CTkButton`/`CTkCheckBox`/`CTkComboBox`
  replace their `ttk`/`tk` equivalents. `CTkButton.configure(state="disabled")`
  (not `ttk`'s `.state(["disabled"])`) is how a `CTkButton` is disabled/re-enabled;
  `CTkComboBox` uses `.configure(values=[...])` to refresh its dropdown list.
- `ctk.CTkTextbox` replaces `scrolledtext.ScrolledText` for the log console and the
  update-notes viewer — it has its own built-in scrollbar, so no separate
  `Scrollbar` widget is needed. `.configure(state="normal"/"disabled")` still works
  the same way as the old `Text` widget for making it read-only between updates.
- `ctk.CTkToplevel` replaces `tk.Toplevel` for the update dialog.
- `ctk.CTkProgressBar` (new, for Step 3's generation progress bar) takes a value in
  `0.0..1.0` via `.set(fraction)`; `.configure(mode="determinate")` is its default.
- `messagebox`/`filedialog`/`colorchooser` stay plain `tkinter` (CustomTkinter has
  no replacement for these — this is normal, matches LyricChord's own GUI, which
  also imports these straight from `tkinter`).

- [ ] **Step 3: Replace lyricvideo/gui.py in full**

```python
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox

import customtkinter as ctk
import httpx
from dotenv import load_dotenv

from .identify import extract_metadata
from .pipeline import (
    STAGES,
    run_pipeline,
    slugify as _slugify,
    list_redoable_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
)
from .settings import Settings
from .settings_panel import SettingsPanel
from .update.apply import copy_updatable_files, extract_release_archive, requirements_changed
from .update.release_client import RELEASES_REPO, check_for_update
from .update.version import read_local_version, write_local_version

_CR_LF_RE = re.compile(r"[\r\n]")


def _split_log_text(pending: str, text: str) -> tuple[str, str]:
    """Terminal-style \\r/\\n handling for the log widget: \\n commits the
    current line permanently, \\r discards it and starts the line over (this
    is what tqdm-style progress bars send on every update). Returns
    (new_pending_line, text_to_commit) -- text_to_commit is zero or more
    complete newline-terminated lines safe to insert verbatim; new_pending
    is the trailing not-yet-terminated content that should currently be
    showing as the widget's last, still-changeable line.
    """
    if not text:
        return pending, ""
    committed: list[str] = []
    current = pending
    start = 0
    for m in _CR_LF_RE.finditer(text):
        idx = m.start()
        current += text[start:idx]
        if m.group() == "\n":
            committed.append(current + "\n")
        current = ""
        start = idx + 1
    current += text[start:]
    return current, "".join(committed)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE_PATH = PROJECT_ROOT / "VERSION"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class _QueueWriter:
    """File-like object that pushes writes into a queue instead of a real
    stream -- lets stdout/stderr from the worker thread (Demucs/moviepy's own
    progress output) reach the GUI's log widget instead of the terminal."""

    def __init__(self, q: "queue.Queue"):
        self._queue = q

    def write(self, text: str) -> int:
        if text:
            self._queue.put(("log", text))
        return len(text)

    def flush(self) -> None:
        pass


class LyricVideoGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        current_version = read_local_version(str(_VERSION_FILE_PATH)) or "v0.0.0"
        root.title(f"PlayAlongVideoProduction {current_version}")
        root.geometry("1200x780")

        self._queue: "queue.Queue" = queue.Queue()
        self._update_queue: "queue.Queue" = queue.Queue()
        self._current_version = current_version
        self._available_update: dict | None = None
        self._running = False
        self._log_pending = ""
        self._log_has_uncommitted_line = False
        self._suppress_settings_save = True  # True while load_from() is populating widgets on launch

        self.settings = Settings.load()

        self.title_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.work_dir_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.redo_song_var = tk.StringVar()
        self.redo_new_images_var = tk.BooleanVar(value=False)

        self.title_var.trace_add("write", self._on_title_changed)

        self._build_widgets()
        self._suppress_settings_save = False
        self._check_api_keys()
        self._start_update_check()

    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        self.update_banner_var = tk.StringVar()
        self.update_banner = ctk.CTkLabel(
            self.root,
            textvariable=self.update_banner_var,
            text_color="#4da3ff",
            cursor="hand2",
            anchor="w",
        )
        self.update_banner.bind("<Button-1>", self._on_update_banner_clicked)

        # Two-column body: left = single-song form + Redo, right = SettingsPanel.
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, **pad)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.top_frame = left  # anchor for the update banner's `before=` pack

        form = ctk.CTkFrame(left, fg_color="transparent")
        form.pack(fill="x", padx=10, pady=10)
        form.grid_columnconfigure(1, weight=1)

        self._add_row(form, 0, "Song title (auto-filled, editable):", self.title_var)
        self._add_file_row(
            form, 1, "Audio file (mp3/wav):", self.audio_var,
            [("Audio files", "*.mp3 *.wav *.m4a *.flac"), ("All files", "*.*")],
            on_selected=self._on_audio_selected,
        )
        self._add_row(form, 2, "Work directory:", self.work_dir_var)

        note = ctk.CTkLabel(
            form,
            text="Title, artist, and lyrics are identified automatically from the audio\n"
            "file's tags and online lookup -- edit the title above if it's wrong. Chords\n"
            "are detected directly from the audio; no tab or chord sheet is needed.",
            text_color="gray60",
            justify="left",
            anchor="w",
        )
        note.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))

        self.generate_button = ctk.CTkButton(form, text="Generate Video", command=self._on_generate)
        self.generate_button.grid(row=4, column=0, columnspan=3, pady=8)

        status_frame = ctk.CTkFrame(left, fg_color="transparent")
        status_frame.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(status_frame, text="Status:").pack(side="left")
        ctk.CTkLabel(status_frame, textvariable=self.status_var, text_color="#3ecf8e").pack(
            side="left", padx=6
        )

        self.progress_bar = ctk.CTkProgressBar(left)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 10))

        redo_frame = ctk.CTkFrame(left)
        redo_frame.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(redo_frame, text="Redo an Existing Song", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        redo_controls = ctk.CTkFrame(redo_frame, fg_color="transparent")
        redo_controls.pack(fill="x", padx=8, pady=(0, 8))
        self.redo_combo = ctk.CTkComboBox(
            redo_controls, variable=self.redo_song_var,
            values=list_redoable_songs(PROJECT_ROOT / "work"), width=260, state="readonly",
        )
        self.redo_combo.pack(side="left", padx=(0, 8))
        ctk.CTkCheckBox(
            redo_controls, text="Generate new images", variable=self.redo_new_images_var,
        ).pack(side="left", padx=8)
        self.redo_button = ctk.CTkButton(redo_controls, text="Redo", command=self._on_redo, width=80)
        self.redo_button.pack(side="left", padx=8)

        self.log_widget = ctk.CTkTextbox(left, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew")
        self.settings_panel = SettingsPanel(right, self.settings, on_change=self._on_settings_changed)
        self.settings_panel.pack(fill="both", expand=True, padx=6, pady=6)

    def _add_row(self, frame: ctk.CTkFrame, row: int, label: str, var: tk.StringVar) -> None:
        ctk.CTkLabel(frame, text=label).grid(row=row, column=0, sticky="w")
        ctk.CTkEntry(frame, textvariable=var, width=360).grid(row=row, column=1, sticky="ew", padx=4)

    def _add_file_row(
        self, frame: ctk.CTkFrame, row: int, label: str, var: tk.StringVar, filetypes: list,
        on_selected=None,
    ) -> None:
        self._add_row(frame, row, label, var)

        def browse() -> None:
            path = filedialog.askopenfilename(filetypes=filetypes)
            if path:
                var.set(path)
                if on_selected is not None:
                    on_selected(path)

        ctk.CTkButton(frame, text="Browse...", command=browse, width=90).grid(row=row, column=2, padx=4)

    def _on_settings_changed(self) -> None:
        """SettingsPanel's on_change fires on every keystroke/slider-move/color-pick.
        Suppressed while the panel is still being populated on launch (Settings.load()
        itself is already the source of truth then -- saving mid-load would just
        write back the same file it was read from, harmlessly but pointlessly)."""
        if self._suppress_settings_save:
            return
        self.settings = self.settings_panel.collect()
        self.settings.save()

    def _on_title_changed(self, *_args) -> None:
        if not self._running:
            slug = _slugify(self.title_var.get())
            self.work_dir_var.set(str(PROJECT_ROOT / "work" / slug))

    def _on_audio_selected(self, path: str) -> None:
        """Best-effort auto-fill of the title once an audio file is picked --
        never overwrites a title the owner already typed, and any failure
        (offline, unreadable file) is silently ignored: Generate still works
        with an auto-identified title computed fresh inside run_pipeline's own
        identify stage regardless of whether this GUI-side preview succeeds."""
        if self.title_var.get().strip():
            return
        thread = threading.Thread(target=self._identify_worker, args=(path,), daemon=True)
        thread.start()

    def _identify_worker(self, path: str) -> None:
        try:
            info = extract_metadata(Path(path))
        except Exception:
            return
        self.root.after(0, lambda: self._apply_identified_title(info.title))

    def _apply_identified_title(self, title: str) -> None:
        if not self.title_var.get().strip():  # still empty -- no manual edit arrived meanwhile
            self.title_var.set(title)

    def _check_api_keys(self) -> None:
        load_dotenv(PROJECT_ROOT / ".env")
        missing = [k for k in ("ANTHROPIC_API_KEY", "REPLICATE_API_TOKEN") if not os.environ.get(k)]
        if missing:
            messagebox.showwarning(
                "Missing API keys",
                f"{', '.join(missing)} not found in {PROJECT_ROOT / '.env'}.\n"
                "Video generation will fail at the images stage without them.",
            )

    def _start_update_check(self) -> None:
        thread = threading.Thread(target=self._update_check_worker, daemon=True)
        thread.start()
        self.root.after(200, self._poll_update_queue)

    def _update_check_worker(self) -> None:
        try:
            release = check_for_update(self._current_version, RELEASES_REPO)
        except Exception:
            # Offline, GitHub hiccup, or no releases cut yet -- the launch-
            # time check must never surface an error or crash the GUI.
            return
        if release is not None:
            self._update_queue.put(("available", release))

    def _poll_update_queue(self) -> None:
        try:
            while True:
                kind, payload = self._update_queue.get_nowait()
                if kind == "available":
                    self._available_update = payload
                    self.update_banner_var.set(
                        f"Update available: {payload['tag_name']} — click for details"
                    )
                    self.update_banner.pack(
                        fill="x", padx=8, pady=(4, 0), before=self.top_frame
                    )
                elif kind == "apply_status":
                    if hasattr(self, "_update_status_var"):
                        self._update_status_var.set(payload)
                elif kind == "apply_done":
                    self._on_apply_update_done(payload)
                elif kind == "apply_error":
                    self._on_apply_update_error(payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_update_queue)

    def _on_update_banner_clicked(self, _event=None) -> None:
        if self._available_update is not None:
            self._open_update_dialog(self._available_update)

    def _open_update_dialog(self, release: dict) -> None:
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Update available: {release['tag_name']}")
        dialog_w, dialog_h = 480, 360
        self.root.update_idletasks()
        # Centered over the main window and kept above it (transient +
        # grab_set + lift/focus_force) -- a plain Toplevel can otherwise open
        # behind the main window with no visible indication, which is
        # exactly how the owner missed the Relaunch Now button appearing.
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(x, 0)}+{max(y, 0)}")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        self._update_dialog_window = dialog

        notes_widget = ctk.CTkTextbox(dialog, wrap="word", height=220)
        notes_widget.insert("1.0", release.get("notes", "") or "(no release notes)")
        notes_widget.configure(state="disabled")
        notes_widget.pack(fill="both", expand=True, padx=8, pady=8)

        self._update_status_var = tk.StringVar(value="")
        ctk.CTkLabel(dialog, textvariable=self._update_status_var, text_color="gray60").pack(
            anchor="w", padx=8
        )

        self._update_button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        self._update_button_frame.pack(fill="x", padx=8, pady=8)

        self._update_apply_button = ctk.CTkButton(
            self._update_button_frame,
            text="Apply Update",
            command=lambda: self._on_apply_update_clicked(release),
        )
        self._update_apply_button.pack(side="left")
        ctk.CTkButton(self._update_button_frame, text="Close", command=dialog.destroy).pack(
            side="right"
        )

        def _on_close() -> None:
            self._update_dialog_window = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", _on_close)

    def _on_apply_update_clicked(self, release: dict) -> None:
        if not messagebox.askyesno(
            "Apply update",
            f"Download and apply {release['tag_name']} now?\n\n"
            "This reinstalls dependencies if they changed and overwrites the "
            "program's own files. Your songs, work files, and .env are never "
            "touched.",
        ):
            return
        self._update_apply_button.configure(state="disabled")
        self._update_status_var.set("Downloading...")
        thread = threading.Thread(
            target=self._apply_update_worker, args=(release,), daemon=True
        )
        thread.start()

    def _apply_update_worker(self, release: dict) -> None:
        try:
            self._update_queue.put(("apply_status", "Downloading..."))
            # follow_redirects=True is required: unlike requests, httpx does
            # NOT follow redirects by default, and this URL 302s to
            # codeload.github.com.
            response = httpx.get(release["download_url"], timeout=60, follow_redirects=True)
            response.raise_for_status()

            with tempfile.TemporaryDirectory() as tmp_dir:
                archive_path = Path(tmp_dir) / "release.tar.gz"
                archive_path.write_bytes(response.content)

                self._update_queue.put(("apply_status", "Extracting..."))
                extract_dir = Path(tmp_dir) / "extracted"
                extract_dir.mkdir()
                extracted_root = extract_release_archive(str(archive_path), str(extract_dir))

                old_requirements_path = PROJECT_ROOT / "requirements.txt"
                old_requirements = (
                    old_requirements_path.read_text(encoding="utf-8")
                    if old_requirements_path.exists()
                    else ""
                )
                new_requirements_path = Path(extracted_root) / "requirements.txt"
                new_requirements = (
                    new_requirements_path.read_text(encoding="utf-8")
                    if new_requirements_path.exists()
                    else old_requirements
                )

                if requirements_changed(old_requirements, new_requirements):
                    self._update_queue.put(("apply_status", "Installing dependencies..."))
                    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
                    pip_result = subprocess.run(
                        [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
                        cwd=extracted_root,
                        capture_output=True,
                        text=True,
                    )
                    if pip_result.returncode != 0:
                        self._update_queue.put((
                            "apply_error",
                            f"pip install failed — program left unchanged:\n{pip_result.stderr}",
                        ))
                        return

                self._update_queue.put(("apply_status", "Copying files..."))
                copy_updatable_files(extracted_root, str(PROJECT_ROOT))
                write_local_version(str(_VERSION_FILE_PATH), release["tag_name"])

            self._update_queue.put(("apply_done", release["tag_name"]))
        except Exception as e:
            self._update_queue.put(("apply_error", f"{type(e).__name__}: {e}"))

    def _on_apply_update_done(self, tag_name: str) -> None:
        self._update_status_var.set(f"Updated to {tag_name}. Relaunch to use it.")
        self._update_apply_button.pack_forget()
        ctk.CTkButton(
            self._update_button_frame, text="Relaunch Now", command=self._on_relaunch_clicked
        ).pack(side="left")

    def _on_apply_update_error(self, message: str) -> None:
        self._update_status_var.set(f"Update failed: {message}")
        self._update_apply_button.configure(state="normal")

    def _on_relaunch_clicked(self) -> None:
        venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
        subprocess.Popen([str(venv_python), "-m", "lyricvideo.gui"], cwd=str(PROJECT_ROOT))
        self.root.destroy()

    def _on_generate(self) -> None:
        if self._running:
            return

        title = self.title_var.get().strip()  # optional -- run_pipeline's own
        audio = self.audio_var.get().strip()   # identify stage falls back if blank
        work_dir = self.work_dir_var.get().strip()

        if not audio:
            messagebox.showerror("Missing input", "Audio file is required.")
            return
        if not work_dir:
            messagebox.showerror("Missing input", "Work directory is required.")
            return

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.status_var.set("Starting...")
        self.progress_bar.set(0.0)
        self._clear_log()

        thread = threading.Thread(
            target=self._run_worker,
            args=(Path(audio), Path(work_dir), title or None),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _on_redo(self) -> None:
        if self._running:
            return

        slug = self.redo_song_var.get().strip()
        if not slug:
            messagebox.showerror("No song selected", "Pick a song from the dropdown to redo.")
            return

        song_dir = PROJECT_ROOT / "work" / slug
        try:
            audio_path, title = load_redo_inputs(song_dir)
        except Exception as e:
            messagebox.showerror("Could not load song", f"{type(e).__name__}: {e}")
            return

        generate_new_images = self.redo_new_images_var.get()
        if not messagebox.askyesno(
            "Redo song",
            f'Redo "{title}" using the current program?\n\n'
            "This re-syncs chords/lyrics with today's code and re-renders the "
            "video, overwriting it in place -- the current video and timing "
            "data are backed up first. "
            + ("New AI images will be generated." if generate_new_images
               else "Existing images will be reused (no AI cost)."),
        ):
            return

        backup_song_outputs(song_dir, _slugify(title))
        if generate_new_images:
            prepare_images_for_fresh_regeneration(song_dir / "images")

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.status_var.set("Starting...")
        self.progress_bar.set(0.0)
        self._clear_log()

        thread = threading.Thread(
            target=self._run_worker,
            args=(audio_path, song_dir, title, "fetch_lyrics"),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _run_worker(
        self,
        audio_path: Path,
        work_dir: Path,
        title: str | None = None,
        start_stage: str = "identify",
    ) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        try:
            out_path = run_pipeline(
                audio_path,
                work_dir,
                title,
                start_stage=start_stage,
                settings=self.settings,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
            )
            self._queue.put(("done", str(out_path)))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    def _poll_queue(self) -> None:
        # Drain everything queued since the last tick up front, rather than
        # handling each item with its own widget update -- moviepy/tqdm can
        # write dozens of progress-bar chunks within a single 100ms tick, and
        # one insert+see() per chunk against a growing Text widget is what
        # made the log pane (and the whole GUI) grind to a crawl.
        items: list[tuple[str, str]] = []
        try:
            while True:
                items.append(self._queue.get_nowait())
        except queue.Empty:
            pass

        log_chunks: list[str] = []

        def flush_log() -> None:
            if log_chunks:
                self._append_log("".join(log_chunks))
                log_chunks.clear()

        for kind, payload in items:
            if kind == "log":
                log_chunks.append(payload)
                continue
            flush_log()
            if kind == "stage":
                self.status_var.set(f"Stage: {payload}")
                # +1: report("done") isn't a real STAGES entry, but seeing the
                # bar reach 100% only once done fires (not at the start of the
                # last real stage) reads better than stalling at 6/7.
                try:
                    fraction = (STAGES.index(payload) + 1) / len(STAGES)
                except ValueError:
                    fraction = self.progress_bar.get()
                self.progress_bar.set(min(1.0, fraction))
            elif kind == "done":
                self.status_var.set("Done")
                self.progress_bar.set(1.0)
                self._running = False
                self.generate_button.configure(state="normal")
                self.redo_button.configure(state="normal")
                messagebox.showinfo("Video ready", f"Wrote {payload}")
                return
            elif kind == "error":
                self.status_var.set("Failed")
                self._running = False
                self.generate_button.configure(state="normal")
                self.redo_button.configure(state="normal")
                self._append_log(f"\nERROR:\n{payload}\n")
                messagebox.showerror("Generation failed", payload.splitlines()[0])
                return
        flush_log()

        if self._running:
            self.root.after(100, self._poll_queue)

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")
        self._log_pending = ""
        self._log_has_uncommitted_line = False

    def _append_log(self, text: str) -> None:
        self._log_pending, to_commit = _split_log_text(self._log_pending, text)

        self.log_widget.configure(state="normal")
        if self._log_has_uncommitted_line:
            # The widget's current last line was left showing a still-in-
            # progress update (e.g. a tqdm percentage) -- replace it rather
            # than appending, so a burst of \r updates collapses into one
            # line instead of piling up a new permanent line per update.
            self.log_widget.delete("end-1c linestart", "end-1c")
        if to_commit:
            self.log_widget.insert("end", to_commit)
        if self._log_pending:
            self.log_widget.insert("end", self._log_pending)
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")
        self._log_has_uncommitted_line = bool(self._log_pending)


def main() -> None:
    root = ctk.CTk()
    LyricVideoGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
```

Note two deliberate behavior additions beyond a pure widget-toolkit swap (both
explicitly in scope): `self.settings = Settings.load()` on launch plus
`_on_settings_changed()` (wired as `SettingsPanel`'s `on_change`) saving on every
control change — persistence across launches, as decided; and the progress bar
filling via `STAGES.index(payload) / len(STAGES)` inside the existing `_poll_queue`
stage-handling branch — no new polling loop, it rides the same "stage" queue
messages `run_pipeline`'s `progress_callback` already emits.

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: every test passes, including `test_gui.py`'s 11 unchanged tests.

- [ ] **Step 5: Manual smoke test — mandatory, not optional**

```bash
cd /home/doug/PlayAlongVideoProduction && ./run_playalongvideoproduction.sh
```

Confirm, by actually looking at the window:
- Two-column layout: left = title/audio/work-dir form + Generate button + status +
  progress bar + Redo section + log console; right = the scrollable Settings panel
  with all four sections and working sliders/dropdowns/color swatches/checkboxes.
- The Redo dropdown lists the same existing songs as before (`list_redoable_songs`
  is unchanged).
- Changing a setting (e.g. dragging the accent-color swatch to a new color) and then
  closing and relaunching the app shows that same color still selected — confirms
  `Settings.save()`/`Settings.load()` round-trip through the real GUI, not just the
  unit tests.
- Click Generate or Redo on a real song and confirm the progress bar visibly
  advances as stages report, the log console still shows live output, and a
  completed run still shows the "Video ready" dialog exactly as before.
- If `ANTHROPIC_API_KEY`/`REPLICATE_API_TOKEN` are missing from `.env`, the same
  startup warning dialog still appears.

If anything above doesn't hold, fix it and re-run this whole step before moving on
— this task is not done until the real window is confirmed correct by eye.

- [ ] **Step 6: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/gui.py CLAUDE.md
git commit -m "Rebuild gui.py on CustomTkinter with a two-column Settings layout"
```

---

## Task 9: Final CLAUDE.md rewrite and full-suite verification

Every prior task left a small, accurate "in progress" sentence in CLAUDE.md (the
same pattern used throughout the previous merge plan) so the pre-commit hook's
CLAUDE.md-staged requirement was always satisfied honestly rather than skipped.
This task replaces those incremental notes with a proper description of the
finished feature, in the same voice/format as the rest of CLAUDE.md, and closes
the plan out with one last full-suite run.

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Rewrite the GUI/Settings section of CLAUDE.md**

Replace the top summary paragraph. Find:

```
Generates synced lyric+chord "play along" videos from nothing but an audio file:
karaoke-style scrolling lyrics (forced-aligned to the real vocal stem) and a
NOW/NEXT/timeline chord bar (chords detected directly from the audio, never from a
tab or chord sheet) composited over an AI-generated, Ken-Burns-panned background
image that changes per lyric line — and per active chord during instrumental
gaps — to follow the song. Original tab-PDF-input design in
`docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md` (superseded —
see the MP3-only merge design below); that build plan (all steps checked off) is in
`docs/superpowers/plans/2026-09-06-tab-pdf-video-generator.md`. The MP3-only merge
design is `docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md`, plan
`docs/superpowers/plans/2026-09-09-mp3-only-chord-merge.md`.
```

Replace with:

```
Generates synced lyric+chord "play along" videos from nothing but an audio file:
karaoke-style scrolling lyrics (forced-aligned to the real vocal stem) and a
NOW/NEXT/timeline chord bar (chords detected directly from the audio, never from a
tab or chord sheet) composited over an AI-generated, Ken-Burns-panned background
image that changes per lyric line — and per active chord during instrumental
gaps — to follow the song. Original tab-PDF-input design in
`docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md` (superseded —
see the MP3-only merge design below); that build plan (all steps checked off) is in
`docs/superpowers/plans/2026-09-06-tab-pdf-video-generator.md`. The MP3-only merge
design is `docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md`, plan
`docs/superpowers/plans/2026-09-09-mp3-only-chord-merge.md`. The GUI is built on
CustomTkinter with an owner-tunable Settings panel (output resolution/fps/encoder/
crf, chord-bar typography/colors/toggles, chord-detection tuning) — design
`docs/superpowers/specs/2026-09-09-customtkinter-settings-gui-design.md`, plan
`docs/superpowers/plans/2026-09-09-customtkinter-settings-gui.md`.
```

Replace the GUI bullet under "Running it". Find:

```
- **GUI (normal use):** double-click the `PlayAlongVideoProduction` desktop icon, or
  run `./run_playalongvideoproduction.sh` from the repo root. That script calls
  `.venv/bin/python` directly rather than `source .venv/bin/activate` — a venv's
  `bin/activate` bakes an absolute `VIRTUAL_ENV` path in at creation time, and this
  venv still carries its pre-rename path (`/home/doug/LyricVideoGen/.venv`), so
  sourcing it would silently put the wrong (or no) `python` first on `PATH`; the
  venv's own `python` binary locates its site-packages relative to itself and needs
  no activation. Supply just an audio
  file — title/artist/lyrics are identified and fetched automatically, chords are
  detected directly from the audio, and the title field is an editable override, not
  a required input — then click Generate. Built with Tkinter (`lyricvideo/gui.py`).
```

Replace with:

```
- **GUI (normal use):** double-click the `PlayAlongVideoProduction` desktop icon, or
  run `./run_playalongvideoproduction.sh` from the repo root. That script calls
  `.venv/bin/python` directly rather than `source .venv/bin/activate` — a venv's
  `bin/activate` bakes an absolute `VIRTUAL_ENV` path in at creation time, and this
  venv still carries its pre-rename path (`/home/doug/LyricVideoGen/.venv`), so
  sourcing it would silently put the wrong (or no) `python` first on `PATH`; the
  venv's own `python` binary locates its site-packages relative to itself and needs
  no activation. Supply just an audio
  file — title/artist/lyrics are identified and fetched automatically, chords are
  detected directly from the audio, and the title field is an editable override, not
  a required input — then click Generate. Built with CustomTkinter
  (`lyricvideo/gui.py`): a two-column layout, left = the single-song form/Generate/
  Redo/log console/generation progress bar, right = the scrollable Settings panel
  (`lyricvideo/settings_panel.py`) bound to a `Settings` object
  (`lyricvideo/settings.py`, persisted to `~/.playalongvideoproduction/settings.json`,
  loaded on launch and saved on every control change). `render.py`/`detect_chords.py`/
  `assemble_video()` all take plain keyword arguments for every Settings-backed value
  (colors as RGB tuples, sizes as int, toggles as bool) and default to the program's
  original hardcoded values — they do not import `settings.py`; `run_pipeline()` is
  the sole integration point that accepts a real `Settings` object and unpacks it.
```

- [ ] **Step 2: Run the full test suite one final time**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: every test passes (the full suite, including every test added across
Tasks 2-7 of this plan and the unchanged `test_gui.py`).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the finished CustomTkinter GUI and Settings feature"
```

---
