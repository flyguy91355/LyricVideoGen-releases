# PlayAlongVideoProduction

Generates synced lyric+chord "play along" videos from nothing but an audio file:
karaoke-style scrolling lyrics (forced-aligned to the real vocal stem) and a
NOW/NEXT/timeline chord bar (chords detected directly from the audio, never a
tab/chord sheet) composited over an AI-generated, Ken-Burns background
image that changes per lyric line — and per chord during instrumental
gaps — to follow the song. A separate `deep_review/` program retries the
backlog, cost-tracked (3 searches/attempt, 5-cent/song cap, skips 85%+
untouched) -- HISTORY 9-22. Original tab-PDF design in
`docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md` (superseded,
see below); plan
`docs/superpowers/plans/2026-09-06-tab-pdf-video-generator.md`. MP3-only merge
design: `docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md`, plan
`docs/superpowers/plans/2026-09-09-mp3-only-chord-merge.md`. GUI uses
CustomTkinter with an owner-tunable Settings panel (output resolution/fps/encoder/
crf, chord-bar typography/colors/toggles, chord-detection tuning) — design
`docs/superpowers/specs/2026-09-09-customtkinter-settings-gui-design.md`, plan
`docs/superpowers/plans/2026-09-09-customtkinter-settings-gui.md`.

## Running it

- **Linux and Windows both supported.** Launchers: `run_playalongvideoproduction.sh`
  (Linux/macOS) and `run_playalongvideoproduction.bat` (Windows). Code never
  hardcodes the venv interpreter path -- `lyricvideo/venv.py`'s `venv_python()`
  resolves `.venv/bin/python` vs `.venv/Scripts/python.exe`; read every
  `.venv/bin/python` below that way.
  `.gitattributes` keeps `*.sh`/`.githooks/*` LF and `*.bat` CRLF.
- **GUI (normal use):** double-click the `PlayAlongVideoProduction` desktop icon, or
  run `./run_playalongvideoproduction.sh` from the repo root. Launchers call the
  venv's own `python` directly, never `source .venv/bin/activate` (stale baked-in
  `VIRTUAL_ENV` path; HISTORY 9-09). Supply an audio
  file — title/artist/lyrics are identified and fetched automatically, chords are
  detected from the audio, and the title field is an editable override, not
  a required input — then click Generate; work dir (no Browse) falls back to
  the filename if identification isn't done (HISTORY). A "New Song"
  button beside Generate resets the form/log/progress bar (no relaunch).
  The window's X (`WM_DELETE_WINDOW` -> `_on_close_window`) confirms first while a Generate/Redo/Batch
  runs (closing kills the pipeline and any in-flight upload; no resume), else closes at once (HISTORY 9-22).
  Tests invoke the registered Tcl callback, not the Python method (HISTORY 9-10). A
  "Batch: Process a Folder" section (`lyricvideo/batch.py` finds/resolves the
  files) runs every audio file in a folder through the pipeline sequentially --
  one up-front confirmation decides whether already-done songs are skipped or
  regenerated (backing up each one first, like Redo) for the whole batch; a
  file that errors is logged and skipped, never aborting the rest. An
  interrupted item whose Demucs stems already exist resumes at
  `BatchItem.resume_stage="fetch_lyrics"` instead of redoing the slowest
  stage from scratch (HISTORY 9-18). `batch.py`'s `release_memory()` (gc.collect() + Linux malloc_trim) runs
  after every song, success or failure -- RSS climbs over a long Batch
  without it (HISTORY 9-18). The chosen folder path is never `.strip()`'d; `resolve_existing_folder()` recovers a trailing-space name the picker dropped (HISTORY 9-10). The batch folder field
  also remembers the last folder used (`load_last_batch_folder`/
  `save_last_batch_folder` in `lyricvideo/batch.py`, a separate JSON file --
  not a `Settings` field, since `SettingsPanel.collect()` wholesale-replaces
  `Settings` and would silently reset any field with no widget behind it) --
  prefilled on launch and used as the Browse
  dialog's `initialdir`. Built with CustomTkinter
  (`lyricvideo/gui.py`): a two-column layout, left = the single-song form/Generate/
  Redo/log console/generation progress bar, right = the YouTube connect status/
  button/comments panel plus a "⚙ Settings" button. Settings (live preview + the
  scrollable Settings panel) live in their own popup window (`_open_settings_window`,
  same transient/grab_set/lift/focus_force/brief-topmost treatment as the Update
  Available dialog) rather than an embedded tab (HISTORY 9-11) -- re-opening
  while already open lifts the existing window instead of
  building a second `SettingsPanel` bound to the same `Settings` object. Closing the
  popup (its own X button) with unsaved changes prompts the same discard
  confirmation as the panel's own Discard button, then reverts `self.settings` to
  the on-disk baseline before destroying the window. Constructing a `SettingsPanel` there (and at startup)
  is wrapped in `self._suppress_settings_save = True`, since `load_from()` fires `on_change` early (HISTORY
  2026-09-11); startup resets the flag once, so a later construction re-arms it. The scrollable Settings panel
  (`lyricvideo/settings_panel.py`) is bound to a `Settings` object
  (`lyricvideo/settings.py`, persisted to `~/.playalongvideoproduction/settings.json`,
  loaded on launch and saved only on explicit Save). Every field shows its own default (`_default_text`,
  from a fresh `Settings()`); every slider has a typeable box (`_parse_clamped_float`, tolerant of a
  "%"/"s" suffix), driven off a trace on the slider's Tk variable, not `CTkSlider`'s `command`, which
  only fires on a live drag (HISTORY 9-11). An unsaved field's label is bold+orange
  (`_dirty_fields()`/itemized confirm before Save).
  `render.py`/`detect_chords.py`/`assemble_video()` all take plain keyword arguments
  for every Settings-backed value (colors as RGB tuples, sizes as int, toggles as
  bool) and default to the program's original hardcoded values — they do not import
  `settings.py`; `run_pipeline()` is the sole integration point that accepts a real
  `Settings` object and unpacks it.
