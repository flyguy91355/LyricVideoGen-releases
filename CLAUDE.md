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
  clears the form/log/progress bar back to blank without relaunching the app. A
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
  Redo/log console/generation progress bar, right = the scrollable Settings panel
  (`lyricvideo/settings_panel.py`) bound to a `Settings` object
  (`lyricvideo/settings.py`, persisted to `~/.playalongvideoproduction/settings.json`,
  loaded on launch and saved on every control change). `render.py`/`detect_chords.py`/
  `assemble_video()` all take plain keyword arguments for every Settings-backed value
  (colors as RGB tuples, sizes as int, toggles as bool) and default to the program's
  original hardcoded values — they do not import `settings.py`; `run_pipeline()` is
  the sole integration point that accepts a real `Settings` object and unpacks it.
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
   own panel always renders near-opaque (fixed, NOT the translucent panel_alpha
   used elsewhere) so it stays legible against any background. Long lyric lines
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
   image-follow and Ken Burns pacing. `build_scene()`'s Ken Burns window
   during a real instrumental stretch also now paces to the active chord's
   own duration (when chord data is available) instead of the current-line-
   to-next-line span, so each chord-driven image gets its own natural pan
   instead of inheriting a stretched-out one. The scrolling timeline lane's
   per-segment chord label (`render.py`'s `_lane_label_font`) shrinks to fit a
   short-duration chord's narrow box instead of being skipped entirely when it
   doesn't fit at the default size (real owner-reported issue, 2026-09-09) --
   floored at 18pt; if even that doesn't fit, the label is still drawn and
   allowed to overflow into the next segment rather than disappear. No song
   title or artist text is drawn into the frame
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
main window widened to 1400x820 to fit it. `pipeline.py`'s font-resolution helper
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
robust to Claude reordering the lines. `lyricvideo/youtube_schedule.py`'s
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
per-item inside `_run_batch_worker`. Any upload failure is caught and
logged as a warning, never raised. A "YouTube: not connected"/"YouTube:
connected as <channel>" status label + "Connect to YouTube" button sit
above the Settings panel; a manual "Upload to YouTube" button next to the
Status line (enabled once a video finishes and YouTube is connected)
always performs a fresh `schedule_upload()` immediately, bypassing the
auto-upload skip-checks -- the owner's deliberate override for a
correction or any other manual re-post. `_update_upload_button_state(work_dir)`
swaps that button for a plain "✓ Uploaded to YouTube" label in the same
spot whenever `load_youtube_state(work_dir)` shows the song already has a
real upload on record (whether from auto-upload or a prior manual click)
-- real live feedback found this necessary: an enabled button right after
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
flags likely error reports. That same 20-minute tick
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
No automated "corrected video" re-upload/relinking mechanism exists
anywhere in this feature -- a correction is always the owner's own manual
call via the upload button above.

This whole feature (11 tasks) is now complete and tested (363 tests
passing). What's NOT yet verified: the interactive OAuth `connect()` flow
and live comment fetch/reply, both of which need the owner's own real
Google Cloud `client_secret_*.json` and a real connected channel to
exercise end-to-end.

## Tests

```bash
cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/ -v
```
