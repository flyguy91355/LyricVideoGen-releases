# CustomTkinter GUI + Settings Panel — Design

**Date:** 2026-09-09
**Status:** Approved by owner, ready for implementation planning

## Purpose

Owner has been using LyricChord (`/home/doug/Lyric+Chord`, a sibling project) and
prefers its CustomTkinter-based GUI and Settings panel to PlayAlongVideoProduction's
current plain-Tkinter 3-field form. This rebuilds our GUI on CustomTkinter with a
Settings panel in the same visual style, wired to our own pipeline's real tunables —
not a literal file copy, since LyricChord's GUI calls its own different pipeline
(batch queue, no forced alignment, no AI image generation).

## Scope

**In scope:**
1. New `lyricvideo/settings.py`: a `Settings` dataclass covering only fields that map
   onto our real pipeline (below), with `load()`/`save()` to
   `~/.playalongvideoproduction/settings.json` (a per-user file, not repo data).
2. `gui.py` rebuilt on CustomTkinter (`customtkinter` added to `requirements.txt`),
   two-column layout matching LyricChord's: left = our existing single-song form
   (title/audio/work-dir + Generate + Redo section, restyled) and right = a
   scrollable `SettingsPanel` bound to the `Settings` object, sectioned like
   LyricChord's own (Output / Typography & colors / Chord bar / Chord detection).
3. `render.py`'s `draw_scene()` and `draw_chord_bar()` gain optional parameters for
   every value in the "Typography & colors" and "Chord bar" sections below,
   defaulting to today's exact hardcoded constants — unset, behavior is unchanged.
4. `detect_chords()` gains optional keyword parameters for the "Chord detection"
   section below, same non-breaking-default approach.
5. `assemble_video()` gains optional `fps`/`encoder`/`crf` parameters (for the
   "Output" section), passed through to `write_videofile()`; `FRAME_SIZE` becomes
   settable via a `resolution` parameter threaded from `Settings` down to
   `apply_ken_burns()`/`draw_scene()`/`draw_chord_bar()` (their internal geometry is
   already computed from `FRAME_SIZE`-relative fractions, so this is a plumbing
   change, not a geometry rewrite).
