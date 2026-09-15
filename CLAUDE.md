# PlayAlongVideoProduction

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

## Running it

- **Linux and Windows both supported.** Launchers: `run_playalongvideoproduction.sh`
  (Linux/macOS) and `run_playalongvideoproduction.bat` (Windows). Code never
  hardcodes the venv interpreter path -- `lyricvideo/venv.py`'s `venv_python()`
  resolves `.venv/bin/python` vs `.venv/Scripts/python.exe`; read every
  `.venv/bin/python` below that way.
  `.gitattributes` keeps `*.sh`/`.githooks/*` LF and `*.bat` CRLF. (HISTORY 2026-09-14.)
- **GUI (normal use):** double-click the `PlayAlongVideoProduction` desktop icon, or
  run `./run_playalongvideoproduction.sh` from the repo root. Launchers call the
  venv's own `python` directly, never `source .venv/bin/activate` (stale baked-in
  `VIRTUAL_ENV` path; HISTORY 2026-09-09). Supply just an audio
  file — title/artist/lyrics are identified and fetched automatically, chords are
  detected directly from the audio, and the title field is an editable override, not
  a required input — then click Generate; work dir (no Browse) falls back to
  the filename if identification isn't done (HISTORY). A "New Song"
  button next to Generate
  clears the form/log/progress bar back to blank without relaunching the app.
  The window's own close (X) button (bound via `root.protocol("WM_DELETE_WINDOW",
  self._on_close_window)` in `__init__` -- the only way to quit) confirms first
  if a Generate/Redo/Batch is actively running -- closing mid-run kills the
  pipeline (and any in-flight upload) partway through with no way to resume;
  closes immediately, no prompt, whenever nothing is running. Tests must invoke
  the registered `WM_DELETE_WINDOW` Tcl callback, not the Python method (the
  binding was once shipped unwired; HISTORY 2026-09-10). A
  "Batch: Process a Folder" section (`lyricvideo/batch.py` finds/resolves the
  files) runs every audio file in a folder through the pipeline sequentially --
  one up-front confirmation decides whether already-done songs are skipped or
  regenerated (backing up each one first, like Redo) for the whole batch; a
  file that errors is logged and skipped, never aborting the rest. The chosen
  folder's path is never `.strip()`'d (a real folder name can carry whitespace),
  and `resolve_existing_folder()` in `lyricvideo/batch.py` recovers a folder
  whose trailing space the native picker itself dropped (real 2026-09-10 bug;
  HISTORY). The batch folder field also remembers the last
  folder used (`load_last_batch_folder`/`save_last_batch_folder` in
  `lyricvideo/batch.py`, a tiny separate JSON file at
  `~/.playalongvideoproduction/batch_state.json` -- deliberately not a
  `Settings` field, since `SettingsPanel.collect()` wholesale-replaces
  `Settings` from its own widgets and would silently reset any field with no
  panel widget behind it) -- prefilled on launch and used as the Browse
  dialog's `initialdir`. Built with
  CustomTkinter
  (`lyricvideo/gui.py`): a two-column layout, left = the single-song form/Generate/
  Redo/log console/generation progress bar, right = the YouTube connect status/
  button/comments panel plus a "⚙ Settings" button. Settings (live preview + the
  scrollable Settings panel) live in their own popup window (`_open_settings_window`,
  same transient/grab_set/lift/focus_force/brief-topmost treatment as the Update
  Available dialog) rather than an embedded tab (HISTORY 2026-09-11) -- re-opening
  while already open lifts the existing window instead of
  building a second `SettingsPanel` bound to the same `Settings` object. Closing the
  popup (its own X button) with unsaved changes prompts the same discard
  confirmation as the panel's own Discard button, then reverts `self.settings` to
  the on-disk baseline before destroying the window. `_open_settings_window` wraps
  its own `SettingsPanel(...)` construction in `self._suppress_settings_save = True`
  (reset to `False` right after): SettingsPanel's `load_from()` fires `on_change`
  before that assignment completes, and `_on_settings_changed` would hit a
  not-yet-assigned attribute (real 2026-09-11 bug, silently swallowed by
  Tkinter; HISTORY). Startup uses the identical guard; it resets the flag only
  once, so a later construction needs its own re-arm. The scrollable Settings panel
  (`lyricvideo/settings_panel.py`) is bound to a `Settings` object
  (`lyricvideo/settings.py`, persisted to `~/.playalongvideoproduction/settings.json`,
  loaded on launch and saved only on explicit Save). Every field shows its own
  default value next to it (`_default_text`, pulled live from a fresh `Settings()`
  so it can't drift); every slider has a typeable value box beside it in addition to
  the draggable slider (parsed/clamped by `_parse_clamped_float`, tolerant of a
  stray "%"/"s" suffix) -- driven off a trace on the slider's own Tk variable rather
  than `CTkSlider`'s `command` callback, since that callback only fires on a live
  drag, never a programmatic `.set()` (real bug found via an actual screenshot,
  2026-09-11: the box showed "0"/"off" instead of the real loaded value on open). An
  unsaved field's row label is bold+orange (was plain orange text), still governed
  by the same `_dirty_fields()`/itemized-confirm-before-Save mechanism as before.
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
  `--work-dir` by an earlier run — useful since `separate`/`fetch_lyrics`/
  `detect_chords`/`images` are the slow/expensive stages.
- Requires `ANTHROPIC_API_KEY` and `REPLICATE_API_TOKEN` in `.env` at the repo
  root (both are set locally; see `.env.example` for the template). No
  Alpaca/trading credentials are involved — this is a separate, unrelated
  project from AITrading despite living alongside it on this machine.

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
   logging why; `LYRICVIDEO_DEVICE=cpu|cuda` overrides and is never
   second-guessed. An auto-picked GPU run that still fails is retried once on
   CPU (too little memory, driver mismatch). Output path convention
   (`work_dir/htdemucs/<audio_stem>/{vocals,no_vocals}.wav`) is what makes
   `--stage` resumption work — later stages look for the file at that same
   path; a resume at fetch_lyrics/align/detect_chords whose stems are missing
   re-runs Demucs instead of crashing. Demucs's own output is relayed through
   `sys.stdout` (`_run_demucs`) so the GUI log shows its progress.
3. **fetch_lyrics** (`fetch_lyrics.py` + `vocal_onset.py`) — plain lyric-line
   text, no manual input required: a sidecar `.lrc`/`.txt` next to the audio file,
   then lrclib.net (edition-consensus voting across every matching-length record,
   using `vocal_onset.py`'s narrow vocal-onset-rise check to disambiguate
   disagreeing first-line candidates), then the `syncedlyrics` aggregator as a
   last resort. Written to `work_dir/lyric_lines.json`. Any timestamps a provider's
   LRC carries are discarded — real timing always comes from the next stage.
   Plain (unsynced) text is split into lines directly, so a file whose
   duration couldn't be probed still keeps its lyrics (9-14).
4. **align** — forced word-level alignment (`align.py`) against the isolated
   vocal stem, timing `fetch_lyrics`'s text; `combine.py` merges the timing onto
   the lines (a last word ending microseconds past the stem's duration is a
   resample rounding artifact, tolerated; issue #6). An empty lyric list raises a
   clear RuntimeError pointing at a sidecar file instead of dying inside the
   aligner. MMS_FA knows only a-z and `'`: `_normalize_word_for_alignment`
   spells digit runs out as sung ("31" -> "thirtyone", "1975" ->
   "nineteenseventyfive", "1st" -> "first"), reads `&` as "and", and gives a
   word with nothing left the model's `*` star token instead of raising
   (issue #3: a lyric word "31" aborted the stage). Display text never changes.
5. **detect_chords** (`detect_chords.py` + `chord_theory.py`) — real chord
   identity, independent of lyrics: `crema` (trained CNN/CRNN, ISC) analyzes
   Demucs's `no_vocals.wav`; its 602-class vocabulary collapses to this
   app's 5 qualities via `_simplify_chord_label()` (every pumpp `3567s`
   quality mapped explicitly, `minmaj7` included -- the `.get()` default is
   `maj`, wrong for any minor-third chord). Replaced CQT-chroma
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
   (3) times before writing a plain-color placeholder; `substitute_fallback_images`
   then replaces any placeholder with the nearest real image in the song's own
   sequence (`is_fallback_image`: a single perfectly solid color), unless every
   image failed. `assemble_video()`'s `get_image` applies the same rule per
   frame: a key with no file behind it renders the nearest real image, never a
   flat color. A missing `REPLICATE_API_TOKEN` raises a clear RuntimeError here.
7. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling lyrics
   (karaoke word-highlight sweep, Ken Burns pans), a NOW/NEXT/segmented-timeline
   chord bar, a Key/BPM badge, and a chord fingering legend
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
   `_legend_layout()` sizes the diagrams from the song's actual chord count (real
   bug 2026-09-09: a 16-chord song wrapped to 4 rows and overlapped the lyrics and
   the chord bar) -- never more than 2 rows, shrinking only as much as needed to
   fit within a reserved upper region, and never growing past `Settings.
   chord_legend_size` (percent, owner-adjustable, default 100%). Each diagram's
   own panel opacity is its own separate owner-tunable slider,
   `Settings.chord_diagram_panel_alpha` (0-255, default 235/near-opaque) --
   originally a fixed constant so it would stay legible against any
   background, then made adjustable (2026-09-10 request) since the right
   amount of transparency is a taste call the panel_alpha used elsewhere
   doesn't control. Every video opens with a `Settings.countdown_beats`
   lead-in (default 4, owner-adjustable in Output, 0 disables it) before the
   song starts, so a musician has a moment to get ready -- a real band's
   count-in is N *beats*, not N seconds, so `assemble_video()` computes
   `beat_duration = 60 / bpm` from the song's own detected
   `chord_track.bpm` (falling back to 120 if undetected/zero) and the
   countdown's actual real-time length is `countdown_beats * beat_duration`
   (owner request, 2026-09-10: "should count down 4, and be in tempo with
   the song"). Frozen on a GUARANTEED-real background (Ken Burns held at
   its own start position, so there's no visual jump into the real content)
   with a small centered `render.draw_countdown()` panel counting down --
   same rounded-box/accent-color language as the chord bar's own NOW/NEXT
   boxes (owner feedback: keep it modest, not "gaudy"), never more than
   ~15% of the frame. `_first_available_image_key()` picks the real first
   moment's own image when its file exists, otherwise ANY real image
   already generated for the song, NEVER the flat `fallback_color` -- real
   owner complaint, 2026-09-10 ("dont have a blank screen... fill it with
   the beginning frame"): some songs' own first-moment image key had no
   cached file, so the countdown fell through to a plain color (intermittent,
   song-specific; HISTORY).
   `assemble_video()`'s inner `make_frame(T)` runs on the OUTER
   (countdown-extended) timeline; real content uses `song_t = T -
   countdown_duration` throughout. Audio is delayed to match
   (`CompositeAudioClip([audio_clip.set_start(countdown_duration)])`), so
   the song's own audio and the real on-screen content always start at the
   exact same instant, right as the countdown reaches zero. Long lyric lines
   wrap onto multiple rows at commas (preferred) or by word (fallback) instead of
   running off the frame edges or ever shrinking the font (`render.py`'s
   `_split_line_into_rows`) -- real bug, a 127-character line overflowed both
   edges before this. `draw_scene`'s current/next-line vertical spacing is
   computed from each shown line's actual (possibly multi-row) height
   (`_rows_and_block_height`), not a fixed single-row gap -- a second real bug
   found on a live redo: the wrap fix alone let a wrapped current line's lower
   rows render on top of the next-line preview underneath it. During an instrumental
   gap (past a line's own `end_time`, before the next line's `start_time`, or
   outside any line at all) the background image follows the active chord
   instead of freezing on the last-sung line — `layout.py`'s `_in_a_line()`
   decides which applies, using `_plausible_sung_intervals()` rather than a
   line's raw `start_time`/`end_time` envelope: a single misaligned word can
   otherwise claim an implausible duration (real bug found live, 2026-09-09 --
   forced alignment gave one word 105 seconds while the rest of that line's
   words were plausibly clustered together 8 seconds later) and make the next
   ~2 minutes falsely read as "still singing," suppressing both the per-chord
   image-follow and Ken Burns pacing. `layout.build_image_timeline()` builds
   the whole song's image schedule once up front (one segment per sung line,
   unchanged; one per instrumental chord otherwise) and merges consecutive
   instrumental chords shorter than `Settings.image_min_hold_seconds`
   (default 2.0s, owner-adjustable) forward into one block until the combined
   span reaches that minimum -- owner complaint, 2026-09-11: fast chord
   changes flipped the background too often. A too-short gap between two
   vocal segments gets no segment -- the prior image just holds through it
   (HISTORY 9-15). Every block boundary is still a
   real chord onset lifted from the detected chord track (never an
   independent timer), so a merged block can only show a chord's own image a
   little LONGER than that chord's raw span, never out of sync with the
   music. `build_scene()`'s Ken Burns window during an instrumental stretch
   paces to that block's own span (not a single absorbed chord's), so a
   merged block's pan doesn't reset partway through. Any image swap (line-to-
   line included) now crossfades over `Settings.image_transition_seconds`
   (default 0.25s) via `render.crossfade_backgrounds()` instead of an instant
   cut, capped to at most 40% of either neighboring segment's own length so a
   briefly-held image never spends its whole visible life mid-fade.
   `render.draw_support_overlay()` optionally burns a small, semi-transparent
   "support this channel" watermark into the LAST `Settings.
   support_overlay_lead_seconds` (default 20s) of every video only -- never
   the countdown, never the whole video -- upper-RIGHT, below the Key/BPM
   badge, NOT upper-left, which is the chord fingering legend's own corner
   (confirmed by rendering an actual composite frame, not just code review,
   since the original placement directly covered the legend).
   `Settings.support_overlay_text` (blank = off) drives only this overlay;
   the separate `Settings.support_description_text` (blank = off) is what
   `schedule_upload()` appends to the YouTube description -- deliberately
   two independent fields, since the overlay is never clickable (no region
   of a rendered video frame can be) but the description needs the real
   `https://` link. Field labels in `settings_panel.py` must stay short --
   one long label once broke rendering for the WHOLE panel (see `_add()`).
   `scripts/backfill_support_overlay_description.py` (manual, re-runnable)
   adds `support_description_text` to already-uploaded videos.
   `build_scene()`'s `scroll_progress` (how far the current line's own
   on-screen scroll animation has advanced) uses `_plausible_line_end()` --
   the same outlier-capped end as `_plausible_sung_intervals()` -- instead of
   the line's raw `end_time` (HISTORY 2026-09-10, "Memoria": one word got a
   6.86s duration). Word-highlight timing (`word_sung`/`word_active`) keys
   only on a word's own start time, never a duration, and is unaffected.
   `build_scene()`'s CURRENT-LINE TEXT is also gated on `_in_a_line()`
   (HISTORY 2026-09-10, "Wish You Were Here": scattered spoken-intro words
   left one line "current" for 85 seconds): once past a line's own
   plausible end, "current" advances to the NEXT line early rather than
   blanking it -- shown as the same unsung preview the pre-first-line intro
   already used (HISTORY 9-15: blanking read as a premature jump). Only
   past the LAST line does it blank. `find_current_line_index` is
   untouched.
   The scrolling timeline lane's per-segment chord label (`render.py`'s
   `_lane_label_font`) shrinks to fit a short-duration chord's narrow box
   instead of being skipped entirely when it doesn't fit at the default size
   (real owner-reported issue, 2026-09-09) -- floored at 18pt. If even that
   doesn't fit, the label is now omitted entirely (`_lane_label_visible`) --
   the colored block itself still draws, so a chord change stays visible,
   but the text no longer overflows into the neighboring segment's own label
   (HISTORY 2026-09-10: a spurious 0.51s chord smeared into the next
   label). No song title or artist text is drawn into the frame
   anywhere (owner decision, 2026-09-09) — only the chord bar, Key/BPM badge, and
   chord legend were added to the frame.