- **CLI (staged/resumable, useful for debugging one stage):**
  ```bash
  cd <repo root>
  .venv/bin/python -m lyricvideo.pipeline --audio <path> --work-dir <dir> \
      [--title "<override>"] \
      [--stage identify|separate|fetch_lyrics|align|detect_chords|images|render]
  ```
  `--stage` resumes from a later stage using artifacts already written to
  `--work-dir` by an earlier run -- useful since `separate`/`fetch_lyrics`/
  `detect_chords`/`images` are the slow/expensive stages.
  `run_pipeline()`'s `end_stage` stops early (no chords/images/render) for
  free vetting (9-22).
- Requires `ANTHROPIC_API_KEY` and `REPLICATE_API_TOKEN` in `.env` at the repo
  root (both are set locally; see `.env.example` for the template). No
  Alpaca/trading credentials -- unrelated to AITrading, which lives beside it.

## Pipeline stages (`lyricvideo/pipeline.py`, `STAGES`)

1. **identify** (`identify.py`) — resolves title/artist/duration from ID3/Vorbis/
   M4A tags, filename parsing, and (if the artist is still unknown) lrclib-artist-
   consensus + MusicBrainz-by-duration lookups. Written to `work_dir/song_info.json`.
   A caller-supplied `--title` overrides the identified title for display/filename
   purposes only — artist/duration always come from this stage's own resolution.
2. **separate** (`separate.py`) — Demucs two-stem split of `--audio` into
   vocals/instrumental on `compute_device()` -- `cuda` only when torch sees a
   GPU AND its own build has kernels for that GPU's compute capability
   (`cuda_build_supports_device()`, torch's same-major cubin rule; issue #5:
   `torch.cuda.is_available()` was true on the GT 1030 under a cu130 wheel
   built for sm_75+, and Demucs died on its first kernel launch), else `cpu`,
   logging why; `LYRICVIDEO_DEVICE=cpu|cuda` overrides and is never second-guessed. Stems >3% shorter than the song are rejected. An auto-picked GPU run that still fails is retried once on
   CPU. Output path convention
   (`work_dir/htdemucs/<audio_stem>/{vocals,no_vocals}.wav`) is what makes
   `--stage` resumption work — later stages look for the file at that same
   path; a resume at fetch_lyrics/align/detect_chords whose stems are missing
   re-runs Demucs instead of crashing. Demucs's own output is relayed through
   `sys.stdout` (`_run_demucs`) so the GUI log shows its progress.
3. **fetch_lyrics** (`fetch_lyrics.py`, `lyric_audio_match.py`, `transcribe.py`; HISTORY
   2026-09-18/19) -- `fetch_lyric_lines_verified()` tries each source in turn (sidecar
   `.lrc`/`.txt` beside `work_dir`'s audio copy, 9-15; lrclib.net edition-consensus
   voting, `vocal_onset.py` tie-breaks; each `syncedlyrics` provider) until one passes,
   into `lyric_lines.json`/`Song`. Passing = matching what faster-whisper HEARS in the
   vocal stem (medium, VAD off): >=70% in-order word coverage, no run of >3 unmatched lines
   or >12 sung words the lyrics lack (backing vocals ignored; unsung lead/tail lines dropped). None passing keeps the least-bad match flagged: no auto-upload, listed in "Flagged for Lyrics Review". Claude then
   JUDGES unmatched stretches (`lyric_arbiter.py`: all recognizer failures -> accepted);
   its fix (`lyric_reconcile.py`) is only SAVED as `lyrics_suggested.txt`. `python -m
   lyricvideo.verify_lyrics -h` re-checks finished songs; `replace_report` lists uploaded.
   Whisper unavailable -> old Claude text check. LRC timestamps discarded.
