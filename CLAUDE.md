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
  a required input — then click Generate. A "New Song" button next to Generate
  clears the form/log/progress bar back to blank without relaunching the app.
  The window's own close (X) button (bound via `root.protocol("WM_DELETE_WINDOW",
  self._on_close_window)` in `__init__` -- the only way to quit) confirms first
  if a Generate/Redo/Batch is actively running -- closing mid-run kills the
  pipeline (and any in-flight upload) partway through with no way to resume;
  closes immediately, no prompt, whenever nothing is running. Real incident,
  2026-09-10: the first attempt shipped `_on_close_window()` itself correctly
  but never actually wired the `root.protocol(...)` binding, so clicking X
  still closed unconditionally -- verifying by calling the handler method
  directly proved the method's own logic but not that a real close ever
  reaches it. Now verified by actually invoking the registered
  `WM_DELETE_WINDOW` Tcl callback (`root.tk.call(root.protocol("WM_DELETE_WINDOW"))`),
  not the Python method directly. A
  "Batch: Process a Folder" section (`lyricvideo/batch.py` finds/resolves the
  files) runs every audio file in a folder through the pipeline sequentially --
  one up-front confirmation decides whether already-done songs are skipped or
  regenerated (backing up each one first, like Redo) for the whole batch; a
  file that errors is logged and skipped, never aborting the rest. The chosen
  folder's path is never `.strip()`'d -- unlike the typed title/audio/work-dir
  fields, this value comes verbatim from the OS file dialog, and a real folder
  name can legitimately have leading or trailing whitespace. That alone wasn't
  enough (real bug found live, 2026-09-10: a folder literally named
  "batch music " with a trailing space still 404'd after removing the
  `.strip()`, because the native folder-picker dialog itself drops the
  trailing space before the path ever reaches this app's code) -- fixed with
  `resolve_existing_folder()` in `lyricvideo/batch.py`, which falls back to
  matching a sibling directory by whitespace-insensitive name when the exact
  path the dialog returned doesn't exist, so the owner never has to rename a
  folder to work around it. The batch folder field also remembers the last
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
  Available dialog) rather than an embedded tab (owner feedback, 2026-09-11: felt
  cramped/"ugly"; a bare `CTkTabview` "Settings" tab was the 2026-09-10 fix before
  that) -- re-opening while already open lifts the existing window instead of
  building a second `SettingsPanel` bound to the same `Settings` object. Closing the
  popup (its own X button) with unsaved changes prompts the same discard
  confirmation as the panel's own Discard button, then reverts `self.settings` to
  the on-disk baseline before destroying the window. `_open_settings_window` wraps
  its own `SettingsPanel(...)` construction in `self._suppress_settings_save = True`
  (reset to `False` right after) -- SettingsPanel's own `load_from()` fires
  `on_change` once before that assignment completes, so without this guard
  `_on_settings_changed`'s `self.settings_panel.collect()` hits an attribute that
  doesn't exist yet (real bug found live, 2026-09-11: silently swallowed by
  Tkinter with no visible error, aborting construction before `.pack()` ever ran --
  the whole panel invisible below the live preview). Startup uses the identical
  guard for the identical reason; it only resets the flag once, so a second,
  later construction needs its own re-arm. The scrollable Settings panel
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
  cd /home/doug/PlayAlongVideoProduction
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
   vocals/instrumental (CPU). Output path convention
   (`work_dir/htdemucs/<audio_stem>/{vocals,no_vocals}.wav`) is what makes
   `--stage` resumption work — later stages look for the file at that same
   path rather than re-running Demucs.
3. **fetch_lyrics** (`fetch_lyrics.py` + `vocal_onset.py`) — plain lyric-line
   text, no manual input required: a sidecar `.lrc`/`.txt` next to the audio file,
   then lrclib.net (edition-consensus voting across every matching-length record,
   using `vocal_onset.py`'s narrow vocal-onset-rise check to disambiguate
   disagreeing first-line candidates), then the `syncedlyrics` aggregator as a
   last resort. Written to `work_dir/lyric_lines.json`. Any timestamps a provider's
   LRC carries are discarded — real timing always comes from the next stage.
4. **align** — forced word-level alignment (`align.py`) against the isolated
   vocal stem, timing `fetch_lyrics`'s text; `combine.py` merges the timing onto
   the lines. `align_words()` has no idea where its input words came from, so this
   is the same alignment mechanism the original tab-PDF design used.
5. **detect_chords** (`detect_chords.py` + `chord_theory.py`) — real chord
   identity, entirely independent of lyrics: harmonic/percussive separation → CQT
   chroma → beat-sync → template match against 12-root × {maj, min, 7, min7, maj7}
   → key-aware (Krumhansl-Schmuckler) Viterbi decoding, run on Demucs's own
   `no_vocals.wav`. Produces one `ChordTrack` (events + key + bpm) covering the
   whole song, saved onto the `Song`. This is the ONLY chord source in this
   program — there is no tab/sheet input to defer to, and audio-detected chords
   always win.
6. **images** — `imagery.py`: one Claude call summarizes the whole song's gist
   once (`summarize_song_gist`), then each *unique* lyric line AND each distinct
   chord label that occurs during an instrumental gap (`_instrumental_chord_labels`
   in `pipeline.py`) gets its own generated background image (Replicate), cached by
   content hash so a repeated chorus or a repeated chord anywhere in the song
   reuses one image instead of paying to regenerate it. Also reuses any
   `images_backup_*/` archive left in the work dir before generating new images.
   `get_or_generate_image` retries a failed generation up to
   `_MAX_GENERATION_ATTEMPTS` (3) times before falling back to a plain-color
   placeholder -- but a flat placeholder visibly breaks a finished video (real
   owner complaint, 2026-09-10), so it's never left as the final answer for a
   line unless every single image in the whole song failed: once every line's
   and instrumental caption's image has been generated for the run,
   `substitute_fallback_images` (also in `imagery.py`, called from the images
   stage in `pipeline.py`) replaces any remaining fallback with a copy of the
   nearest real, successfully-generated image in the song's own sequence
   (`is_fallback_image` detects one by its unmistakable signature -- a single
   perfectly solid color, which a real AI-generated image never is).
7. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling lyrics
   (karaoke word-highlight sweep, Ken Burns pans), a NOW/NEXT/segmented-timeline
   chord bar, a Key/BPM badge, and a chord fingering legend
   (`lyricvideo/chord_shapes.py` + `chord_diagram.py`) over the audio into the
   final mp4 (`work_dir/<slugified-title>.mp4`). The legend shows one small
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
   cached file, so the countdown fell through to a plain color. Confirmed
   intermittent, not universal, against the owner's own real batch (one
   song's countdown was blank, another in the same batch was fine) --
   consistent with a missing-file gap on specific songs, not every song.
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
   changes flipped the background too often. Every block boundary is still a
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
   `build_scene()`'s
   `scroll_progress` (how far the current line's own on-screen scroll
   animation has advanced) uses `_plausible_line_end()` -- the same
   outlier-capped end as `_plausible_sung_intervals()` -- instead of the
   line's raw `end_time`, for the identical reason: real bug found live,
   2026-09-10, a repeated one-word line ("Memoria") got a 6.86-second
   duration in forced alignment for what's normally close to 1 second,
   which by itself didn't displace `_in_a_line()`/Ken Burns (that line's
   own start/end weren't wildly wrong the way 2026-09-09's was) but did
   distort how fast that one line's own scroll animation should move.
   Deliberately scoped to just this one mechanism at the time -- word-highlight
   timing (`word_sung`/`word_active`) keys only on a word's own start time,
   never a duration, and is still genuinely unaffected by any of this. But
   `build_scene()`'s CURRENT-LINE TEXT is now also gated on `_in_a_line()`
   (real bug found live, 2026-09-10, "Wish You Were Here"): a line whose
   words were themselves scattered with huge gaps (real spoken radio-intro
   dialogue, not sung to a rhythm) left `find_current_line_index` treating it
   as "current" from 9.5s to 94.4s -- 85 seconds -- since that function keys
   only on start_time and nothing else started in between. The line's own
   text now blanks during any stretch `_in_a_line()` says isn't plausibly
   part of it, even mid-line between that same line's own scattered
   plausible-speech islands, while the upcoming-line preview is unaffected.
   `find_current_line_index` ITSELF is still untouched -- this is a display
   gate layered on top of its result, not a change to which index it returns.
   The scrolling timeline lane's per-segment chord label (`render.py`'s
   `_lane_label_font`) shrinks to fit a short-duration chord's narrow box
   instead of being skipped entirely when it doesn't fit at the default size
   (real owner-reported issue, 2026-09-09) -- floored at 18pt. If even that
   doesn't fit, the label is now omitted entirely (`_lane_label_visible`) --
   the colored block itself still draws, so a chord change stays visible,
   but the text no longer overflows into the neighboring segment's own label
   (real bug found live, 2026-09-10: a spurious 0.51-second detected chord
   produced a sliver too narrow for any label, smearing it into the next
   segment's text). No song title or artist text is drawn into the frame
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
`load_redo_inputs()` (reads the original `audio_path`/`title` back off the song's
own `lyrics_timed.json`, so the owner never re-browses for the original files). In
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
packs the banner above) must be a widget managed by `.pack()` directly under
`self.root` -- real bug found live 2026-09-09 (never once showed a real
available update across multiple relaunches): the CustomTkinter rebuild left
`top_frame` pointing at `left`, which is `.grid()`-managed inside the `body`
frame, so every attempt raised `TclError: window isn't packed`, silently (a
background-thread Tkinter callback exception prints to a log the desktop-
launched app's owner never sees). Fixed to anchor on `body` itself, which
really is pack()-managed under `root` like the banner. `gui.py` checks once on
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
The Apply Update confirmation itself (`_on_apply_update_clicked`'s own
`messagebox.askyesno`) is a SEPARATE dialog from the outer "Update
available" one and needs the identical `parent=`/topmost treatment for
the same reason -- real recurrence, 2026-09-10 (owner screenshots): with
no `parent=` given it wasn't WM-recognized as that dialog's child and
could open behind it. Fixed by passing `parent=self._update_dialog_window`
and briefly forcing that dialog topmost around the call.
Cut a release with `scripts/cut_release.sh <version-tag> <notes-file>` (the
releases repo itself was created 2026-09-08, public/unlisted, no source
code — just synced snapshots + release notes). The sync step exports from
git's committed `HEAD` (`git show HEAD:<path>`, never a raw working-tree
`cp`) specifically so uncommitted local changes can never leak into a
public release — see the 2026-09-08 history entry for the real incident
that found this the hard way. Since `git show ... > file` is a shell
redirect, it never carries over git's own tracked executable-bit metadata
(always the destination's default umask instead) — after writing each
file, the script now checks `git ls-tree HEAD` for that path and
`chmod +x`s it if git tracks it as `100755` (real incident, 2026-09-10:
`run_playalongvideoproduction.sh` shipped non-executable in every release
this session, silently re-breaking the desktop launcher on every Apply
Update even after being fixed locally). The owner runs the app directly from this same
git checkout (not a separate deployed copy), so code changes reach them
immediately on every commit; releases exist so the Update Available banner
and changelog stay meaningful, not because Apply Update is the only way
changes reach this install. `v1.1.0` (2026-09-09) is the first release cut
since the project rename — it had drifted to reference the pre-rename
`run_lyricvideogen.sh` (file no longer exists), fixed to
`run_playalongvideoproduction.sh`.

`Settings.render_kwargs()` centralizes resolution/color unpacking for
`assemble_video()`; `run_pipeline()` builds on it. `lyricvideo/settings_preview.py`
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
preview needs it too).

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
write a title/description/tags, and one more per new comment
(`draft_comment_reply`) to draft a reply and flag whether it looks like an
error report -- both parse a labeled-line reply format
(`TITLE:`/`DESCRIPTION:`/`TAGS:` or `IS_ERROR_REPORT:`/`REPLY:`) that's
robust to Claude reordering the lines. `generate_video_metadata()` takes the
real artist `identify.py` already resolved (read from `work_dir/
song_info.json` by `schedule_upload()`, failing soft to `""` on a missing/
corrupt file) and states it as a known fact in the prompt, asking Claude to
work it into the title -- never left to guesswork, and never fabricated
when identify.py itself couldn't resolve one. `lyricvideo/youtube_schedule.py`'s
`schedule_upload()` is the single upload code path (auto AND manual): for
a Public target it uploads immediately as YouTube-Private with a computed
future `publishAt` (`compute_next_publish_slot()` spaces each new slot
`youtube_min_days_between_uploads` days past whichever slot was reserved
LAST, snapped to `youtube_preferred_upload_hour` -- not past `now`, so a
batch of several videos scheduled back-to-back still lands one every N
days in order); Unlisted/Private upload immediately with that literal
status, no scheduling at all. `lyricvideo/youtube_auth.py` owns the OAuth
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
logged as a warning, never raised. A "YouTube: not connected"/"YouTube:
connected as <channel>" status label + "Connect to YouTube" button sit
above the Settings panel; a manual "Upload to YouTube" button next to the
Status line (enabled once a video finishes and YouTube is connected)
always performs a fresh `schedule_upload()` immediately, bypassing the
auto-upload skip-checks -- the owner's deliberate override for a
correction or any other manual re-post. `_update_upload_button_state(work_dir)`
swaps that button for a plain "✓ Uploaded to YouTube" label in the same
spot whenever a saved `youtube_state.json` record's video_id still
verifiably exists on YouTube (same `video_exists()` check as
`_maybe_upload_to_youtube`, same fail-closed behavior on a verification
error -- keep showing "uploaded" rather than flash a possibly-wrong
button) -- real live feedback found the original "just check the local
file" version necessary in the first place: an enabled button right after
an auto-upload already succeeded looked exactly like a pending action
(the owner assumed auto-upload had silently failed), and clicking it
again would have created a duplicate video. `lyricvideo/youtube_comment_state.py`
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
video's failure must never block the rest. That same 20-minute tick
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

This whole feature (11 tasks) is now complete and tested (363 tests
passing). What's NOT yet verified: the interactive OAuth `connect()` flow
and live comment fetch/reply, both of which need the owner's own real
Google Cloud `client_secret_*.json` and a real connected channel to
exercise end-to-end.

## Tests

```bash
cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/ -v
```