## Redo an Existing Song

`models.py`'s `load_song()` tolerates `lyrics_timed.json` files saved by the
pre-MP3-only-merge pipeline (a `"chord"` key on word dicts, `"instrumental_chords"`
instead of `"chord_track"`) — it reads only `Word`'s own current fields rather than
splatting the whole legacy dict, and a missing `"chord_track"` key degrades to an
empty `ChordTrack` rather than raising. Legacy chord/word data is silently dropped,
never migrated — correct, since Redo resumes before lyrics/chords are regenerated
anyway. Re-runs a previously completed song through the current code, picking up
fixes made since the original run without re-running Demucs. A redo resumes at
`"fetch_lyrics"` (there is no `parsed_tab.json` to reuse post-merge — lyrics are
re-fetched and chords re-detected fresh on every redo, both cheap relative to
Demucs/images), reusing the existing Demucs stems and `work_dir/song_info.json`
(read via the `else` branch of `run_pipeline`'s `identify` stage, since a
`start_stage` past `"identify"` never re-runs it), via `list_redoable_songs()`/
`load_redo_inputs()` (reads `audio_path`/`title` off `lyrics_timed.json`,
preferring a local copy `run_pipeline()` writes into `work_dir`). In
`gui.py`, the "Redo an Existing Song" dropdown lists every `work/` folder with a
completed run; a "Generate new images" checkbox (default off = reuse, matching this
app's existing cost-conscious convention) forces fresh images via
`prepare_images_for_fresh_regeneration()` — it moves the old `images/` dir aside to
`images_prior_<timestamp>/`, deliberately NOT `images_backup_*` (that name is
auto-searched for reuse by the images stage, which would silently defeat "generate
new"). `backup_song_outputs()` always copies the current video + `lyrics_timed.json`
into `work_dir/redo_backup_<timestamp>/` before a redo touches anything.

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
`work/`, `.venv/`). `self.top_frame` (the `before=` anchor `_poll_update_queue`
packs the banner above) must be a `.pack()`-managed child of `self.root` --
it is `body`; anchoring on the grid-managed `left` raised `TclError: window
isn't packed`, silently, so the banner never showed (real 2026-09-09 bug;
HISTORY). `gui.py` checks once on
launch (background thread) and
shows a clickable banner if a newer release exists; clicking it opens a
modal dialog (centered over the main window, `transient`+`grab_set`+`lift`+
`focus_force`, plus a brief `-topmost` toggle — `lift`/`focus_force` alone
are not reliably honored by every Linux window manager (Cinnamon
included; confirmed live, 2026-09-10 recurrence of this same "invisible
dialog" class of bug) — it must never be losable behind the main window)
with the release notes and an Apply Update button (confirms first,
then downloads/reinstalls-dependencies-if-changed/copies/writes the new
VERSION) followed by a Relaunch Now button. No severity tiering, no
periodic re-check, no manual "Check Now" button — see the spec for why.
The Apply Update confirmation (`_on_apply_update_clicked`'s own
`messagebox.askyesno`) is a SEPARATE dialog and needs the identical
`parent=`/topmost treatment (`parent=self._update_dialog_window`) -- it
opened behind the outer dialog without it (HISTORY 2026-09-10).
Cut a release with `scripts/cut_release.sh <version-tag> <notes-file>` (syncs
both launchers, `.sh` and `.bat`; the
releases repo itself was created 2026-09-08, public/unlisted, no source
code — just synced snapshots + release notes). The sync step exports from
git's committed `HEAD` (`git show HEAD:<path>`, never a raw working-tree
`cp`) specifically so uncommitted local changes can never leak into a
public release — see the 2026-09-08 history entry for the real incident
that found this the hard way. `git show ... > file` drops git's executable
bit, so the script re-applies `chmod +x` to any path `git ls-tree HEAD`
tracks as `100755` (the `.sh` launcher once shipped non-executable;
HISTORY 2026-09-10). The owner runs the app directly from this same
git checkout (not a separate deployed copy), so code changes reach them
immediately on every commit; releases exist so the Update Available banner
and changelog stay meaningful, not because Apply Update is the only way
changes reach this install. `v1.1.0` (2026-09-09) is the first release cut
since the project rename — it had drifted to reference the pre-rename
`run_lyricvideogen.sh` (file no longer exists), fixed to
`run_playalongvideoproduction.sh`. `cut_release.sh`/`apply.py` guard
against a stale release reverting newer commits (HISTORY, 9-13).

`Settings.render_kwargs()` centralizes resolution/color unpacking for
`assemble_video()` (an unknown resolution label falls back to 1080p with a
warning, never a KeyError); `run_pipeline()` builds on it. `lyricvideo/settings_preview.py`
(owner request 2026-09-09) renders a synthetic sample frame (fake lyric line + fake
chord track, no real song/network/AI image) at the chosen output resolution using
that same mapping, then downscales it for on-screen display. `gui.py`'s Settings
column is now preview pane (fixed, on top) + the scrollable `SettingsPanel` (which
also gained a "Reset to Defaults" button, confirmed via a dialog, that repopulates
every control from `Settings()` in one on_change firing rather than one per field);
main window widened to 1400x820 to fit it. **`SettingsPanel` never writes to disk
except via its own "Save Settings" button** (2026-09-10, real owner incident: every
slider drag used to call `Settings.save()` immediately, so one accidental bump
silently became the permanent default forever) -- `self._baseline` (the settings
actually on disk) is compared field-by-field against the live widgets on every
change; any field that differs gets a small "●" marker directly on its own label
(`_refresh_dirty_indicators`), and Save Settings/Discard changes only enable when
something is actually dirty. Save shows an itemized `old → new` confirm dialog
for every changed field before writing anything (catches an earlier accidental
change riding along with a later deliberate one -- the exact scenario the owner
described); Discard just reloads `self._baseline`, touching disk not at all.
Closing the app (or a crash) with unsaved changes simply loses them, by design.
`gui.py`'s in-memory `self.settings` still updates live on every change (so the
current session's own Generate/Redo/Batch always uses your latest tweak) -- only
the on-disk file itself is gated behind the explicit Save click. `pipeline.py`'s font-resolution helper
was renamed `_default_font` -> `default_font` (no longer module-private, since the
preview needs it too). Its candidates cover Linux DejaVu/Liberation and Windows
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
write the description/tags, and one more per new comment
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
future `publishAt` (`compute_next_publish_slot()` gap-fills: first date
>= `youtube_min_days_between_uploads` days from every date the channel
already claims, live via `reserved_publish_dates()`, not a file --
9-13), snapped to `youtube_preferred_upload_hour`, rolled to tomorrow
past that hour;
Unlisted/Private upload immediately, no scheduling at all. `lyricvideo/youtube_auth.py` owns the OAuth
connection lifecycle: `connect()` opens the owner's browser once for
consent (using a `client_secret_*.json` downloaded from Google Cloud
Console) and saves a refresh token to
`~/.playalongvideoproduction/youtube_token.json`; `load_credentials()`
returns `None` for "not connected" (never raises) and auto-refreshes an
expired token; `get_channel_title()` confirms which channel is connected. `Settings`
gained seven YouTube fields (`youtube_auto_upload`,
`youtube_client_secrets_path`, `youtube_privacy` default `"public"`,
`youtube_category_id` default `"27"` ("Education" -- real 2026-09-10
finding: YouTube Studio's "How-to" *subcategory* only appears under the
Education top-level category and isn't reachable through the Data API at
all, so the owner sets it manually per video in Studio; the app just
needs to leave the video on Education for that option to be there),
`youtube_made_for_kids` default
`False`, `youtube_min_days_between_uploads` default `2`,
`youtube_preferred_upload_hour` default `15`). `SettingsPanel` gained a
"YouTube" section (client-secrets file picker, auto-upload checkbox,
privacy/category dropdowns, made-for-kids checkbox, two sliders) --
the Category dropdown shows friendly labels ("Howto & Style"/"Education"/
"Music") while `Settings.youtube_category_id` stores the real numeric
YouTube category id; `values_to_settings()`/`load_from()` translate
between the two at the Settings boundary. `gui.py`'s module-level
`_maybe_upload_to_youtube(work_dir, settings)` (testable the same way
`_slugify`/`_split_log_text` already are) gates every auto-upload trigger
-- only if enabled, connected, AND this song was never uploaded before --
and is called from `_run_worker` (shared by both Generate and Redo) and
per-item inside `_run_batch_worker`. "Never uploaded before" is verified
live via `youtube.video_exists(client, video_id)`
(`videos().list(part="id", id=...)`), not just "a `youtube_state.json`
exists" -- real incident 2026-09-10: the owner deleted a video directly
on YouTube Studio after a redo, and the stale local record left that song
permanently stuck claiming "already uploaded" with no automatic recovery.
A verification call that itself fails (network hiccup) fails CLOSED here
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
sole manual-upload UI: a `list_rendered_songs()` single-select scrollable
list (any song, uploaded or not) + Upload -- confirms first if that song
has a `youtube_state.json` -- plus a "Pending YouTube Uploads" checklist
below, live from `list_pending_uploads()` (never-uploaded only), with a
"Select All" toggle and an "Upload Selected" button; both share
`_start_retry_upload()`/`_retry_pending_uploads()`, ignoring
`youtube_auto_upload` (a deliberate click always has). The Redo
dropdown, this list, and the Pending checklist are each
`CTkRadioButton`/`CTkCheckBox` rows in a `CTkScrollableFrame` fixed to
`SONG_LIST_HEIGHT` (~15 rows, scrolls for the rest), in a section that
starts CLOSED (`_make_collapsible_section`), opened on click -- never nest
a `CTkScrollableFrame` in another (9-15). Each row has a Watch button and
✕ that hides it from that list, files untouched.
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
top-level one around the whole method as a last resort) -- real live
crash, 2026-09-10: a video with comments disabled (YouTube returns a
completely normal `HttpError 403 commentsDisabled`, not a bug) was
uncaught, silently aborting the check for every OTHER video too, forever,
since the identical failure recurs on every future 20-minute tick. One
video's failure must never block the rest. `is_video_public()` skips a
still-scheduled video before the call (9-13). That same 20-minute tick
(`_youtube_periodic_tick`, on a background thread -- both
`load_credentials()`'s token refresh and `get_channel_title()` can make a
real network call, never safe on the GUI thread) also refreshes the
connect-status label, so a token that expires mid-session shows "not
connected" within 20 minutes instead of only at next launch. This matters
because a personal single-user OAuth app always stays in Google's
"Testing" publishing status (real published-app verification is 2-6 weeks
and pointless for personal use) -- Testing-mode refresh tokens hard-expire
after exactly 7 days regardless of use, so reconnecting periodically via
the "Connect to YouTube" button is expected, normal behavior, not a bug.
A video redone after being deleted directly on YouTube now re-uploads
automatically on the next Redo/auto-upload check (via the `video_exists()`
self-heal above); short of that specific case, there is still no
automated "corrected video" relinking -- an in-session correction is the
owner's own manual call via the upload button.

This whole feature is complete and tested. What's NOT yet verified: the
interactive OAuth `connect()` flow and live comment fetch/reply, both of which
need the owner's own real Google Cloud `client_secret_*.json` and a real
connected channel to exercise end-to-end. `load_credentials()` returns `None`
("not connected") for a stored token that's expired with no refresh token.

## Tests

```bash
cd <repo root> && .venv/bin/python -m pytest tests/ -v      # Windows: .venv/Scripts/python.exe
```

Two tests self-skip on Windows (trailing-space folder; symlinked destination).
`tests/conftest.py`'s font fixture lists Windows fonts too (before 2026-09-14,
73 render tests silently skipped there). The align tests need FFmpeg's shared
DLLs on PATH (torchcodec) -- an environment requirement, not a code one.