4. **align** — forced word-level alignment (`align.py`, model run in 75 s pieces: flat ~4 GB)
   of `fetch_lyrics`'s text against the vocal stem; `combine.py` merges it onto the lines (a last word
   microseconds past the stem's end is tolerated, issue #6; an empty lyric list
   raises a clear RuntimeError). One whole-song CTC pass DRIFTED 20-50 s on repeated
   choruses, so Whisper word times (`anchors.py`; words in silence dropped) checked
   against lrclib's line timestamps (`combine_anchors`) bound each line to its own
   window (`align_words_anchored`); `precision.py`/`sync.py` only suggest; `timing_gate.py` alone decides (`Settings.timing_pass_percent`, default 90: % of lines within 0.5 s of Whisper's singing) -- best of whole-song/anchored/blended else set aside; pending/flagged lists hold failing older songs, a failing song is HELD before chords/images/render (Flagged: Render Anyway; Remove = hide only); the Upload list (`list_uploadable_songs`) shows only passing; `owner_verified.py` (Mark Verified, or an Upload Anyway the daily limit skips) overrides every check until a Redo; `python -m lyricvideo.timing_gate` reports. A cleared song's real % (not the bar) is in cleared_log.py's note (9-23). MMS_FA knows
   only a-z and `'`: `_normalize_word_for_alignment` spells digits out ("31" ->
   "thirtyone"), reads `&` as "and", and gives a word with nothing left the `*` star
   token (issue #3). Display text never changes.
5. **detect_chords** (`detect_chords.py` + `chord_theory.py`) — real chord
   identity, independent of lyrics: `crema` (trained CNN/CRNN, ISC) analyzes
   Demucs's `no_vocals.wav`; its 602-class vocabulary collapses to this
   app's 5 qualities via `_simplify_chord_label()` (every pumpp `3567s`
   quality mapped explicitly, `minmaj7` included -- the `.get()` default `maj` is wrong for minor thirds). Replaced CQT-chroma
   template matching 9-13 (HISTORY). Needs old TF/Keras/sklearn, no 3.12
   wheels — **`.venv` runs on Python 3.11**; see
   `requirements.txt` pins first. Only chord source, no tab/sheet.
6. **images** — `imagery.py`: one Claude call summarizes the whole song's gist
   once (`summarize_song_gist`), then each *unique* lyric line AND each
   instrumental-stretch caption (`layout.instrumental_image_captions()` -- the
   SAME gap/segment walk `build_image_timeline()` renders from, so every key the
   render looks up was generated; an uncovered sliver adopts its neighboring
   chord's caption, and the generic `[Instrumental]` caption only exists for a
   song with no chords at all) gets its own generated background image
   (Replicate), cached by content hash so a repeated chorus or chord reuses one
   image. Also reuses any `images_backup_*/` archive in the work dir.
   `get_or_generate_image` retries a failed generation `_MAX_GENERATION_ATTEMPTS`
   (3) times, then reuses the song's own most-recent real image
   (`pipeline.py`'s `last_real_image`) instead of a plain color whenever a
   real predecessor exists -- only a song's first image still falls
   back to plain color (HISTORY 9-18). `substitute_fallback_images`
   still replaces any remaining placeholder with the nearest real image in
   the song's own sequence (`is_fallback_image`: a single perfectly solid
   color), unless every image failed. `assemble_video()`'s `get_image`
   applies the same rule per
   frame: a key with no file behind it renders the nearest real image, never a
   flat color. A missing `REPLICATE_API_TOKEN` raises a clear RuntimeError here.
   **Shared image library** (spec `2026-09-25-shared-image-library-design.md`; `Settings.use_image_library`, OFF
   until the owner reviews `scripts/preview_library_matches.py`'s leave-one-out contact sheet in `reports/`, and
   `image_library_min_score`, provisional 0.34):
   before buying, the images stage looks in `~/PlayAlongVideoProductionImages/` (`image_library.py`: SQLite + PNGs
   deduped by picture; env `PLAYALONG_IMAGE_LIBRARY`) for a picture close to Claude's prompt by local CLIP
   (`clip_embedder.py`; a pipeline run never downloads the ~605 MB weights -- only `scripts/import_image_library.py`
   [`--dry-run`/`--limit N`; re-runnable; skips plain-colour placeholders], which also catches up old images, may). `library_session.py`'s `LibrarySession` COPIES a hit into
   `work/<song>/images/` (one picture serves one line per song), files every purchase with its real prompt, and on
   any error disables itself for that song. Redo's "Generate new images" passes `fresh_images=True` (skips the
   lookup). `python -m lyricvideo.image_library stats` shows savings.
7. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling lyrics
   (karaoke word-highlight sweep, Ken Burns pans), a NOW/NEXT/segmented-timeline
   chord bar, a Key/BPM badge (paneled, 9-23), and a chord fingering legend
   (`lyricvideo/chord_shapes.py` + `chord_diagram.py`) over the audio into the
   final mp4 (`work_dir/<slugified-title>.mp4`); all text is drawn via
   `render.load_font()` (a per-thread font cache -- never share FreeType faces
   across the GUI and worker threads). `assemble_video()` closes its
   `AudioFileClip` in a `finally` (moviepy never does; each render leaked an
   ffmpeg reader) and caches backgrounds pre-scaled to the frame so
   `apply_ken_burns()` skips a per-frame resample. The legend shows one small
   guitar diagram per unique chord in the song (`pipeline.ordered_unique_chords()`,
   first-appearance order), upper-left, with the currently-playing chord's
   diagram highlighted; fingering data is extracted from `tombatossals/chords-db`
   (MIT licensed), not hand-authored — every one of the 12 roots x 5 qualities
   `detect_chords()` can produce resolves to a real shape. `chord_diagram.py`'s
   `_legend_layout()` sizes the diagrams from the song's actual chord count
   (HISTORY 9-09) -- never more than 2 rows, shrinking as needed to
   fit within a reserved upper region, and never growing past `Settings.
   chord_legend_size` (percent, owner-adjustable, default 100%). Each diagram's
   own panel opacity is its own owner-tunable slider,
   `Settings.chord_diagram_panel_alpha` (0-255, default 235/near-opaque;
   made adjustable 2026-09-10, previously a fixed constant). Every video
   opens with a `Settings.countdown_beats` lead-in (default 4; 0 disables) -- N *beats*, so `assemble_video()`
   uses `beat_duration = 60 / bpm` from `chord_track.bpm` (120 if undetected) and the count-in lasts
   `countdown_beats * beat_duration` (HISTORY 9-10). It sits on a GUARANTEED-real background (Ken Burns held at
   its start; `_first_available_image_key()` picks the first moment's own image, else ANY real image, NEVER the
   flat `fallback_color`; HISTORY 9-10) under a small centered `render.draw_countdown()` panel (chord-bar box
   style, capped ~15% of the frame). `make_frame(T)` runs on the OUTER countdown-extended timeline
   (`song_t = T - countdown_duration`) and the audio is delayed to match
   (`CompositeAudioClip([audio_clip.set_start(countdown_duration)])`). Long lyric lines
   wrap onto multiple rows at commas (preferred) or by word (fallback) instead of
   running off the frame edges or ever shrinking the font (`render.py`'s
   `_split_line_into_rows`). `draw_scene`'s current/next-line vertical spacing is
   computed from each shown line's actual (possibly multi-row) height
   (`_rows_and_block_height`), not a fixed single-row gap, so a wrapped
   current line's lower rows never render on top of the next-line preview
   underneath it. During an instrumental
   gap (past a line's own `end_time`, before the next line's `start_time`, or
   outside any line at all) the background image follows the active chord
   instead of freezing on the last-sung line — `layout.py`'s `_in_a_line()`
   decides which applies, using `_plausible_sung_intervals()` rather than a
   line's raw `start_time`/`end_time` envelope: a single misaligned word can
   otherwise claim an implausible duration (HISTORY 9-09) and make the next
   several minutes falsely read as "still singing," suppressing both the per-chord
   image-follow and Ken Burns pacing. `layout.build_image_timeline()` builds
   the whole song's image schedule once up front (one segment per sung line,
   unchanged; one per instrumental chord otherwise) and merges consecutive
   instrumental chords shorter than `Settings.image_min_hold_seconds`
   (default 2.0s, owner-adjustable) forward into one block until the combined
   span reaches that minimum (HISTORY 9-11). A too-short gap gets no
   segment -- the prior image holds through it (9-15). Every block boundary is still a
   real chord onset lifted from the detected chord track (never an
   independent timer), so a merged block can only show a chord's own image a
   little LONGER than that chord's raw span, never out of sync with the
   music. `build_scene()`'s Ken Burns window during an instrumental stretch
   paces to that block's own span (not a single absorbed chord's), so a
   merged block's pan doesn't reset partway through. Any image swap (line-to-
   line included) now crossfades over `Settings.image_transition_seconds`
   (default 0.25s) via `render.crossfade_backgrounds()` instead of a cut,
   capped to 40% of either neighbor segment's own length so a
   briefly-held image never spends its whole visible life mid-fade.
   `render.draw_support_overlay()` optionally burns a small semi-transparent "support this channel" watermark
   into the LAST `Settings.support_overlay_lead_seconds` (default 20s) only -- never the countdown -- upper-RIGHT
   below the Key/BPM badge (HISTORY 9-11). `Settings.support_overlay_text` (blank = off) drives only the overlay;
   the separate `Settings.support_description_text` (blank = off; a multi-line box in Settings) is placed by
   `schedule_upload()` right after the YouTube description's FIRST sentence (`place_support_text`, HISTORY 9-26) --
   deliberately two fields, since a video frame is never clickable but the description needs the real `https://`
   link. Field labels in `settings_panel.py` must stay short (one long label once broke the WHOLE panel; see
   `_add()`). `scripts/update_support_description.py` (manual, re-runnable, backs up first, `--only`/`--dry-run`)
   moves it on already-uploaded videos; `backfill_support_overlay_description.py` is the older append-only one.
   `build_scene()`'s `scroll_progress` (how far the current line's own
   on-screen scroll animation has advanced) uses `_plausible_line_end()` --
   the same outlier-capped end as `_plausible_sung_intervals()` -- instead of
   the line's raw `end_time` (HISTORY 9-10). Word-highlight timing (`word_sung`/`word_active`) keys
   only on a word's own start time, never a duration, and is unaffected.
   `build_scene()`'s CURRENT-LINE TEXT is also gated on `_in_a_line()`
   (HISTORY 9-10): once past a line's own plausible end, "current"
   advances to the NEXT line early rather than blanking it -- shown as the
   same unsung preview the pre-first-line intro already used (9-15); past
   the last line it still blanks. That preview itself stays hidden until
   `Settings.lyric_preview_lead_seconds` (default 3.0s) before the line's
   own start -- no lyrics through most of an intro or solo.
   The scrolling timeline lane's per-segment chord label (`render.py`'s
   `_lane_label_font`) shrinks to fit a short-duration chord's narrow box
   instead of being skipped entirely when it doesn't fit at the default
   size -- floored at 18pt. If even that
   doesn't fit, the label is now omitted entirely (`_lane_label_visible`) --
   the colored block itself still draws, so a chord change stays visible,
   but the text no longer overflows into the neighboring segment's own
   label (HISTORY 9-10). No song title or artist text is drawn into the frame
   anywhere (owner, 9-09) — only the chord bar, Key/BPM badge, and
   chord legend were added to the frame.