6. `pipeline.run_pipeline()` gains an optional `settings: Settings | None = None`
   parameter (`None` → `Settings()` defaults, today's exact behavior), threading it
   into the `detect_chords`/`assemble_video` calls.
7. A new GUI-level generation progress bar (not a `Settings` field, not persisted):
   fills proportionally to `STAGES.index(stage) / len(STAGES)` as
   `progress_callback` fires during Generate/Redo. This is distinct from
   LyricChord's `show_progress_bar` setting, which draws a playback scrubber *into
   the rendered video itself* — out of scope, see below.
8. Tests for `settings.py` (round-trip load/save, defaults), and for the new
   optional parameters on `draw_scene`/`draw_chord_bar`/`detect_chords`/
   `assemble_video` (each producing a visibly/measurably different result when a
   non-default value is passed, while the no-argument call stays pixel/value
   identical to before).

**`Settings` fields:**

```python
@dataclass
class Settings:
    # Output
    resolution: str = "1080p (1920x1080)"   # -> {"1080p (1920x1080)": (1920, 1080), ...}
    fps: int = 24
    encoder: str = "libx264"
    crf: int = 20

    # Typography & colors (render.py)
    font_path: str = ""                      # "" = auto-detect (today's _default_font())
    lyric_size: int = 48
    chord_now_size: int = 64
    chord_next_size: int = 32
    accent_color: str = "#38bdf8"
    text_color: str = "#ffffff"
    dim_text_color: str = "#94a3b8"
    panel_color: str = "#0b1220"
    panel_alpha: int = 150                   # 0-255

    # Chord bar (render.py)
    show_chord_timeline: bool = True
    show_key_bpm: bool = True
    timeline_window_sec: float = 12.0

    # Chord detection (detect_chords.py)
    snap_chords_to_key: bool = True
    prefer_flats: bool = True
    include_seventh_chords: bool = False
    min_chord_seconds: float = 0.5
```

A small `RESOLUTIONS` dict (`"1080p (1920x1080)" -> (1920, 1080)`, `"720p (1280x720)"
-> (1280, 720)`, `"1440p (2560x1440)" -> (2560, 1440)`) and `ENCODERS`/`FPS_OPTIONS`
lists live alongside the dataclass, matching LyricChord's `config.py` convention.

**Out of scope** (confirmed with owner):
- LyricChord's entire "Background" section (solid/gradient/loop-video) — we always
  composite over real AI-generated Ken-Burns images; there is no flat-background mode.
- Batch-queue concepts: `skip_existing`, `scan_subfolders`, the drag-drop file queue
  and file listbox — we generate one song at a time, not a batch tool.
- `lyrics_offset_ms`, `use_online_chords`, `acoustid_api_key` — no provider-LRC
  timing, no online-chords cascade, no AcoustID fingerprinting in this program.
- `karaoke_fill` — our word-highlight-box sweep is a different, already-tuned
  mechanism (2026-09-09 color refinements), not a toggle.
- A playback progress bar baked into the rendered video (LyricChord's
  `show_progress_bar` + `draw_progress()`) — not requested; what's being added is a
  GUI-side generation-progress bar instead (see scope item 7).
- Theme presets (Midnight/Sunset/Forest/Mono) and CustomTkinter's own light/dark
  appearance-mode toggle — the Settings fields above already let colors be picked
  individually; a curated preset list is a nice-to-have not requested here.

## Architecture

```
gui.py (CustomTkinter)
  ├─ left column: title/audio/work-dir form + Generate/Redo (existing logic, restyled)
  ├─ right column: SettingsPanel(settings: Settings) — sliders/color-pickers/checkboxes
  ├─ bottom: generation progress bar (new) + status + log console
  └─ on launch: Settings.load(); on each control change: Settings.save()

run_pipeline(audio_path, work_dir, title=None, start_stage="identify",
             font_path=None, settings: Settings | None = None, progress_callback=None)
  ├─ detect_chords(instrumental_stem_path, snap_chords_to_key=settings.snap_chords_to_key,
  │                prefer_flats=settings.prefer_flats,
  │                include_seventh_chords=settings.include_seventh_chords,
  │                min_chord_seconds=settings.min_chord_seconds)
  └─ assemble_video(lines, chord_track, image_dir, audio_path, out_path, font_path,
                     resolution=RESOLUTIONS[settings.resolution], fps=settings.fps,
                     encoder=settings.encoder, crf=settings.crf,
                     accent_color=..., text_color=..., dim_text_color=...,
                     panel_color=..., panel_alpha=..., lyric_size=..., chord_now_size=...,
                     chord_next_size=..., show_chord_timeline=..., show_key_bpm=...,
                     timeline_window_sec=...)
        ├─ passes the color/size/toggle kwargs straight through to draw_scene()/draw_chord_bar()
        └─ write_videofile(fps=fps, codec=encoder, ffmpeg_params=["-crf", str(crf)])
```

`render.py`'s `draw_scene()`/`draw_chord_bar()`, `detect_chords()`, and
`assemble_video()` all take plain keyword arguments (colors as `(r,g,b)` tuples,
sizes as `int`, toggles as `bool`) — **not** a `Settings` object — so those modules
stay decoupled from `settings.py` and independently testable with plain values,
matching this codebase's existing design-for-isolation convention. `run_pipeline()`
is the ONE integration point that accepts a real `Settings` object and unpacks its
fields into those plain keyword arguments when it calls `detect_chords()`/
`assemble_video()` (`Settings.resolution`, a display string, is resolved through the
`RESOLUTIONS` dict to a real `(width, height)` tuple at this boundary).

Every new parameter defaults to today's exact hardcoded value, so `run_pipeline(audio,
work_dir)` with no `settings` argument (the CLI's current behavior, and every existing
test) renders byte-for-byte the same as before this feature.

## Testing

- `test_settings.py`: round-trip `save()`/`load()`, defaults when no file exists,
  tolerance of a missing/extra key (same forward-compatibility discipline as
  `models.py`'s `_song_from_dict`).
- Extend `test_render.py`/`test_detect_chords.py`/`test_assemble.py` with one test
  per new optional parameter: passing a non-default value measurably changes the
  output (a different color appears in the frame, a different chord-detection
  constant changes the result, etc.), and calling with no new arguments reproduces
  today's exact output.
- `gui.py`'s CustomTkinter widget wiring itself stays manually/visually verified
  (matches this project's existing testing-constraint precedent for GUI code) —
  automated tests cover the `Settings` dataclass and the pipeline/render functions
  it feeds, not the widgets themselves.