## Redo an Existing Song

`models.py`'s `load_song()` tolerates `lyrics_timed.json` files saved by the
pre-MP3-only-merge pipeline (a `"chord"` key on word dicts, `"instrumental_chords"`
instead of `"chord_track"`) — it reads only `Word`'s own current fields rather than
splatting the whole legacy dict, and a missing `"chord_track"` key degrades to an
empty `ChordTrack` rather than raising. Legacy chord/word data is silently dropped
(Redo regenerates it). Re-runs a completed song through current code, picking up
fixes since the original run without re-running Demucs. A redo resumes at
`"fetch_lyrics"` (lyrics/chords re-fetch/re-detect fresh every redo -- cheap
relative to Demucs/images), reusing the existing Demucs stems and
`work_dir/song_info.json`
(read via the `else` branch of `run_pipeline`'s `identify` stage, since a
`start_stage` past `"identify"` never re-runs it), via `list_redoable_songs()`/
`load_redo_inputs()` (reads `audio_path`/`title` off `lyrics_timed.json`,
preferring a local copy `run_pipeline()` writes into `work_dir`). In
`gui.py`, "Redo an Existing Song" lists every `work/` folder with a
completed run; a "Generate new images" checkbox (default off = reuse, this
app's cost-conscious default) forces fresh images via
`prepare_images_for_fresh_regeneration()` — it moves the old `images/` dir aside to
`images_prior_<timestamp>/`, deliberately NOT `images_backup_*` (that name is
auto-searched for reuse by the images stage, which would silently defeat "generate
new"). `backup_song_outputs()` always copies the current video + `lyrics_timed.json`
into `work_dir/redo_backup_<timestamp>/` before a redo touches anything. It also logs the redo (`redo_log.py`: redone songs already on YouTube still need replacing).

## Notable pinned dependency

`requirements.txt` pins `moviepy>=1.0.3,<2.0` and `decorator<5.0,>=4.0.2`:
moviepy 1.0.3's decorators silently break under `decorator>=5.0` (fps
resolution returns `None`). Don't bump either without re-verifying rendered
output, not just that imports succeed.

`.venv`'s Python 3.11 must be a real system install (`python3.11-tk` via
`deadsnakes`), never `uv`'s standalone build -- its bundled Tk lacks Xft
and silently breaks the GUI's font (HISTORY, 2026-09-14).

## Update Available Feature

See `docs/superpowers/specs/2026-09-08-update-available-design.md`.
`VERSION` at the repo root tracks the last version actually applied to
this checkout (never hand-edited, never bumped per-commit).
`lyricvideo/update/` provides version parsing/comparison
(`version.py`), a GitHub Releases API client (`release_client.py`,
httpx-based) against the public, unlisted `flyguy91355/LyricVideoGen-releases`
repo — still the pre-rename name as of the 2026-09-09 project rename to
PlayAlongVideoProduction, deliberately not moved yet (see CLAUDE_HISTORY) —
and allow-listed archive extraction/copy
(`apply.py` — allows `lyricvideo/`, `tests/`, `docs/`, `requirements.txt`,
`CLAUDE.md`, a bare top-level `*.py`/`*.sh`; denies `.env`, `songs/`,
`work/`, `.venv/`). `self.top_frame` (the update banner's `before=` anchor) must be
`.pack()`-managed (HISTORY 9-09). `gui.py` checks once on
launch (background thread) and
shows a clickable banner if a newer release exists; clicking it opens a
modal dialog (centered over the main window, `transient`+`grab_set`+`lift`+
`focus_force`, plus a brief `-topmost` toggle -- `lift`/`focus_force` alone
aren't reliably honored by every Linux WM)
with the release notes and an Apply Update button (confirms first,
then downloads/reinstalls-deps-if-changed/copies/writes the new
VERSION) then a Relaunch Now button. No severity tiering, no
periodic re-check, no manual "Check Now" -- see the spec for why.
The Apply Update confirmation (`_on_apply_update_clicked`'s `askyesno`) is a SEPARATE dialog and needs the same
`parent=self._update_dialog_window`/topmost treatment (HISTORY 9-10).
Cut a release with `scripts/cut_release.sh <version-tag> <notes-file>` (syncs both launchers; releases repo is public).
The sync exports from committed `HEAD` (`git show HEAD:<path>`, never a working-tree `cp`), so uncommitted changes
never leak into a public release (HISTORY 2026-09-08), and re-applies `chmod +x` to paths `git ls-tree HEAD`
tracks as `100755` (HISTORY 9-10). The owner runs the app directly from this same
git checkout, so code changes reach them
immediately on every commit; releases exist so the Update Available banner
and changelog stay meaningful, not because Apply Update is the only way
changes reach this install. `cut_release.sh`/`apply.py` guard
against a stale release reverting newer commits (HISTORY, 9-13).

`Settings.render_kwargs()` centralizes resolution/color unpacking for
`assemble_video()` (an unknown resolution label falls back to 1080p with a
warning, never a KeyError); `run_pipeline()` builds on it. `lyricvideo/settings_preview.py`
(owner, 9-09) renders a synthetic sample frame (fake lyric line + fake
chord track, no real song/network/AI image) at the chosen output resolution using
that same mapping, then downscales it for on-screen display. `gui.py`'s Settings
column is now preview pane (fixed, on top) + the scrollable `SettingsPanel` (which
also gained a confirmed "Reset to Defaults" button that repopulates every control
from `Settings()` in one on_change firing);
main window 1600x1000 (review rows: two button rows). **`SettingsPanel` never writes to disk
except via its own "Save Settings" button** (HISTORY 9-10) -- `self._baseline` (the settings
on disk) is compared field-by-field against the live widgets on every
change; any field that differs gets a small "●" marker directly on its own label
(`_refresh_dirty_indicators`), and Save Settings/Discard changes only enable when
something is dirty. Save shows an itemized `old → new` confirm dialog
for every changed field before writing anything (catches an accidental change
riding along with a later deliberate one); Discard reloads
`self._baseline`, touching disk not at all.
Closing the app (or a crash) with unsaved changes loses them, by design.
`gui.py`'s in-memory `self.settings` still updates live on every change (so the
current session's own Generate/Redo/Batch always uses your latest tweak) -- only
the on-disk file itself is gated behind the explicit Save click. `pipeline.py`'s `default_font()` (public; the
preview uses it too). Its candidates cover Linux DejaVu/Liberation and Windows
Arial Bold / Segoe UI Bold.

`lyricvideo/chord_shapes.py` holds guitar fingering data for every chord
`detect_chords()` can produce, extracted from tombatossals/chords-db (MIT).
`lyricvideo/chord_diagram.py` draws a full chord-fingering legend from that
data, wired into `assemble_video()` (new `chord_legend_labels`/
`show_chord_legend` parameters, drawn every frame with the live current chord)
and threaded all the way through `run_pipeline()` via
`pipeline.ordered_unique_chords()`. `Settings.show_chord_legend` toggle (in `render_kwargs()` too) has a
`SettingsPanel` checkbox and shows up in the live preview pane too.

**YouTube upload + channel management** (shipped 2026-09-10, per
`docs/superpowers/plans/2026-09-10-youtube-upload-channel-management.md`):
`lyricvideo/youtube_state.py` tracks each song's own upload (`video_id`,
`uploaded_at`, `title`) in a `youtube_state.json` alongside that song's
`lyrics_timed.json`, so the app knows which of its own uploads exist (used
to skip auto-re-uploading a song on Redo, and to scope comment monitoring
to only videos this app posted). See
`docs/superpowers/specs/2026-09-10-youtube-upload-design.md` for the full
design, including why publishing is scheduled via YouTube's own
`publishAt` rather than a local queue. `lyricvideo/youtube.py` wraps the
raw YouTube Data API v3 calls (`upload_video`, `list_new_comments`,
`post_reply`), each taking an already-built API client as its first
argument -- same "inject the client, fake it in tests" pattern as
`imagery.py`'s `anthropic_client`/`http_client`, so nothing here ever
makes a real network call in tests. New dependencies:
`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`.
`lyricvideo/youtube_metadata.py` spends one small Claude call per upload
(`generate_video_metadata`, same cost profile as the image prompts) to
write the description/tags (thinking off; retried 3x, then RAISES -- never a blank description/tags, so
`schedule_upload` never uploads a bare video; HISTORY 9-26), and one more per new comment
(`draft_comment_reply`) to draft a reply and flag whether it looks like an
error report -- both parse a labeled-line reply format
(`DESCRIPTION:`/`TAGS:` or `IS_ERROR_REPORT:`/`REPLY:`) that's robust to
Claude reordering the lines. The video TITLE is never Claude-authored (HISTORY, 2026-09-14) -- always
the deterministic `build_play_along_title()`: `"{title} - {artist} -
(Play Along Lyrics & Chords)"` (artist omitted when unknown). The GUI's
"Song title" field shows a live, non-editable preview of this string
underneath it without feeding it into the field's own value, which is
also `run_pipeline`'s `--title` override (lyrics search + filename).
`lyricvideo/youtube_schedule.py`'s
`schedule_upload()` is the single upload code path (auto AND manual): for
a Public target it uploads immediately as YouTube-Private with a computed
future `publishAt` (`compute_next_publish_slot()` gap-fills against the
channel's own live schedule, `reserved_publish_datetimes()`, not a file --
9-13; multiple-times-a-day scheduling via `Settings.youtube_upload_times`,
quota/`uploadLimitExceeded` cooldown via `Settings.youtube_quota_retry_hours`,
HISTORY 9-17/9-19). Unlisted/Private upload immediately, no scheduling. `lyricvideo/youtube_auth.py` owns the OAuth
connection lifecycle: `connect()` opens the owner's browser once for
consent (using a `client_secret_*.json` downloaded from Google Cloud
Console) and saves a refresh token to
`~/.playalongvideoproduction/youtube_token.json`; `load_credentials()`
returns `None` for "not connected" (never raises) and auto-refreshes an
expired token; `get_channel_title()` confirms the connected channel. `Settings` holds the
YouTube config fields (auto-upload toggle, client secrets path, privacy,
`youtube_category_id` default `"27"` ("Education" -- HISTORY 9-10),
made-for-kids, publish times/uploads-per-day, the separate enforced
`youtube_max_uploads_per_day` cap, and quota-retry hours -- see
`settings.py`'s YouTube block for the full field list and current defaults).
`SettingsPanel` gained a
"YouTube" section (secrets picker, auto-upload checkbox, privacy/category
dropdowns, made-for-kids box, publish-times box, "Maximum publish/uploads
per day" sliders) --
the Category dropdown shows friendly labels ("Howto & Style"/"Education"/
"Music") while `Settings.youtube_category_id` stores the real numeric
YouTube category id; `values_to_settings()`/`load_from()` translate
between the two at the Settings boundary. `gui.py`'s module-level
`_maybe_upload_to_youtube(work_dir, settings)` gates every auto-upload trigger
-- only if enabled, connected, AND this song was never uploaded before --
and is called from `_run_worker` (shared by both Generate and Redo) and
per-item inside `_run_batch_worker`. "Never uploaded before" is verified
live via `youtube.video_exists(client, video_id)`
(`videos().list(part="id", id=...)`), not just "a `youtube_state.json`
exists" (HISTORY 9-10). A verification call that itself fails (network hiccup) fails CLOSED here
(skip, never risk a duplicate upload). Any upload failure is caught and
logged as a warning, never raised. GUI worker threads format an error's
text BEFORE the deferred `root.after` lambda: `except ... as e` unbinds `e`
when the block ends, so a lambda reading it raised NameError and the
connect/upload/Approve error dialogs never showed (9-14). Generate
and Redo refuse up front, naming the path, when the audio file is gone.
A "YouTube: not connected"/"YouTube:
connected as <channel>" status label + "Connect to YouTube" button sit
above the Settings panel (refreshed on a background thread -- YouTube is
never called from the GUI thread). "Upload to YouTube" (below Redo) is the
sole manual-upload UI: a `list_rendered_songs()` single-select list (any
song, uploaded or not) + Upload -- confirms first if that song has a
`youtube_state.json` -- plus a "Pending YouTube Uploads" checklist below,
live from `list_pending_uploads()` (never-uploaded, cleared;
`list_flagged_songs()` backs a review list too, HISTORY 9-18), with
"Select All" and an "Upload Selected" button; both share
`_start_retry_upload()`/`_retry_pending_uploads()`, ignoring
`youtube_auto_upload` (deliberate). A "Flagged for
Lyrics Review" panel (same lazy pattern) shows each flagged song's concern
text with Watch (else Play MP3), Whisper Text (per lyric line, close tested, HISTORY 9-22), Edit Lyrics (saved to `lyrics_owner.txt`, reused by Redo), Redo and Upload Anyway buttons; `_maybe_upload_to_youtube()` skips any flagged song
outright. `_run_batch_worker` emits a `"batch_item_done"` queue message
after each song so these three lists update live during a long Batch
not just at the end (real gap, HISTORY 9-18).
Redo's list, this
one, and the Pending checklist are each `CTkRadioButton`/`CTkCheckBox`
rows in a `CTkScrollableFrame` fixed to
`SONG_LIST_HEIGHT` (~15 rows, scrolls for rest), in a section starting
CLOSED (`_make_collapsible_section`), opened on click -- never nest a
`CTkScrollableFrame` in another. Rows build lazily on first expand, never
at launch or a later refresh while closed (`invalidate()` marks stale;
9-15). Each row: a Watch button and ✕ that hides it, files untouched.
`lyricvideo/youtube_comment_state.py`
persists which comment ids have already been seen
(`load_seen_comment_ids`/`mark_comments_seen`) and which drafted replies
are still awaiting the owner's review (`PendingReply` +
`load_pending_replies`/`add_pending_reply`/`remove_pending_reply`) --
both flat local JSON files, so pending drafts survive an app restart. A
"YouTube Comments" panel (bottom of the right-hand column, below Settings)
lists pending drafts with an editable reply box, an "⚠ possible error
report" badge, and Approve (posts via `post_reply`)/Dismiss buttons --
nothing ever posts without that explicit click. A "Check Now" button plus
a 20-minute `root.after` timer (only while the app is open) scan every
song with a `youtube_state.json` for new comments, scoped to only videos
this app uploaded; one Claude call per new comment drafts a reply and
flags likely error reports. `_check_youtube_comments_worker` isolates each
video's own `list_new_comments()` call in its own try/except (plus a
top-level one around the whole method as a last resort), so one video's
failure (e.g. comments disabled) never blocks checking the rest
(HISTORY 9-10). `is_video_public()` skips a
still-scheduled video before the call (9-13). That same 20-minute tick
(`_youtube_periodic_tick`, on a background thread -- both
`load_credentials()`'s token refresh and `get_channel_title()` can make a
real network call, never safe on the GUI thread) also refreshes the
connect-status label, so a token that expires mid-session shows "not
connected" within 20 minutes instead of only at next launch. This matters
because a personal single-user OAuth app always stays in Google's
"Testing" publishing status (real verification is 2-6 weeks, pointless
for personal use) -- Testing-mode tokens hard-expire after exactly 7 days,
so reconnecting periodically via "Connect to YouTube" is expected, not a bug.
A video redone after being deleted directly on YouTube now re-uploads
automatically on the next Redo/auto-upload check (via the `video_exists()`
self-heal above); short of that case, there is no automated
"corrected video" relinking -- a correction is the owner's own manual
call via the upload button.

**Channel organization** (playlists + per-video engagement comment,
2026-09-17, spec `2026-09-17-youtube-channel-organization-design.md`):
`YoutubeState` gained `engagement_comment_posted` (default `False`, so
pre-existing `youtube_state.json` files still load) tracking whether a
video's engagement comment has posted. `youtube_comment_state.py`'s new
`PendingComment` mirrors `PendingReply`. `youtube.py` gained playlist
calls and `post_top_level_comment` (`commentThreads().insert`, unlike
`post_reply`'s `comments().insert`). `youtube_metadata.py` gained
`classify_genre` and `draft_engagement_comment`. New
`youtube_playlist_state.py` persists cached playlist ids and the growing
genre list. `youtube_playlists.py`'s `organize_video()` adds a video to an
All playlist, one per listed artist (exact string; a curated exception
list keeps a comma-in-its-name band, e.g. Crosby Stills Nash & Young,
from splitting into 3 (9-23), and
a Genre playlist via `get_or_create_playlist()`/the now-public
`add_video_to_playlist_with_retry()` (self-heal/retry a fresh 404 lag);
idempotent. `gui.py` calls it
after every `schedule_upload()`
-- failing soft. A "Pending Engagement Comments" panel (Redo/Upload's
lazy-build pattern) has Approve (posts, marks
`engagement_comment_posted`) and Dismiss.
`scripts/backfill_channel_organization.py` applies this to older uploads,
loads `.env` itself, and stops cleanly on a quota error instead of
failing every remaining song (9-17). EASY/3-/4-CHORD use `is_easy_key`
(open C/D/E/G/A/Am/Dm/Em, F/B excluded). `pipeline.build_capo_variant()`
(9-23 spec) renders `<slug>/easychords` (nested): same audio/lyrics/images, chords
re-spelled via `capo_and_shape_key()`, `draw_capo_badge()` shows CAPO N under
the legend, Key badge the original key (`key_label`); Settings/Redo/backfill/upload triggers exist -- `easy_chord_capo.json` backs the title/description.

This feature is complete and tested; the interactive OAuth `connect()` flow
and live comment/engagement-comment posting have run live against the
real channel (9-18).
`load_credentials()` returns `None` ("not connected") for a stored token
that's expired with no refresh token. Approving a pending engagement comment
checks `is_video_public()` first, like the comment-reading path (9-18).

## Tests

```bash
cd <repo root> && .venv/bin/python -m pytest tests/ -v      # Windows: .venv/Scripts/python.exe
```

Two tests self-skip on Windows (trailing-space folder; symlinked destination).
`tests/conftest.py`'s font fixture lists Windows fonts too (73 render tests skipped there
before 2026-09-14). The align tests need FFmpeg's shared
DLLs on PATH (torchcodec) -- an environment requirement, not a code one.
