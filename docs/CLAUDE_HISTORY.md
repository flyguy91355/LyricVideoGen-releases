# CLAUDE_HISTORY

Dated narrative history for PlayAlongVideoProduction (named LyricVideoGen before the
2026-09-09 rename — see that entry below): incidents, root-cause investigations, and
notable design decisions. `CLAUDE.md` holds only current, load-bearing facts — when
documenting a fix or incident, write the dated entry here, then touch CLAUDE.md only if
a rule or current behavior actually changed. Pattern mirrored from the AITrading
project's history split (same owner, same discipline).

## 2026-09-06 — Project built (v1: tab-PDF-driven path)

Owner wanted a program to produce YouTube-style "play along" videos — scrolling lyrics
with guitar chord names synced in time over the song — with an added twist: an
AI-generated background that changes per lyric line to follow the song's meaning
instead of a plain static background. Full design in
`docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md`; build plan (all
steps completed same day) in
`docs/superpowers/plans/2026-09-06-tab-pdf-video-generator.md`.

Built via TDD, one pipeline stage per commit: project scaffolding and shared data model,
tab PDF chord/lyric parser, Demucs vocal-stem separation, forced-alignment word timing,
alignment-combination/sanity checks, AI background image generation with per-line
caching, pure scene-layout functions, Pillow frame rendering (Ken Burns pans + word-
synced chord labels), moviepy video assembly with audio mux, and the staged/resumable
pipeline orchestration + CLI entrypoint. 30 unit tests passing at completion.

Notable fixes during the build:
- Forced Demucs to run on CPU explicitly — this machine's GT 1030 GPU can't run the
  CUDA kernels Demucs wants by default (matches the project-wide "no GPU reliance" rule
  already tracked for AITrading).
- Alignment normalization: had to strip hyphens (they collide with the forced-alignment
  model's CTC blank-token index) and normalize words to the MMS_FA model's
  lowercase-only alphabet before tokenizing.
- Fixed a `vocals_path` resume bug and replaced a removed `torchaudio.info()` call with
  `torchaudio.load()` (API changed out from under the original implementation).
- `moviepy>=1.0.3,<2.0` and `decorator<5.0,>=4.0.2` are pinned together deliberately:
  moviepy 1.0.3's old-style decorators silently break under `decorator>=5.0` (fps
  resolution returns `None` instead of the passed value) even though librosa declares a
  `decorator>=5.2.1` floor — librosa's actual functions used here work fine against
  4.4.2 in practice. Don't bump either without re-verifying real rendered output.

## 2026-09-06/07 — Vision-fallback chord mapping and content-filter workarounds

Scanned/image-only tab PDFs (no pdfplumber text layer) need a different path than the
deterministic text parser. Added a Claude-vision fallback (`vision_parse.py`) that maps
`[word_index, chord]` position pairs onto owner-supplied plain lyric text — deliberately
designed so **Claude never generates or alters lyric text itself**, only positions
chords against text the owner already provided, to avoid Claude reproducing copyrighted
lyrics.

In practice, some songs' most iconic lines hit Anthropic's content-filtering policy on
this vision call — confirmed real and non-deterministic (identical requests failed then
succeeded on retry). Two mitigations landed:
- Batched vision chord-mapping into small line groups instead of one full-song request
  (a full 50-line song's verbatim lyrics as input tripped the filter far more than small
  batches); default batch size lowered to 10 after empirical testing against real songs.
- Raised the content-filter retry budget to 6 attempts after "Turn the Page" showed a
  higher failure rate than "Wish You Were Here" in testing.
- Added a fully deterministic bypass: `plaintext_chords.py` + `--chords-text-file`, an
  owner-typed chord-over-lyric plain text file (standard tab-site format) that skips
  Claude/vision entirely for songs the content filter blocks outright.

## 2026-09-08 — CLAUDE.md and this history file created retroactively

Owner asked for a desktop launcher icon and, in the course of building it, discovered
the project had no `CLAUDE.md` at all — despite having asked in an earlier session not
to forget CLAUDE.md/CLAUDE_HISTORY.md. That earlier request could not be traced from
this machine: there is no `~/.claude/projects/` transcript here referencing
LyricVideoGen at all (the owner works across two machines; it likely happened elsewhere,
or in a different tool). LyricVideoGen also had no git hook enforcing the CLAUDE.md
discipline the way AITrading does, so nothing would have mechanically caught the gap
either way.

Fixed same day: `CLAUDE.md` written from the actual code/spec/plan; this history file
backfilled from git log; AITrading's `.githooks/pre-commit` (blocks code commits
without CLAUDE.md staged, and large CLAUDE.md additions without this file staged) ported
verbatim and wired via `git config core.hooksPath .githooks`. Also added
`run_lyricvideogen.sh` and a `~/Desktop/lyricvideogen.desktop` launcher icon (pattern
matched to the existing GuitarTrainer/FretRunner desktop icons).

## 2026-09-08 — Instrumental-section chord timing was badly wrong; root-caused and fixed

Owner report: lyric-synced chords and images were both fine, but chord timing during
instrumental sections (intros/breaks/outros — music with no vocals) was "way off" in
every song that had one.

Confirmed with real data before touching any code: inspected `lyrics_timed.json` from
three already-generated songs. "Wish You Were Here"'s famous long outro
(`before_line_index` past the last line, 20 chords over a real ~111-second span) had
collapsed to one **101-second** span for the first chord (`Em7`), with the other 19
chords crammed into the remaining **9.4 seconds** — several of them sub-0.1-second
slivers, physically unwatchable. Its ~20-second intro showed the same pattern (one
44-second `A7sus4` span next to a 0.02-second one).

Root cause, in `lyricvideo/instrumental_chords.py`'s `detect_chord_change_times()`:
boundary times were chosen by taking the globally highest-`needed_boundaries` frames of
frame-to-frame chroma novelty (`np.argsort(novelty)[-needed_boundaries:]`), with no
constraint that they be spread out. Reproduced deterministically with a synthetic WAV
(four distinct sustained two-note "chords" back to back, no fades, no injected noise
needed) requesting 4 chords: any abrupt join between two sustained tones smears across
*several consecutive* CQT frames (window overlap, not several real events), so most or
all of the boundary slots got filled by that one transition's neighboring frames,
leaving genuinely separate real chord changes elsewhere in the clip with no boundary at
all — exactly the collapsed-span pattern seen in the real songs.

Fix: replaced the naive top-K selection with a greedy pick (highest novelty first)
enforcing a minimum time gap — half the block's average expected chord duration —
between any two accepted boundaries (`_select_boundaries_with_min_spacing`), falling
back to even spacing (the function's existing honest-default philosophy) if too few
candidates survive the constraint. Verified against a regression test at real-world
scale (20 chords over ~111 seconds of synthetic audio, matching the real outro) and
against a small 4-chord case; both landed within 0.5s of the true joins.

That first fix surfaced a second, smaller-scale but still real bug at the same 20-chord
test scale: analyzing only the hard-cropped `[start_time, end_time)` slice makes the
very first/last analyzed frame's novelty (silence-padding edge vs. real content) look
like a genuine change, even though it's a window artifact of the slice's own boundary,
not a real *internal* one. Reproducible in 4 of 5 random-seed trials at 20-chord scale:
one real internal boundary got dropped in favor of a spurious one within ~0.03s of an
edge, doubling one chord's displayed duration to ~11s. Fixed by excluding any candidate
within the same minimum-spacing margin of the gap's own `start_time`/`end_time` from
being selectable at all. Re-verified across 8 random seeds at 20-chord scale after this
second fix: every span landed within ±0.1s of the true ~5.55s, no slivers, no doubles.

Both fixes are covered by new tests in `tests/test_instrumental_chords.py`
(`test_detect_chord_change_times_spreads_boundaries_across_all_real_changes`,
`test_detect_chord_change_times_does_not_plant_a_spurious_boundary_at_the_edges`); full
suite (107 tests) passes. Not yet re-verified against a freshly re-rendered real song —
the owner plans to generate a new one next, which will be the live confirmation.

## 2026-09-08 — Update Available feature: GUI side wired up

Continuing the same-day work started with the spec/plan
(`docs/superpowers/specs/2026-09-08-update-available-design.md`,
`docs/superpowers/plans/2026-09-08-update-available-feature.md`): `VERSION` file,
`lyricvideo/update/` package (`version.py`, `release_client.py` — httpx-based since
`requests` isn't a project dependency, `apply.py`), and the `gui.py` wiring (version in
the window title, launch-time-only background check, clickable banner, an Apply Update
dialog that downloads/reinstalls-dependencies-if-changed/copies files/writes VERSION,
and a Relaunch Now button) are all built and unit-tested (173 tests passing).

Two real gotchas hit during implementation, both now handled: (1) `httpx.get()` does
not follow redirects by default (unlike `requests`), and the release archive download
URL 302s to `codeload.github.com` — the apply worker passes `follow_redirects=True`
explicitly, or the "download" would silently succeed with an HTML redirect page instead
of the tarball. (2) `gui.py` had pre-existing uncommitted work in progress (the
carriage-return-aware log widget) at the time this was built; each commit's `git add`
was done via a git-plumbing reconstruction (`hash-object` + `update-index --cacheinfo`)
rather than a plain `git add lyricvideo/gui.py`, so only this feature's own hunks landed
in these commits and the owner's separate WIP stayed uncommitted and untouched.

Not yet done: the `flyguy91355/LyricVideoGen-releases` repo doesn't exist yet, so the
launch-time check currently always fails silently (by design) and no banner has ever
actually been shown. `scripts/cut_release.sh`, the repo creation, a real release cut,
and the owner's own visual confirmation of the banner/dialog/apply/relaunch flow are the
remaining plan tasks.

## 2026-09-08 — First real Apply Update run found a release-cutting bug (fixed)

Repo and script created; `flyguy91355/LyricVideoGen-releases` v1.0.1 cut; owner ran
Apply Update live and confirmed the flow end to end (VERSION file moved to v1.0.1). Owner
asked to verify the update "really did" happen, since the visible effect was easy to
miss (see the dialog-covering-window UX issue tracked separately).

Verification surfaced a real bug: `scripts/cut_release.sh`'s sync step used `cp "$f"
"$CLONE_DIR/$f"` after `git ls-files` -- `git ls-files` only lists tracked PATH NAMES,
regardless of whether the working copy matches HEAD, so `cp` copied whatever was
actually sitting in the working tree. At the moment v1.0.1 was first cut, unrelated
uncommitted work-in-progress (the carriage-return-aware log widget, plus WIP in
pdf_parse.py/pipeline.py/render.py/requirements.txt/tests/test_gui.py) was sitting in
the tree, and all of it got swept into the public v1.0.1 release. Applying that release
back onto the same dirty tree was consequently a no-op for those files -- which is
exactly why the owner didn't see the expected file changes, even though Apply Update's
own mechanics (download, extract, copy, VERSION bump) all genuinely worked. The owner's
actual uncommitted WIP itself was never at risk -- it lives in the working tree, and
Apply Update only ever overwrote it with a copy of itself.

Fixed by replacing `cp "$f" ...` with `git show "HEAD:$f" > ...`, so the sync always
exports git's clean committed content regardless of working-tree state. Verified: cut a
throwaway `v1.0.2-test` tag with the fixed script and confirmed the synced repo no
longer contained the WIP marker and was byte-identical to `git show HEAD:...`; deleted
that test tag, then deleted and recreated `v1.0.1` itself so the public release now
matches clean HEAD. Note for later: AITrading's own `cut_release.sh` (the template this
was ported from) has the same `cp`-after-`git-ls-files` pattern and is exposed to the
identical latent bug if it's ever run with uncommitted changes in its own tree -- not
fixed here (out of scope for this project), just flagged.

Separately, the owner also reported the update dialog itself could open behind the main
window with no visible cue, which is the real reason the Relaunch Now button went
unnoticed. Fixed: `_open_update_dialog` now centers the Toplevel over the main window
and calls `transient()` + `grab_set()` + `lift()` + `focus_force()` so it can no longer
get lost behind it.

## 2026-09-08 — Redo an Existing Song feature

Owner wanted to re-run "Angie" through the pipeline to pick up the same day's
instrumental-chord timing fix, without re-parsing the tab or re-running Demucs, plus a
choice to reuse or regenerate images, plus a backup of what gets replaced. Classified as
a bounded change (brainstorming skill) since it composes pipeline.py's existing staged/
resumable design and gui.py's existing worker-thread pattern rather than introducing a
new subsystem.

Investigated the real on-disk state before designing: Angie's Demucs stems
(`vocals.wav`/`no_vocals.wav`), `parsed_tab.json`, and all 25 cached per-line images
still existed, and `get_or_generate_image()`'s cache-by-content-hash check
(`lyricvideo/imagery.py`) already means re-running the images stage with an unmodified
`images/` dir is a free, instant no-op — the "reuse existing images" default needs no
special handling at all. The "generate new" case does: the images stage also
auto-searches any `images_backup_*/`-named directory for a reusable cached image
(`extra_cache_dirs`), so forcing a genuinely fresh generation requires moving the old
`images/` dir aside under a *different* name (`images_prior_<timestamp>/`) that the
cache search won't match.

Also found `run_pipeline()`'s tab_pdf/chords_text_file validation ran unconditionally
even when `start_stage` would skip the parse stage entirely (the only stage that ever
uses either argument) — a real friction point for this exact feature, fixed by gating
the check on whether parse will actually run.

Shipped: `list_redoable_songs()`, `load_redo_inputs()` (reads the original audio path
and title back off the song's own `lyrics_timed.json`, so no re-browsing for files),
`backup_song_outputs()` (always copies the current video + timing JSON into
`redo_backup_<timestamp>/` before anything is touched), and
`prepare_images_for_fresh_regeneration()`, plus a "Redo an Existing Song" section in the
GUI (dropdown + "Generate new images" checkbox, default off + Redo button, sharing the
existing Generate button's worker-thread/queue/log machinery and mutual-exclusion
guard). Verified against Angie's real files: title "Angie Rolling Stones" slugifies to
"angie-rolling-stones", matching the real video filename; the stored `audio_path` still
exists on disk with a stem matching the existing Demucs output folder exactly.

## 2026-09-09 — OCR fallback groundwork, log widget \r handling, word-highlight refinements

Committed a batch of in-progress work that had been sitting uncommitted in the working
tree (verified against the real diff before committing, not assumed): 183 tests pass
with it applied.

- **OCR fallback groundwork for scanned tab PDFs.** `pdf_parse.py`'s line-clustering and
  chord-to-word pairing logic was extracted into a shared `_assemble_from_words()`
  helper that takes generic word dicts and a caller-supplied y-tolerance, so it works
  against both pdfplumber's PDF-point coordinates and pytesseract's pixel coordinates.
  `ocr_parse.py` (new, unit-tested against a real rendered page and a blank-page
  failure case) uses it to extract chord/lyric pairs via OCR instead of Claude vision.
  The intent is to give scanned/image-only tabs a path that never calls Claude at all
  (avoiding the vision-based content-filtering issue CLAUDE.md already documents), but
  **this is not wired into `run_pipeline`'s fallback cascade yet** — a scanned PDF today
  still only reaches `vision_parse.py`. Wiring OCR in as a first attempt before vision
  (or as the sole path) is follow-up work, not done here.
- **Log widget \r/\n handling** (`gui.py`): `_split_log_text()` gives the log console
  real terminal-style semantics — `\n` commits a line permanently, `\r` (what
  tqdm-style progress output sends on every update) discards the current pending line
  and starts it over, instead of the log filling up with one line per progress tick.
- **Word-highlight/chord-flash color refinements** (`render.py`): split the old single
  `CURRENT_LINE_COLOR` into `CURRENT_LINE_UNSUNG_COLOR` (white, not-yet-sung text) and
  `WORD_HIGHLIGHT_BG_COLOR`/`WORD_HIGHLIGHT_TEXT_COLOR` (the green box behind
  already-sung words), and gave `CHORD_ACTIVE_COLOR` a genuine flash: `_lerp_color`
  fades a chord from `CHORD_FLASH_COLOR` (white, peak brightness on the hit) down to
  the steady gold over `CHORD_FLASH_DURATION_SECONDS`, rather than snapping straight to
  the steady color. A black text-stroke outline (`TEXT_STROKE_COLOR`/
  `TEXT_STROKE_WIDTH`) was added so text stays readable over any Ken-Burns background.
- Output filenames are now slugified from the song title (`slugify()` in both
  `pipeline.py` and `gui.py`) instead of a fixed `final.mp4` — each song gets its own
  distinctly-named file in its work dir.
- `requirements.txt` gained `pytesseract>=0.3` for the new OCR path; `VERSION` bumped
  to v1.0.1 (not yet released — no corresponding `cut_release.sh` run).

## 2026-09-09 — Project renamed LyricVideoGen → PlayAlongVideoProduction

Owner is merging in LyricChord's (a sibling MIT-licensed project, `/home/doug/Lyric+Chord`)
audio-based chord detection and MP3-only lyric-fetching approach (design in
`docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md`) and wants that
merged result to be this project's own forward version progression — not a
permanently-separate fork — so the project itself was renamed rather than kept as
"LyricVideoGen" with a bolted-on feature.

An earlier attempt this same day forked LyricVideoGen into a brand-new, separate
`/home/doug/PlayAlongVideoProduction` directory (detached git history, no shared
future) before the owner clarified they wanted an in-place rename/evolution instead,
continuing this repo's own git history, `VERSION`, and Update Available mechanics.
That fork was deleted; this rename supersedes it.

Sequence: pushed this repo's 15 then-unpushed commits to GitHub first (`origin` was
14 commits behind `main`, plus one new commit for in-progress OCR/render work found
sitting uncommitted in the working tree — see the entry above) so a complete, current
copy of "the existing program" is preserved on GitHub under its original name,
`flyguy91355/LyricVideoGen`, before anything changed. Then: renamed the local
directory, `run_lyricvideogen.sh` → `run_playalongvideoproduction.sh`, the GUI window
title, and CLAUDE.md/CLAUDE_HISTORY.md's own identity references; created a new empty
GitHub repo `flyguy91355/PlayAlongVideoProduction` and re-pointed `origin` at it;
updated the desktop launcher icon to the renamed path.

**Deliberately NOT renamed yet:** the Update Available feature's
`flyguy91355/LyricVideoGen-releases` distribution repo (`release_client.py`,
`cut_release.sh`) — renaming/recreating that releases repo is a separate decision for
whenever a release is actually next cut, not part of this identity rename. Until then,
Update Available still checks the old-named releases repo, which is still valid.
`lyricvideo/` was deliberately kept as the internal Python package name (not renamed
to match) — a large import-path refactor across the whole codebase with no functional
benefit, since it's an internal detail invisible to the owner.

## 2026-09-09 — MP3-only merge: LyricChord's chord detection replaces tab-PDF input

Implemented `docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md` (plan:
`docs/superpowers/plans/2026-09-09-mp3-only-chord-merge.md`). Owner was impressed by
two things in a sibling project, LyricChord (`/home/doug/Lyric+Chord`, MIT licensed):
its chord detection ("spot on") and that it needs nothing but the audio file. Both
turned out to come from the same set of modules, so both got ported wholesale —
`identify.py`/`text_clean.py` (from LyricChord's `metadata.py`), `fetch_lyrics.py`/
`vocal_onset.py` (from `lyrics.py`/`vocal.py`), `detect_chords.py`/`chord_theory.py`
(from `chords/local.py`/`theory.py`), `audio_decode.py` (from `utils/audio.py` +
`utils/ffmpeg.py`) — while this program's own forced-alignment lyric sync, AI
Ken-Burns images, and Redo-an-Existing-Song feature were kept, since the owner
considers this program's lyric/vocal sync meaningfully better than trusting
LyricChord's third-party LRC timestamps directly.

Net effect: `run_pipeline()` now needs nothing but an audio file. The tab-PDF/
chords-text/vision-fallback input path (`pdf_parse.py`, `vision_parse.py`,
`plaintext_chords.py`, and the never-wired-in `ocr_parse.py`/`ocr_clean.py`
groundwork from the 2026-09-09 OCR-fallback commit earlier this same day) is deleted
entirely, along with `instrumental_chords.py`'s chroma-novelty gap-timing (superseded
— full-song `detect_chords` already assigns real chords through instrumental
sections, not just timing boundaries for chords a human already supplied).

Two owner-requested additions beyond the original chord-detection ask, both folded
into the spec before implementation:
- The bottom chord bar (NOW box, NEXT box + countdown, duration-proportional
  scrolling timeline, ported from LyricChord's `_draw_chords`) replaces the old
  above-word chord-flash display entirely — the owner wanted the complete display,
  not a partial port.
- Background images now follow the active chord during instrumental gaps instead of
  freezing on the last-sung line's image (`layout.py`'s `_in_a_line()` + the
  `images` stage's new per-instrumental-chord generation) — the owner noticed the
  freeze specifically while reviewing this same design.

`Song.instrumental_chords: list[InstrumentalChord]` became `Song.chord_track:
ChordTrack`; `ChordWord` (with its now-permanently-`None` `.chord` field) became
`Word`. `requirements.txt` gained `mutagen`/`requests`/`syncedlyrics`, lost
`pdfplumber`/`pytesseract`. The Update Available feature's `LyricVideoGen-releases`
distribution repo name is unaffected by any of this (separate, already-deferred
decision from the 2026-09-09 project rename).

Two real bugs were caught by TDD while porting (both fixed in the test, not the
ported code, since the ported logic matched LyricChord's real behavior in both
cases): `artist_consensus`'s `min_votes` threshold is weighted (a single synced
record counts as 2 votes, not 1), so a test asserting "no agreement" needs a
*plain*-lyrics record to actually fall short of the threshold; and `parse_lrc`'s
`MAX_LINE_HOLD` cap applies even to the last line in the file (a deliberate
LyricChord design choice — no line lingers on screen forever just because the song
has a long tail left), so a lone late-starting line does not simply extend to the
file's full duration.

## 2026-09-09 — Desktop icon silently failed to launch after the rename

Owner reported the desktop icon "will not start the program." Reproduced live by
running `./run_playalongvideoproduction.sh` directly with output captured (rather
than piped straight to a terminal, which had swallowed the traceback on the first
attempt): `ModuleNotFoundError: No module named 'torchaudio'`, even though
`.venv/bin/python -c "import torchaudio"` worked fine and the full pytest suite
(which also runs via `.venv/bin/python`) passed cleanly.

Root cause: the venv was created before the 2026-09-09 rename, at
`/home/doug/LyricVideoGen/.venv`. A Python venv's `bin/activate` script bakes that
creation-time path into `VIRTUAL_ENV=...` and prepends `$VIRTUAL_ENV/bin` onto
`PATH` — it is not relocatable by a plain directory rename/`mv`. The launch script
did `source .venv/bin/activate; exec python -m lyricvideo.gui`, so `activate`
silently exported the stale `/home/doug/LyricVideoGen/.venv` path, `python` on
`PATH` resolved to something outside the real venv entirely, and the import failed
even though every actual venv-invoking command elsewhere (tests, this session's own
verification commands, the Update Available feature's Relaunch Now button) had
always called `.venv/bin/python` directly and so never hit this. The same
class of bug had already surfaced once this session as the broken `pip` shebang
(worked around with `python -m pip` at the time) without generalizing the lesson to
`activate` too.

Fixed by changing `run_playalongvideoproduction.sh` to `exec .venv/bin/python -m
lyricvideo.gui` directly, skipping `activate` entirely — a venv's own python binary
resolves its site-packages relative to its own location, not via environment
variables, so this sidesteps the staleness regardless of what directory the venv
was originally created under. Verified live: process now runs with the real venv
(no traceback) and the actual GUI window (`PlayAlongVideoProduction v1.0.1`)
appears on screen (`wmctrl -l`), not just "the process didn't crash."

**Generalizable lesson:** any time a project's `.venv` predates a directory
rename/move, audit every consumer of it for a `source .../activate` (or a
console-script whose shebang embeds the old absolute path) — direct
`.venv/bin/python`/`.venv/bin/pip` invocation is immune to this whole class of
bug and should be preferred generally, not just as a one-off fix when it's caught.

## 2026-09-09 — Redo crashed on every pre-existing song (two separate bugs)

Owner reported "error when trying redo." Reproduced by loading a real pre-merge
`work/eye-in-the-sky/lyrics_timed.json` through the new `load_song()` directly
rather than guessing, per this project's own verify-against-real-data standard.

**Bug 1 (the actual crash):** `_song_from_dict()` built each `Word` via
`Word(**w)`. Every song saved by the pre-merge pipeline has a `"chord"` key on
each word dict (from the old `ChordWord` model, which `Word` no longer carries),
so `Word(**w)` raised `TypeError: unexpected keyword argument 'chord'` on every
single pre-existing song's file — Redo was broken for 100% of real songs, not an
edge case. Fixed by constructing `Word` from its own named fields
(`word`/`start_time`/`end_time`) instead of splatting the whole legacy dict,
which harmlessly ignores the stray `chord` key (and any other future removed
field) rather than crashing on it. `chord_track` was already tolerant by
accident — `data.get("chord_track") or {}` degrades a missing key (every
pre-merge file has `"instrumental_chords"` instead) to an empty `ChordTrack`
instead of raising, so old chord data is silently dropped rather than migrated,
which is correct: Redo resumes at `"fetch_lyrics"`, well before `detect_chords`
regenerates real chords anyway.

**Bug 2 (would have surfaced next, at render):** two of the four existing
`work/*/lyrics_timed.json` files (`eye-in-the-sky`, `angie-rolling-stones`) had
`audio_path`/`vocal_stem_path`/`instrumental_stem_path` saved as absolute paths
under the pre-rename `/home/doug/LyricVideoGen/...` root, which no longer exists.
The Demucs stem lookup in `run_pipeline` is immune to this (it only ever uses
`Path(audio_path).stem`, a bare filename, never the stored directory), but
`assemble.py`'s final `AudioFileClip(str(audio_path))` needs the real file at
that exact path, so a Redo on either song would have failed at the render stage
after several minutes of real API spend on lyrics/chords/images. (The other two
songs were unaffected: `turn-the-page`'s saved path was already relative and
resolves correctly under the new root since the whole tree moved together;
`wish-you-were-here`'s path points into a different, unrelated project
(`GuitarTrainer`) that was never renamed.) Fixed with a one-time migration
(not new pipeline code — this doesn't recur for future saves) using the
now-fixed `load_song`/`save_song` themselves: load each affected file, rewrite
the `/home/doug/LyricVideoGen` prefix to `/home/doug/PlayAlongVideoProduction`
on all three path fields, save back. Re-saving through the current `Song`
dataclass also transparently upgraded both files off the legacy schema (no more
`"chord"`/`"instrumental_chords"` keys at all). Verified live: all four
redoable songs now resolve to a real, existing audio file
(`Path(audio_path).exists()`).

## 2026-09-09 — Chord legend overlap and lyric-edge overflow, found by watching a real render

Owner watched a real rendered video ("speak-to-me-breathe", a 16-unique-chord
song) and reported two bugs. Both were confirmed directly by extracting real
frames from the actual mp4 with ffmpeg before touching any code.

**Chord legend overlap:** `chord_diagram.py`'s `draw_chord_legend()` had no cap
on rows or overall size — with 16 unique chords it wrapped to 4 rows and grew
tall enough to overlap both the lyric text and the NOW/NEXT/timeline chord bar
below it. Fixed with `_legend_layout()`: computes a box size from the song's
actual chord count that never needs more than 2 rows and never exceeds a
reserved 35%-of-frame-height region, shrinking only as much as necessary (never
below what's needed, never above the owner's own size preference). This
required no per-song foreknowledge or preview — it derives the safe size at
render time from the real chord count. `Settings.chord_legend_size` (percent,
default 100, new slider in the Chord bar section) sets the ceiling this
algorithm scales down from, for the owner's own taste on top of the safety net.

**Chord diagrams hard to read against some backgrounds:** the diagrams' panel
used the same translucent `panel_alpha` as the chord bar's decorative outer
backdrop, so a bright background image could wash out the panel and hurt
legibility. Fixed by giving the diagrams a fixed near-opaque panel fill (235,
matching `render.py`'s existing `BOX_FILL` precedent for "must always be
legible" UI chrome) — no longer tied to the user's translucent `panel_alpha`
setting at all.

**Lyric lines running off both edges:** `render.py`'s `_draw_line_words` drew
every line at a single fixed width with no frame-width check — a real 127-
character line ran off both the left and right edges of the frame. Owner
explicitly rejected shrinking the font as a fix ("that's a bandaid") and
suggested wrapping at commas instead, since a comma is usually a real vocal
pause. Implemented `_split_line_into_rows()`: greedily packs comma-delimited
clauses onto as few rows as fit the frame width, falling back to word-by-word
wrapping only for a comma-free clause that's still too wide on its own, and
never changing the font size. Rows stack centered on the line's original
vertical slot. Verified against the exact real overflowing line from the video.

## 2026-09-10 — YouTube upload + channel management feature shipped

Owner wanted finished videos to optionally auto-upload to their YouTube
channel, with the channel side (title/description, comment replies) managed
by the app too. Full design discussion in
`docs/superpowers/specs/2026-09-10-youtube-upload-design.md`, implementation
plan in `docs/superpowers/plans/2026-09-10-youtube-upload-channel-management.md`
(11 TDD tasks, all complete, 363 tests passing).

**Scheduling redesign mid-brainstorm:** the original design queued finished
videos locally and released one every N days via a periodic `root.after`
timer. Before implementing, checked YouTube's real `videos.insert` docs and
found `status.publishAt` requires `privacyStatus: "private"` at upload time,
with YouTube's own servers auto-publishing at that moment — even immediately
if `publishAt` is already in the past. Replaced the local queue entirely with
`youtube_schedule.py`: upload immediately, reserve a future publish slot
(`compute_next_publish_slot()`, spaced from the last *reserved* slot, not
from `now`, so a batch of several videos still lands one every N days in
order), let YouTube do the actual publishing. Strictly more reboot-proof than
the original design — publishing no longer depends on this app being open at
the right moment at all, only the initial upload does, and that already
happens the instant a video finishes.

**Owner's real growth-strategy question, answered with 2026 research, not
guessed:** owner asked about seeding a brand-new zero-subscriber channel with
a backlog vs. spacing uploads. Real 2026 upload-timing/frequency studies
(SocialPilot, WebFX, Crisp, Alan Spicer) back both halves of the owner's
instinct: don't publish many videos live on the same day (hurts ranking more
than it helps), but bulk-*producing* content and staggering the actual
publish schedule is standard practice. No feature change needed — the
already-live `youtube_min_days_between_uploads` Settings field just gets
turned down (e.g. to 1) during an initial seeding push, then back up once
caught up to real-time production.

**Explicitly dropped from scope, owner's own call:** an automated
"corrected video" re-upload/relinking mechanic (upload a fix, link it from
the old video's description, draft a reply pointing to it). Owner realized
multiple corrections over a song's lifetime would pile up as several videos
on the channel, and didn't want to lose the original's views/comments by
deleting duplicates either — so this was cut entirely. The description
still invites viewers to report errors in the comments (useful signal, still
flagged for the owner via the comment-review panel), but any actual
correction is now a fully manual call using the ordinary "Upload to YouTube"
button, same as any other upload.

Other real decisions baked in: category defaults to "Howto & Style" (id 26);
`made_for_kids` defaults to `False` (a required COPPA declaration, not just
a preference); comment replies are always draft-then-approve, never
auto-posted, matching how this owner already operates the AITrading project
(review before anything automated posts publicly); comment monitoring is
scoped only to videos this app itself uploaded and only runs while the GUI
is open (no always-on service, matching how every other feature here already
works).

## 2026-09-10 — Wrong claim about YouTube categories, corrected by the owner

Told the owner "YouTube doesn't nest categories" when explaining why picking
"Education" in the app's Category dropdown didn't reveal a further "how-to"
option to click. Owner pushed back ("not true.. go look at it") instead of
accepting it. Checked YouTube's real docs and the owner was right: YouTube
Studio's own upload page has a genuine nested subcategory picker under
Education specifically (How-to, Lecture, Tips, Concept overview, etc.) —
this exists, just not through the API. Verified against the actual YouTube
Data API v3 `videos` resource docs (`snippet`/`status` field lists) that this
subcategory is Studio-web-UI-only; no REST API field exposes it at all, so
setting it programmatically would require full browser automation (scripting
a real logged-in browser to click through Studio), not an API call — judged
not worth building for one metadata field given the fragility (breaks on any
Studio redesign) and cost (a real browser session separate from the OAuth
token already in place). Owner chose to set the subcategory manually per
video in Studio instead. Since that subcategory only appears under the
Education top-level category, `Settings.youtube_category_id` default changed
from `"26"` (Howto & Style) to `"27"` (Education) so the option is actually
there for the owner to pick when they go set it by hand.

## 2026-09-10 — First real end-to-end YouTube upload test, and its real bug

Owner completed the full Google Cloud OAuth setup live (project, YouTube
Data API v3 enabled, OAuth consent screen, test user, Desktop app
credential) and ran a real Redo with "Auto-upload finished videos to
YouTube" checked. Owner initially thought it failed ("done but not
uploading.. has a upload to youtube button tho") because the manual
"Upload to YouTube" button was still sitting there active after the run
finished. The real log showed `Uploaded to YouTube: all-the-young-dudes`
-- it had actually succeeded; the auto-upload path only ever logged a
quiet console line, unlike the manual button's own popup confirmation,
so success was easy to mistake for failure. Owner's own diagnosis was
right: an active button in that state is misleading and risks a
duplicate upload if clicked. Fixed with `_update_upload_button_state()`
swapping the button for a plain confirmation label whenever
`load_youtube_state(work_dir)` shows the song already has a real upload
on record. Verified directly against the real `youtube_state.json`
this test run actually wrote (`video_id: uCFrQ2DE76Y`).

## 2026-09-10 — Desktop launcher kept losing its executable bit, root-caused for real this time

Owner reported "error launching application" after closing/reopening the
app; direct reproduction found `run_playalongvideoproduction.sh` at mode
644 again -- the exact same symptom fixed earlier this session (chmod +x,
confirmed matching git's own tracked 755). This time, instead of just
re-applying the same local chmod and moving on, checked WHY it kept
recurring: cloned the actual public releases repo
(`flyguy91355/LyricVideoGen-releases`) fresh and found the file shipped
there at mode 664 -- non-executable -- even though this source repo
correctly tracks it as 755. Root cause: `cut_release.sh`'s
`git show "HEAD:$f" > "$CLONE_DIR/$f"` is a shell redirect, and a shell
redirect always creates its destination file under the process's default
umask; it has no way to know or carry over git's own tracked executable
bit for that blob. Every single release cut this session (v1.1.0 through
v1.3.3) shipped this file non-executable, which is exactly why "fixing"
it locally never stuck -- the very next Apply Update copied the broken
version right back over it (`copy_updatable_files` uses `shutil.copy2`,
which correctly preserves whatever mode the SOURCE file already has --
the bug was entirely upstream, in what got published, not in how updates
get applied). Fixed by reading each file's tracked mode via
`git ls-tree HEAD -- "$f"` and `chmod +x`ing the copy when it reports
`100755`. Verified for real, not assumed: re-cut v1.3.4 with the fixed
script, then cloned the releases repo fresh and confirmed the file now
ships at 775 (was 664 before the fix).

## 2026-09-10 — Countdown lead-in added, so a musician has time to get ready

Owner's real observation: "these song start as soon as the video starts..
needs a couple seconds after you click on the video to start them. maybe
a count down?" First proposal (big number over the background) got
pushback ("im not sure big number.. dont make it gawdy") -- landed on a
modest centered panel matching the chord bar's own existing NOW/NEXT box
language (same `BOX_FILL`/rounded-rectangle/accent-color style) instead
of inventing a new visual idiom.

Implementation: `render.draw_countdown()` composites the number panel;
`assemble_video()` gained `countdown_seconds` (default 3, `Settings`
slider in Output, 0 disables it). The video's own total duration is now
`song_duration + countdown_seconds`; `make_frame(T)` runs on that OUTER
timeline and derives `song_t = T - countdown_seconds` for everything real
(scene/chord-bar/legend), rendering the frozen first-scene countdown
panel instead whenever `song_t < 0`. Audio is delayed to match via
`CompositeAudioClip([audio_clip.set_start(countdown_seconds)])`, so the
audio and the on-screen content always start at the same instant. All 10
pre-existing `test_assemble.py` tests needed `countdown_seconds=0` added
to their calls (the new default of 3 would have silently redirected their
existing small-`t` `make_frame()` assertions into the new countdown-frame
code path instead of the real content path they were written to test) --
5 new tests cover the countdown behavior itself directly.

## 2026-09-10 — Close-confirmation feature shipped broken; a real gap in how it was "verified"

Owner asked for a confirmation prompt when closing the app while a
Generate/Redo/Batch is running. Implemented `_on_close_window()` and
"verified" it by constructing a real `LyricVideoGUI` and calling
`app._on_close_window()` directly with `_running` set both ways --
all three scenarios (idle, running+decline, running+confirm) passed.
Shipped as v1.4.1.

Owner reported it did nothing: closed a running batch with zero warning.
The bug: the commit added the `_on_close_window()` method itself but
never actually added `root.protocol("WM_DELETE_WINDOW", self._on_close_window)`
in `__init__` -- the binding line simply never made it into the file (root
cause of the omission itself was never pinned down; what matters is the
verification gap that let it ship anyway). Calling the method directly
proved the method's OWN internal logic was correct, but proved nothing
about whether a real click on the X button would ever reach it -- and it
didn't, since nothing had told the window to call it.

Fixed by adding the missing `root.protocol(...)` line, and this time
verified by triggering the ACTUAL registered Tcl callback the window
manager itself would invoke: `root.tk.call(root.protocol("WM_DELETE_WINDOW"))`,
never calling the Python method directly. This is now the standing
pattern for any future `WM_DELETE_WINDOW`-style binding in this app --
proving a handler's own logic is correct is not the same as proving it's
actually wired to the thing that's supposed to call it.

## 2026-09-10 — Countdown reworked: beat-synced 4-count, guaranteed real background

Owner reported "Come As You Are" was extremely out of sync, then narrowed
it to specifically the chord sync, then reported the countdown's own
background was blank ("its in the countdown"), then asked for a 4-count
"in tempo with the song" instead of a flat number of seconds.

Investigated the chord-sync claim directly: extracted a real frame at
video-time 65.5s and confirmed the chord bar's own timing math was
already correct (NOW: Cmaj7, NEXT: Em7 in 1.7s, matching the underlying
chord_track data exactly) -- the countdown's `song_t = T -
countdown_seconds` remapping was NOT the cause. Whether the detected
chords themselves are musically accurate for this song is a separate,
unresolved question (Come As You Are's chorus/flanger guitar tone is a
known hard case for the chroma-based detector).

The blank-background report was real, though: `_first_available_image_key()`
was written to make it structurally impossible to happen again -- if the
real first moment's own image key has no cached file, it falls back to
ANY real image already generated for the song rather than the flat
`fallback_color`, only returning None (flat color) if literally no image
exists at all for that song yet. Confirmed intermittent, not universal,
against the owner's own real batch: "Where the Streets Have No Name"
(same batch) had a real countdown image; this song did not -- consistent
with a missing-cache-file gap on specific songs, not a systemic bug.

Also reworked the countdown from a flat N-seconds duration to N *beats*
at the song's own detected tempo (`beat_duration = 60 / chord_track.bpm`,
falling back to 120 BPM if undetected), matching how a real band's
count-in actually works and directly answering "should count down 4, and
be in tempo with the song." `Settings.countdown_seconds` renamed to
`countdown_beats` (default 4) throughout.

Separately, while investigating the lyric-sync half of the original
complaint, found a real forced-alignment outlier in this song's own data:
the repeated word "Memoria" got assigned durations up to 6.86 seconds in
`lyrics_timed.json` (word timestamps direct from alignment, unrelated to
any of the above) -- the same class of bug as the earlier "Breathe," 105
second outlier (2026-09-09), which was fixed for Ken-Burns/image-follow
purposes only (`_plausible_sung_intervals()`) but never extended to the
actual lyric-highlight display timing itself. Owner is removing this
video from YouTube and will redo it once fixed; the underlying
"cap implausible per-word durations for display too, not just Ken-Burns"
fix has not been scoped or implemented yet.

## 2026-09-10 — Lyric-sync fix: cap the outlier line's own scroll timing, not just Ken Burns

Follow-up to the "Come As You Are" investigation above. Traced exactly
where the 6.86-second "Memoria" outlier could and couldn't actually cause
a visible problem: `find_current_line_index` (which line is "current")
and `word_sung`/`word_active` (karaoke word-highlight) both key only on a
word's own START time, never a duration, so they were never actually
distorted by this bug -- confirmed by reading the code, not assumed.
`build_scene()`'s `scroll_progress`, though, paced the current line's own
on-screen scroll animation across its raw `(start_time, end_time)`, and
for this line that meant animating across a 6.86-second span for an
utterance that plausibly takes closer to 1. Fixed with a new
`_plausible_line_end()` (the end of `_plausible_sung_intervals()`'s last
capped interval) used in place of the raw `end_time`. Verified against
the real data: at 1 second into the real "Memoria" word, scroll_progress
went from ~0.146 (raw 6.86s span) to ~0.332 (correctly paced against the
3-second cap) -- a real, measurable difference on the exact case that
triggered the report. Deliberately left `find_current_line_index` and
word-highlight timing untouched, and left the Ken Burns fallback branch
(a narrow edge case already covered by the 2026-09-09 fix for the cases
that actually matter) alone too, to keep this fix scoped to the one
mechanism actually shown to be broken.

## 2026-09-10 — Removed the per-video "spot an error" invite; channel branding; stale-upload self-heal

Owner had second thoughts about `youtube_metadata.py`'s auto-appended
`"Spot an error in this video? Let me know in the comments!"` line on
every video description -- removed outright (`generate_video_metadata` no
longer appends anything past Claude's own description). The internal
"⚠ possible error report" badge in the comment-reply review panel
(`gui.py`, driven by `draft_comment_reply`'s `IS_ERROR_REPORT`
classification) stays -- that's a private triage aid for the owner, not a
public invitation, and was never what the owner objected to.

Built channel branding (owner wanted "high class," explicitly not
cartoon-style): `branding/generate_channel_banner.py` renders a
2560x1440 PNG via Pillow -- deep indigo/magenta/gold diagonal gradient,
a soft warm spotlight behind the wordmark, thin gold corner brackets and
a rule line, letter-spaced typography, respecting YouTube's centered
1546x423 safe area. Output at `branding/youtube_channel_banner.png`.
Full About-tab description and channel keywords written to
`branding/channel_customization.md` for the owner to paste into YouTube
Studio's Basic info -- nothing in the codebase stores the channel's own
name/description, so this is a one-time reference doc, not something the
app reads.

Separately, root-caused why "Come As You Are" wasn't showing up after
its redo: `work/come-as-you-are/youtube_state.json` still recorded the
OLD (buggy, pre-fix) video's video_id, uploaded before the owner deleted
it directly from YouTube Studio. `_maybe_upload_to_youtube` (auto-upload)
and `_update_upload_button_state` (the GUI label) both only ever checked
"does a local state file exist," never "is that video still real on
YouTube" -- so a manually-deleted video left the song permanently stuck
showing "already uploaded" with the owner having no way to recover short
of hand-editing the JSON file. Added `youtube.video_exists(client,
video_id)` (a `videos().list(part="id", id=...)` call) and wired it into
both call sites: a saved state now only counts as "already uploaded" if
the video still verifiably exists on YouTube; if it's gone, auto-upload
re-uploads for real and the button flips back to clickable. A
verification call that itself fails (network hiccup) fails CLOSED in
`_maybe_upload_to_youtube` (skip, never risk a duplicate) and CLOSED in
`_update_upload_button_state` too (keep showing "uploaded" rather than
flash a possibly-wrong button). Fixed the immediate case by hand
(cleared the stale JSON, re-ran `schedule_upload` directly with a
one-off script using the exact same code path as the manual upload
button) -- new video_id `yyiyDvLizQw`, scheduled to publish
2026-09-16T14:00 ET per the existing once-a-day release spacing.

## 2026-09-10 — Comment-disabled videos were silently killing every future comment check

Real live crash: `_check_youtube_comments_worker` looped over every song
with a `youtube_state.json` and called `list_new_comments()` on each --
completely uncaught. The first video in that loop with comments disabled
(YouTube returns a totally normal `HttpError 403 commentsDisabled`, not a
bug) raised out of the loop and aborted the check for every OTHER video
too, silently, and since the exact same video hits the exact same failure
on every future 20-minute tick, comment checking was effectively dead for
the whole channel from that point on with nothing visible in the GUI to
suggest why. Fixed by wrapping each video's own `list_new_comments()` call
in its own try/except (logged, skipped, loop continues), plus a top-level
try/except around the whole method as a last-resort safety net. One
video's failure must never block checking the rest.

## 2026-09-10 — Settings no longer auto-save; itemized confirm before any write

Owner reported having moved a settings slider by accident, without
noticing, at some point in the past -- and every single slider
drag/color-pick/checkbox toggle was calling `Settings.save()` immediately,
so that one unnoticed bump had already silently become the permanent
default with no undo. First proposal (gate persistence behind an explicit
"Save Settings" button, keep the live preview instant) got real pushback:
"so what if i moved something by mistake and not knowing, and then later
a changed a setting on purpose.. and when i saved, it would also saved
the one i didnt know i moved, correct?" -- correct, and a real gap: a
plain Save button still saves the WHOLE current state, silently including
whatever else drifted. Fixed by making the drift visible, not just gating
when it lands: `SettingsPanel._baseline` holds what's actually on disk;
every change is diffed against it (`_dirty_fields`, comparing
`asdict(baseline)` to `asdict(self.collect())` field-by-field), and any
differing field gets a small "●" marker directly on its own label
(`_refresh_dirty_indicators`) -- visible just scrolling past it, before
Save is ever clicked. Save Settings' confirm dialog then lists every
changed field as `label: old → new` (reusing each slider's own display
`fmt` closure, now also stored per-field in `_field_formatters`, so the
values read the same way they do on the slider itself) -- the owner
reviews the whole list and can back out to fix a field that shouldn't be
there before anything touches disk. Discard changes reloads `_baseline`
with no disk write at all. Reset to Defaults still just repopulates the
live widgets (as before); it now also requires a subsequent Save to
persist, so previewing defaults no longer instantly overwrites the real
config either. `gui.py`'s `_on_settings_changed` still updates the
in-memory `self.settings` and the live preview on every change (so the
current session's Generate/Redo/Batch always uses the latest tweak) but
no longer calls `.save()` -- SettingsPanel owns persistence entirely on
its own now. Verified with a real headless Tk root (this project's
established GUI-verification precedent, not pytest, per
`values_to_settings`' own docstring) exercising the exact reported
scenario: change A goes unnoticed, change B is deliberate, Save's
confirm dialog lists both by name before writing, Discard reverts to the
last real save (not the factory default) without touching disk, and a
change with no Save click never reaches disk at all -- plus a screenshot
of the live dirty markers for a visual check. Mid-implementation, EVERY
edit to `settings_panel.py` silently failed to persist to disk on the
first attempt (each `Edit` call reported success; a fresh `Read`/`grep`
immediately after showed the pre-edit file, unchanged) for reasons never
identified -- caught only by verifying with `grep` after every edit
instead of trusting the tool's own success report, and fixed by simply
redoing the same edits a second time, each one confirmed on disk before
moving to the next.

## 2026-09-10 — "Wish You Were Here" at 1:38: two real bugs, not one

Owner reported "the lyrics go totally messed up" at 1:38 into "Wish You
Were Here" and couldn't use the video. Investigated with real frame
extraction (ffmpeg) plus direct inspection of the song's own
`lyrics_timed.json`, and found two distinct, unrelated bugs both landing
in the same few seconds:

**Bug A (the visible mess at exactly 1:38):** the chord data has a real
0.51-second "Gmaj7" segment (98.17-98.68s) sandwiched between two normal
chords -- almost certainly chord-detection noise, not a real strum. In a
26-second timeline window, that's a sliver too narrow for its label even
at `_lane_label_font`'s 18pt floor, and the existing 2026-09-09 behavior
("let it overflow rather than disappear") smeared "Gmaj7" into the
following segment's own label, garbling both. Added `_lane_label_visible()`
in `render.py`: the segment's colored block still always draws (a chord
change stays visible), but the label itself is now omitted when it can't
actually fit its own box, rather than overflowing into the neighbor.
Verified against the real data (`draw_chord_bar` at the real blip's
timestamp) and by pixel-inspecting the following segment's own label
region in a targeted integration test -- confirmed clean, no smearing.

**Bug B (the bigger one, found while investigating): a lyric line frozen
on screen for 85 real seconds.** The song's real, officially-published
intro lyrics include a snippet of spoken radio dialogue ("Yes and um, I'm
with you, Derek, this star nonsense" -- confirmed genuine by checking the
raw fetched lyrics file, not an OCR/ASR error). Forced alignment gave that
line's own words wildly scattered timestamps (a 28+ second gap between
"I'm" and "with", and the single word "star" spanning 46 real seconds),
and because `find_current_line_index` keys only on each line's
`start_time`, it stayed "current" from 9.56s all the way to the next
line's `start_time` at 94.36s -- a stretch during which real singing had
already started elsewhere. This directly contradicts the 2026-09-10
"Come As You Are" entry above, which stated current-line selection and
word-highlight were "never actually affected by this bug [class]" --
true for that narrower case, but not universally: a sufficiently large
gap between two real lines (common with a long instrumental intro) was
never actually exercised before. Fixed by reusing the ALREADY-EXISTING
`_in_a_line()` plausibility check (previously used only to pace the Ken
Burns image-follow during instrumental gaps) to also blank the CURRENT
line's own displayed text whenever `t` falls outside every one of that
line's plausible speech intervals -- `find_current_line_index` itself is
untouched, this is a display gate layered on its result. The upcoming
(distance_from_current=1) line preview is deliberately left alone, so the
next real line still shows up ahead of time as normal. Verified against
the real song data directly: at song_t=20 (a real silent stretch) the
current line now blanks; at song_t=46 (inside one of this same line's own
scattered real speech islands) it still shows correctly -- confirming the
fix tracks the real islands of speech within the line, not a blunt
all-or-nothing blank of the whole 85-second span.

Both fixes need the owner to Redo "Wish You Were Here" once applied --
the bug is baked into the already-rendered `wish-you-were-here.mp4`,
not something that self-heals on next playback.

## 2026-09-11 — Channel branding finished: profile icon added

Owner asked to continue the 2026-09-10 branding work with "a little round
icon too." Added `branding/generate_channel_icon.py`: an 800x800 PNG using
the exact same jewel-tone gradient/gold-accent language as the banner, with
a "P▶A" monogram (the play-triangle standing in for the "L" in "Play") in a
double gold ring, "PLAY ALONG" beneath. Since YouTube always displays the
channel icon cropped to a circle, every meaningful element is kept inside a
centered inscribed circle -- the four square corners are left as plain
background, since real uploads lose them to the crop. Output at
`branding/youtube_channel_icon.png`; `channel_customization.md` documents it
alongside the banner, including a real platform limitation found while
writing that doc: the YouTube Data API has no endpoint for the channel
profile picture at all (unlike the banner and description, which ARE
API-settable) -- Studio's Customization page is the only way, so this asset
stays a manual-upload reference image like the banner already was.

While in there, fixed a real portability bug in `generate_channel_banner.py`
(present since the 2026-09-10 commit): its output path was a hardcoded
absolute path under a *specific past Claude session's own scratchpad*
directory, so running the script from any other session -- including a
plain `python3 branding/generate_channel_banner.py` exactly as
`channel_customization.md` instructs -- would fail outright, since that
directory doesn't exist outside the session that created it. Fixed to write
to `Path(__file__).parent / "youtube_channel_banner.png"` (and the new
icon script was written the same way from the start). Verified by actually
re-running both generators and confirming the banner's regenerated bytes
are pixel-identical to the previously-committed PNG (the path fix and an
unrelated dead-code cleanup -- a leftover mid-edit double-composite of the
vignette layer -- were both no-ops on the rendered output).

Owner then asked to actually apply the banner + description live rather
than leave it as a copy-paste doc, since the app was already OAuth-connected
(`youtube_token.json` present from the 2026-09-10 feature). Confirmed real
API behavior before writing anything (channelBanners.insert uploads the
image and returns a URL; that URL is then written onto
`brandingSettings.image.bannerExternalUrl` via a SEPARATE `channels.update`
call -- there's no single "set banner" endpoint; `channels.update(part=
"brandingSettings")` replaces the whole part, not just the fields
mentioned, so the current brandingSettings must be fetched and merged into,
never sent as a bare `{description, keywords}` body). Added
`branding/push_channel_branding.py` (not wired into the GUI -- a manual,
re-runnable apply step) and ran it live: confirmed the connected channel
identity first (`Play Along Videos`, matching expectation) before writing
anything, then pushed the banner and the same description/keywords from
`channel_customization.md`. `brandingSettings.channel.keywords` turned out
to need one space-separated string with each multi-word phrase quoted
(`"guitar play along" "play along videos" ...`), not the comma-separated
list the doc and Studio's own UI show -- kept under the real 500-character
cap (274 used). Verified against the API's own update response, not just
"the call didn't raise": description length and the final
`bannerExternalUrl` both echoed back matched what was sent. The profile
icon still needs the owner's own manual Studio upload -- confirmed while
building this that the Data API genuinely has no endpoint for the channel
picture at all, unlike banner/description which this script now handles
directly.

## 2026-09-11 — Image pacing (hold + crossfade), Settings popup redesign, artist in upload titles

Three owner-requested features in one session.

**Image pacing.** Owner complaint: during an instrumental stretch with fast
chord changes, the background image flipped too often to be watchable, and
switches were an instant hard cut. Added `layout.build_image_timeline()`:
walks the whole song once and merges consecutive instrumental chords
shorter than the new `Settings.image_min_hold_seconds` (default 2.0s,
owner-adjustable) forward into one block until the combined span reaches
that minimum -- every block boundary is still lifted from a real chord
onset (never an independent timer), confirmed by the owner's own concern
mid-design ("make sure it still stays in sync with the music after the
hold"). `build_scene()` was rewired to look up this precomputed timeline
instead of re-deriving the active chord itself, and now also reports
`prev_image_key`/`image_blend` so `assemble.py` can crossfade between any
two images (`render.crossfade_backgrounds()`) over the new
`Settings.image_transition_seconds` (default 0.25s, owner explicitly
rejected an initial 0.5s suggestion as "too long") -- capped to at most 40%
of either neighboring segment's own length so a briefly-held image can't
spend its whole visible life mid-fade. A subtle merge-algorithm bug was
caught by its own test during TDD: the first version accumulated duration
across chords without stopping at one that already met the minimum on its
own, silently swallowing the next chord into the same block too.

**Settings popup redesign.** Owner: "i hate the settings in the program...
i want it to be a popup window instead." Moved Settings + its live preview
out of the embedded `CTkTabview` "Settings" tab (the 2026-09-10 fix) into
their own modal popup (`gui._open_settings_window`, same transient/
grab_set/lift/focus_force/brief-topmost treatment as the Update Available
dialog, since a plain Toplevel has a documented history of opening silently
behind the main window on this project). Added a typeable value box next
to every slider (`_parse_clamped_float`, tolerant of a stray "%"/"s"
suffix) and a "default: X" label on every field. Verifying with an actual
offscreen screenshot (Xvfb + ImageMagick `import`, so nothing popped up on
the owner's real desktop) caught two real bugs a code read alone missed:
the new entry box was invisible at first (packing the expand=True slider
before the fixed-width entry claims the whole frame, a Tkinter pack-order
gotcha), and once visible it showed "0"/"off" instead of the real loaded
value, because `CTkSlider`'s own `command` callback -- confirmed by reading
customtkinter's source -- only fires from a live mouse drag, never from a
variable set programmatically (e.g. `load_from()` on open), a latent gap
the OLD read-only label had too, just never surfaced since nobody looked
this closely. Fixed by driving the entry off a trace on the slider's own Tk
variable instead, which covers every path (drag, load, Reset to Defaults,
and the new typed-commit) through one mechanism. The unsaved-change
indicator is now bold in addition to its existing orange color (owner:
highlight it "like you have done in AITrading").

**Artist in upload titles.** Owner: "we should add the artist to the title
of the video as well." Found that `Song` never carried the artist past the
`identify` stage -- `schedule_upload()` passed only `song.title` into
`generate_video_metadata()`, which had no way to know who performed a song
it didn't already recognize by title alone. Rather than have Claude guess,
`schedule_upload()` now reads the real, already-resolved artist back from
`work_dir/song_info.json` (written by `identify()`, sitting right next to
the `lyrics_timed.json` it already reads) and states it as a known fact in
the prompt, asking Claude to work it into the title -- silently omitted,
never fabricated, when identify.py itself couldn't resolve one (fails soft
to `""` on a missing/corrupt song_info.json too, e.g. a very old work dir).

## 2026-09-11 — Monetization support: burned-in overlay + description link

Owner wanted a way to earn from these videos since standard YouTube ad
monetization doesn't fit template-driven content -- researched real
options (Ko-fi, Buy Me a Coffee, YouTube's own Super Thanks/Memberships,
which needs ~500 subscribers this brand-new channel doesn't have yet) and
the owner picked Ko-fi + Stripe payout, walked through account/Stripe
verification live over several turns until Stripe showed "active" and
Ko-fi's own "Action Required" flag cleared after a manual refresh.

Built `render.draw_support_overlay()` (new `Settings.support_overlay_text`/
`support_overlay_size`/`support_overlay_lead_seconds`) plus a matching line
`schedule_upload()` appends to the YouTube description from the same text
field -- one owner-typed string, two surfaces, blank disables both. Design
evolved live across the conversation: first "always visible," then owner
asked for "just the last 15 or 30 seconds" (landed on 20s) instead of the
whole video, explicitly confirmed as the last 20s not the first, and
confirmed it should never show during the countdown either. Owner also
explicitly clarified the burned-in graphic can never be clickable (no
region of a rendered video frame is) -- it's a pointer to the real,
clickable link in the description, not a link itself.

The FIRST implementation placed the overlay upper-left, verified only in
isolation against a flat background. Owner asked to actually see it
composited with real content before trusting it ("make sure its not
covering up anything important") -- rendering it together with the chord
fingering legend (also upper-left, `chord_diagram.py`) immediately showed
a real, direct collision: the overlay text sat directly on top of the
first two chord diagrams' own labels. Moved to upper-right instead, but
the Key/BPM badge (`draw_chord_bar`) already lives there too -- fixed by
right-aligning the overlay and positioning it below the badge's own line,
then confirmed clean with a second composite render (chord bar + legend +
lyrics + overlay together) before calling it done. A real lesson in this
project's own established practice: an isolated-flat-background render is
not sufficient proof for a new visual element; only a composite against
the other real overlays it will actually share the frame with is.

Also set the owner's real Ko-fi link (`ko-fi.com/playalongvideos`) directly
into the live `~/.playalongvideoproduction/settings.json` on request, since
the owner's already-running GUI process (started before this session, so
running old code with no Settings-popup UI to type it into) needed to be
closed first -- confirmed the process had actually exited before writing,
via `Settings.load()`/modify/`.save()` (not a hand-edited JSON string) so
every existing field round-tripped untouched and only the new fields were
added with their real defaults.

## 2026-09-11 — Settings popup was blank on real use (v1.8.0 regression)

Owner applied v1.8.0 and reported the new Settings popup showed the live
preview fine but the entire panel below it -- Output section, every
slider, Save/Discard -- was blank gray. Reproduced directly (not just read
the code) using the owner's own real settings.json: constructing
`SettingsPanel` with `on_change=self._on_settings_changed` wired the same
way `_open_settings_window` does raised `AttributeError:
'...' object has no attribute 'settings_panel'`, confirmed via a script
that mirrors the exact real call. Root cause: `SettingsPanel.__init__`'s
own trailing `load_from()` fires `on_change()` once before
`self.settings_panel = SettingsPanel(...)` in `gui.py` has finished
assigning -- so `_on_settings_changed`'s `self.settings_panel.collect()`
hits an attribute that doesn't exist yet. Tkinter swallows the exception
silently (no console the desktop-launched owner would see), aborting
`SettingsPanel.__init__` before `.pack()` ever ran, leaving the whole
panel never added to the window.

This exact hazard already existed at app startup too (`self.settings_panel
= SettingsPanel(settings_tab, ..., on_change=self._on_settings_changed)`)
but never manifested there, because `self._suppress_settings_save` starts
`True` and doesn't flip to `False` until after that first construction
completes. The popup redesign added a SECOND construction point (whenever
the button is clicked, well after startup's guard already flipped false)
without re-arming that same protection. Fixed by wrapping
`_open_settings_window`'s own `SettingsPanel(...)` construction in
`self._suppress_settings_save = True` / `False`, mirroring startup's own
guard. Verified two ways: the same direct reproduction script now
constructs without raising, and an offscreen screenshot (Xvfb) using the
owner's real settings.json shows every field populated (including their
actual saved Lyric size 41 / Chord size 77), not just "no exception."

## 2026-09-11 — Split support_overlay_text into two fields; a second, much stranger blank-Settings bug

Owner: the overlay text ("Support: This Channel. Link in Description")
was never meant to double as the description's own link -- the on-screen
watermark can't be clickable regardless of wording, but the YouTube
description needs the REAL `https://ko-fi.com/...` URL somewhere in it,
and the shared field left 10 already-uploaded videos' descriptions saying
"link in description" with no actual link anywhere. Split into two fully
independent `Settings` fields -- `support_overlay_text` (on-screen only)
and a new `support_description_text` (description only) -- each
independently blank-disables its own surface. Also built
`youtube.get_video_snippet()`/`update_video_description()` (fetch-modify-
send-back, since `videos.update(part="snippet")` replaces the WHOLE
snippet, the same real gotcha this project already hit with
`channels.update`) and a one-off `scripts/
backfill_support_overlay_description.py` to add the line to every
already-uploaded video's description; ran it live (10 videos updated) once
before the split existed, so those 10 currently carry the pre-split
(unlinked) text and will need a second backfill run now that
`support_description_text` actually holds the real URL.

Separately, the owner reported almost the ENTIRE Settings popup had gone
uneditable -- every slider/dropdown/button gone, only checkboxes left.
Live investigation (screenshotting the owner's REAL window via `wmctrl`/
`xdotool`/`import -window <id>`, not a guess) ruled out CPU load, color
theme resolution (the resolved fg_color was a normal, correct blue),
widget geometry (widgets reported correct 272px width), being inside a
`CTkToplevel` specifically, the `load_from()` mass-variable-update
cascade, and forced resize/redraw timing -- all tested and eliminated one
at a time by reproducing against the owner's own real settings.json.
Root cause, found by bisecting the real `_build()` section by section:
the two new text fields' labels were ~80 characters (`"On-screen overlay
text -- blank = off (never clickable, e.g. 'Support: link
below')"`) -- far longer than any other label in the panel (previous max
~37 chars). Tkinter's `grid` geometry manager computes ONE width per
column, shared across every row using that column -- so this one long
label blew out column 0's width for the WHOLE panel, squeezing columns 1
(the actual control) and 2 (default-value text) to nothing for every
OTHER field too, not just the long-labeled ones. Fixed by shortening both
labels to match every other field's convention (`"Overlay text (blank =
off)"` / `"Description text (blank = off)"`); a warning comment now sits
directly on `_add()` so a future field addition doesn't reintroduce this.
Confirmed fixed by rendering the real, unmodified `SettingsPanel` again
with the owner's actual settings.json -- every field populated correctly,
including their real saved overlay text and a 30s hold duration they'd
already adjusted themselves in the (until-now broken) popup.

## 2026-09-11 — Backfill script still read the pre-split field; cleaned up 10 live descriptions

`scripts/backfill_support_overlay_description.py` was written before the
`support_overlay_text`/`support_description_text` split above and never
updated -- it still read `support_overlay_text`, so re-running it after
the split silently found the (correct) new text absent from its own
check and reported "already had it" for all 10 videos without touching
anything. Fixed to read `support_description_text`. Also hand-cleaned the
10 videos' real descriptions directly (not through the script, since this
was a one-off correction for stale content the script was never designed
to detect): removed a leftover non-`https://`, non-clickable line from an
even earlier attempt, and replaced the placeholder "This Channel. Link in
Description" text with the real `https://ko-fi.com/playalongvideos` link.
Verified by re-fetching two of the ten videos' actual live descriptions
afterward.

## 2026-09-13 — Apply Update silently reverted a newer local commit's docs; added a staleness guard

Found live: the working tree had uncommitted changes removing the backfill-
script writeup above (both here and in `CLAUDE.md`) and bumping `VERSION`
from v1.8.1 to v1.8.2, with no corresponding commit. Root cause: release
`v1.8.2` was cut (2026-09-11 23:27:35 UTC) from commit `4917284`; commit
`920cbb2` (the backfill-script fix and this file's writeup of it) landed
~41 minutes later, but no new release was cut afterward. Clicking Apply
Update then correctly saw v1.8.2 > the locally-recorded v1.8.1 and copied
that release's (older) `CLAUDE.md`/`docs/` content over the local files --
silently discarding `920cbb2`'s documentation changes in the working tree,
since Apply Update only compares release version numbers and has no idea
the local git checkout had already moved past the commit a release was
built from. Recovered by restoring the two docs files from git and keeping
the (legitimate) VERSION bump.

Fixed the underlying gap rather than just the one-off damage:
`scripts/cut_release.sh` now writes a `RELEASE_SOURCE_COMMIT` marker (this
repo's own `git rev-parse HEAD` at cut time, never itself copied into an
install -- it isn't in `apply.py`'s `ALLOWED_PATH_PREFIXES`) into every
release snapshot going forward. `lyricvideo/update/apply.py`'s new
`is_source_commit_already_applied()` checks that marker against the local
checkout's own git history (`git merge-base --is-ancestor`) before
`_apply_update_worker` in `gui.py` copies anything; if the release's
source commit is already an ancestor of local HEAD, no files are touched
-- only `VERSION` is recorded (via the new `apply_up_to_date` queue event
and `_on_apply_update_up_to_date`), so a stale-relative-to-git release
can never silently regress newer local work, but the update banner still
clears normally. Deliberately fails open (returns `False`, i.e. proceeds
with the copy as before) whenever it can't *confirm* ancestry -- no
marker (a release cut before this fix existed), not a git checkout, or an
unresolvable commit -- since refusing to update on an inconclusive check
would be worse than the bug this guards against. Also cut release
`v1.8.3` from the current commit to bring the releases repo back in sync
with local git history.

## 2026-09-13 — YouTube upload scheduling replaced a local counter with the channel's real, live schedule; then made it gap-fill

Owner-reported: a batch upload's next scheduled publish date looked wrong
after the owner had manually published several already-scheduled videos
early directly in YouTube Studio. Investigated live against the real
channel (`flyguy91355`'s connected account): the local
`~/.playalongvideoproduction/youtube_next_slot.json` running counter had
been inflated by 14 days at some point mid-batch (matching this session's
earlier finding that the owner had changed settings mid-run), and every
later upload kept compounding that same gap forward since the counter
only ever remembers "the last slot I personally reserved," with no way to
notice the owner had since published six of those already-scheduled
videos early by hand (freeing up 2026-09-14 through 2026-09-19) or that
one outlier video had landed on 2026-10-03 instead of a normal daily
slot.

Replaced the whole mechanism: `youtube.py`'s new `reserved_publish_dates()`
lists every video ever uploaded to the connected channel (via its uploads
playlist, paginated) and returns the LOCAL calendar date each one already
claims -- a still-scheduled private video's `publishAt`, or an already-
public video's real `publishedAt` -- so the schedule is always read from
YouTube itself, never a local file that can silently drift out of sync
with manual changes made directly in Studio. `youtube_next_slot.json`,
`load_next_slot()`, and `save_next_slot()` are deleted outright, not
deprecated.

Found and fixed a real regression while verifying this against the live
channel: `compute_next_publish_slot()`'s `.replace(hour=preferred_hour)`
had always operated on a value that was implicitly LOCAL time (the old
file always stored local timestamps) -- the new live-API dates are UTC.
Snapping the hour without converting first silently turned every video's
intended 2pm-Eastern slot into 2pm UTC (10am Eastern) the moment the data
source changed. Fixed by converting to local time first; added a
regression test that fails without the fix.

Then went further per an explicit owner follow-up ("can it fill in the
spaces too... fill that slot with the newly uploaded songs" rather than
moving every already-scheduled video's date): rewrote
`compute_next_publish_slot()` from "reserved-slot-plus-interval" (which
only ever pushed a new upload further into the future) to a day-by-day
gap search -- starting from today, it walks forward and picks the first
date at least `youtube_min_days_between_uploads` days from EVERY already-
claimed date (checked in both directions, so a gap is never so narrow
that filling it would land a new video too close to what's already
scheduled on either side). Verified against the real channel: with
2026-09-09 through 2026-09-19 and 2026-10-03 all claimed, the old logic
would have scheduled the next upload for 2026-10-04; the new logic
correctly proposes 2026-09-20, filling the real 14-day gap instead of
extending past the outlier. A batch run naturally still spaces multiple
new uploads apart, since each `schedule_upload()` call re-queries the
channel live and the previous item's own video is now really on it.

## 2026-09-13 — Comment checker was hitting commentsDisabled on every still-scheduled video

Owner-reported, seen live in the log: `_check_youtube_comments_worker`
was warning `could not check comments for <song> (<video_id>): HttpError:
... commentsDisabled` for several songs on every 20-minute tick. Those
were all videos still private/scheduled (a future `publishAt`, not yet
actually public) -- YouTube always refuses a `commentThreads.list` call
against a video that isn't public yet, which is normal, expected
behavior, not a real failure. The per-video try/except already caught it
so it never blocked checking other videos, but it was pure log noise for
something entirely predictable in advance.

Added `youtube.is_video_public()` (one cheap `videos().list(part="status")`
call) and check it before ever calling `list_new_comments()` -- a non-
public video is skipped silently, no warning printed, since this is an
expected, normal state for a song mid-schedule, not an error condition.

## 2026-09-13 — HPCP chord-detection fix reverted: real regression on distorted/riff material

Earlier the same day, `detect_chords.py` was switched from plain CQT chroma
to essentia's HPCP after verifying clear improvement on three real songs
(Bridge Over Troubled Water, Fire and Rain, Hotel California) -- all
acoustic-leaning, cleanly-strummed material. Owner then redid "Sunshine of
Your Love" (Cream) with the new code and reported it still wasn't in sync.
Investigated properly instead of assuming the report was wrong: extracted
the ACTUAL rendered chord track from `lyrics_timed.json` (not a fresh
re-run) and separately ran the exact pre-fix code (`git show
575a651^:lyricvideo/detect_chords.py`) against the identical `no_vocals.wav`
for a direct, controlled comparison:

- **New (HPCP)**: D, E, D#m, D, A, E, D, A, E, D, A, D#m, Dmaj7, D#m,
  Dmaj7, E, D#, Dmaj7, D#m, Dmaj7, E, D#m, D, C#maj7, D, C#m, D, Amaj7,
  Gm7, A, C#m7... -- rapid flicker through harmonically implausible jazz
  chords, nothing like the real song.
- **Old (plain CQT)**: D (15s), G, D (17s), G, D, F#m, G (6s), D (8s), A,
  G, A, G, A (5s), D, G#, D... -- long, stable, musically coherent, matches
  the song's actual well-known simple blues-rock structure.

Root cause: the three validation songs were all clean acoustic/ballad
material; "Sunshine of Your Love" is heavily distorted, riff-driven hard
rock. HPCP's harmonic summation across 8 overtones (exactly the mechanism
that helped the quiet acoustic passages) most likely amplifies distortion-
generated harmonic/intermodulation content into spurious pitch-class
energy, actively hurting detection on overdriven material -- the opposite
of what it did for clean signals. The three-song validation set was not
diverse enough in genre/production style before shipping; this is now a
confirmed limitation, not a guess.

Reverted outright (`git revert` of the HPCP commit, which also reverted
that commit's own history entry documenting the original investigation --
summarized here so the evidence isn't lost: on real songs with a quiet-
intro-to-loud-chorus structure, plain-CQT chroma produces measurably more
chord-label flicker in the quieter first half than the louder second half,
e.g. "Bridge Over Troubled Water" had 71 segments averaging 2.1s in its
first half vs. 42 averaging 3.5s in its second -- confirmed via real RMS
measurements, not assumed) rather than leaving a broken default in place
while a real fix is designed. That original first-half flicker weakness on
quiet passages is real and still unfixed after this revert. Any genre-
adaptive or hybrid approach attempted next needs testing across BOTH clean
acoustic and distorted/riff material before it can be trusted again --
three same-genre songs was not enough evidence to ship a core-pipeline
change, which is exactly how this regression shipped in the first place.

## 2026-09-13 — Chord detection replaced entirely with crema, a real trained model; venv rebuilt on Python 3.11

Follow-up to the reverted HPCP attempt above. Owner asked to research what
real chord-recognition products actually use. Findings: Chordify (a real
commercial product) confirmed via their own engineering blog that they use
a trained deep neural network for chords plus a separate one for beat-
tracking -- not hand-tuned chroma-template matching, which is what this
project had used since its original LyricChord port. Evaluated real
options: `madmom` (unmaintained since ~2018, fails to build on this
Python/numpy version), `essentia`-based `autochord` (inherits AGPL,
modest ~67% published accuracy), `BTC-ISMIR19` (a real transformer model,
MIT licensed, but no pretrained weights shipped in the repo), and `crema`
(Brian McFee, ISC licensed, actively maintained -- last pushed 2024).

`crema` was the only one actually gotten running and tested against real
audio. Getting there took real, methodical debugging, not a quick pip
install: `pkg_resources` (setuptools>=81 removed it -- pinned
`setuptools<81`), Keras 3 vs. the Keras-2-style APIs crema's code uses
(pinned `tensorflow==2.15.0`, which pulls a matching `keras==2.15.0`
automatically -- newer TensorFlow defaults to Keras 3), and
`scikit-learn>=1.6`'s `__sklearn_tags__` API breaking crema's own `pumpp`
dependency's `LabelEncoder` usage (pinned `scikit-learn<1.6`). None of
this old stack has wheels for Python 3.12 -- only up through 3.11.

Tested crema directly against 5 of this project's real songs before ever
touching the pipeline, including the exact song that broke the HPCP
attempt ("Sunshine of Your Love"). Results, compared against real,
independently-known chord progressions:
- **Sunshine of Your Love**: D(24s)-G-D-G-D-G-D(11s)-A-C-G-A-C-G-A... --
  long, stable, correct. The old algorithm and the reverted HPCP attempt
  both produced choppy, implausible output on this exact song.
- **Hotel California**: Bm-F#7-A-E7-G-D-Em-F#7-Bm-F#7-A-E-G-D-Em-F#7... --
  matches the real, famous Eagles progression exactly, including the
  correct dominant F#7 (both prior pipelines got this wrong).
- **Before You Accuse Me**: N-B-E-A-E-A-E-E7-B-A-E7-B7-E7... -- a
  textbook 12-bar blues shuffle, correct for this real blues standard.
- **Come As You Are**: a clean, steady D-Em alternation -- this exact
  song is flagged in this project's own history (2026-09-10) as "a known
  hard case for the chroma-based detector" (its flanger guitar tone);
  crema handled it cleanly.
- **Bridge Over Troubled Water**: more harmonically complex output
  (half-diminished/6th/slash chords) -- plausibly more accurate given the
  song's real, sophisticated piano arrangement, but not independently
  verified by ear.

Owner explicitly declined an on/off fallback toggle ("why would I need a
fallback -- fall back to something that produces a video I don't want?
lets just fully change out the program... I can always revert back to an
older version if I want") -- git history is the safety net, matching how
the HPCP regression was already handled hours earlier. Full replacement,
not a Settings-gated option.

**Architecture decision**: crema's old dependency stack conflicts with
nothing in this project's own requirements once resolved together --
verified with a real combined install (not just pip's dependency
resolver on paper): `torch`, `torchaudio`, `demucs`, `librosa`, and
`tensorflow==2.15.0`/`crema` all installed and ran correctly in the SAME
environment, with pip settling on `numpy==1.26.4` as the one version
that satisfies both torch and TensorFlow's constraints simultaneously.
This ruled out an earlier, more complex plan (a second isolated venv
with a subprocess boundary) in favor of one environment -- the owner's
own suggestion ("can you do the entire program in 3.11 or whatever it
needed?"). The project's `.venv` was rebuilt from scratch on Python 3.11
(previously 3.12) for this reason; `requirements.txt` gained the four
pins above with comments explaining why each exists.

**Implementation** (`lyricvideo/detect_chords.py`, fully rewritten): calls
`crema.analyze.analyze(filename=...)` directly (in-process, no subprocess)
and converts its JAMS chord annotation into this app's own `ChordEvent`
list. crema's 602-class vocabulary (with inversions and extended
tensions) collapses down to this app's deliberately small 5-quality set
(maj/min/7/min7/maj7, matching `chord_shapes.py`'s existing fingering
diagram coverage exactly, so the legend needs no changes) via
`_simplify_chord_label()`: half-diminished sevenths collapse to min7,
diminished/augmented/sus/6th chords collapse to their closest plain
triad, extended 9/11/13ths drop to their base 7th quality, slash-chord
inversions are dropped entirely keeping only the root. crema also
handles segmentation and silence ("N") detection as part of its own
trained output, so the old pipeline's separate beat-tracking-based
segmentation and RMS-relative silence heuristic are both gone entirely.
Key and BPM are still estimated independently via the same librosa code
as before (chroma + Krumhansl-Schmuckler for key, `beat_track` for BPM) --
untouched, since neither was ever implicated in any past incident.

Found one real edge case via the full test suite, not assumed: crema is
trained on real recordings, which always carry SOME noise floor even in
quiet passages -- true digital silence (an exact-zero-amplitude test
tone) is out-of-distribution for it, and it guessed a real chord label
("F:7/b7") at low confidence (0.26) rather than its own "N" class.
Investigated whether crema's own per-observation confidence score could
distinguish this from a real quiet-but-correct passage: no -- a real,
correctly-detected song's own legitimate quiet segments scored as low as
0.30, too close to reliably separate from the silence case's 0.26 by a
threshold. Fixed instead with a narrow ABSOLUTE (not peak-relative) RMS
floor (`_ABSOLUTE_SILENCE_RMS`) that only catches genuinely near-zero
audio, deliberately avoiding the old pipeline's own bug (a peak-relative
threshold that misclassified real quiet passages in loud songs as
silence).

`snap_chords_to_key` (a real, owner-visible Settings checkbox) is kept in
`detect_chords()`'s signature for backward compatibility but no longer
changes chord identity -- it was the old template-matching pipeline's
per-candidate similarity prior, a mechanism that doesn't exist anymore
now that a trained model decides chords directly. Removing the dead
Settings control itself is deliberately left as separate future cleanup,
not bundled into this change. `chord_theory.py`'s template-matching/
diatonic-chord functions (`build_templates`, `diatonic_chords`, etc.) are
no longer called by `detect_chords.py` but were left in place -- they're
independently tested (`tests/test_chord_theory.py`) and still work;
removing them is unrelated scope creep for a change already this large.

All 468 tests pass (up from 454 before this session's chord work) against
the real, rebuilt Python 3.11 environment -- no mocking of crema itself,
same convention this project already used for librosa. Verified end-to-
end through the real `detect_chords()` (not just raw `crema.analyze()`)
against Sunshine of Your Love and Hotel California post-integration,
confirming the label-simplification and independent key/BPM logic didn't
regress anything found during raw-crema validation.

## 2026-09-13 — Generate blocked by a blank work directory with no way to fix it

Owner reported Generate erroring "work directory is required" on a fresh
GUI run, with title also blank. Root cause: `_on_title_changed`/
`_on_audio_selected`'s background title-identification (a network lookup
when tags are missing) hadn't completed (or failed silently, by its own
existing design) by the time Generate was clicked, so `work_dir_var` --
which only ever gets set via that same title-driven auto-fill trace --
was still empty, and there's no Browse button for the work-directory
field itself to work around it. `_on_generate` now falls back to
`_default_work_dir_from_audio()` (same slug convention, derived from the
audio filename instead of an identified title) instead of erroring, so a
slow or failed background identification can never block Generate --
`run_pipeline`'s own identify stage still re-resolves the real title
independently either way.

## 2026-09-14 — GUI font silently broken by the crema venv rebuild; deterministic YouTube titles

Owner reported the whole program's font had gotten "really bad" -- small
and blocky in some areas -- after the 2026-09-13 crema/Python 3.11 work.
Chased several wrong leads first (a chord-timeline label-shrink feature
in the rendered VIDEO -- unrelated, owner clarified "not in the final
video, in the program"; then the native GTK file-picker's own system font
size, fixed with `gsettings` but reverted once the owner clarified the
broken font was specific to this app, not system-wide) before finding the
real cause: rebuilding `.venv` onto Python 3.11 (required for
`crema`/`tensorflow==2.15.0`, no 3.12 wheels) had used `uv`'s own
downloaded standalone interpreter, whose bundled Tcl/Tk 9.0 has no
working Xft/fontconfig on Linux -- confirmed directly
(`tkinter.font.families()` returned only 48 legacy X11 core fonts, e.g.
"fixed"/"helvetica"/"nimbus roman", none of them TrueType, and
CustomTkinter's requested "Roboto" resolved to `{'family': 'fixed',
'size': 24}`). `fc-cache`/fontconfig-cache fixes could never have worked
since Tk here was never using fontconfig at all. Fixed by installing
`python3.11`/`python3.11-venv`/`python3.11-dev`/`python3.11-tk` from the
`deadsnakes` PPA (verified via Launchpad's API, not just its HTML
package-listing page, which initially gave a misleading "not available"
read for the noble series) and rebuilding `.venv` on
`/usr/bin/python3.11` -- same exact CPython patch version (3.11.15) as
the `uv` build, so purely a Tcl/Tk linkage fix, zero functional change.
Confirmed with the same `tkinter.font.families()` check (268 real
families afterward) and a real screenshot of the running app. All 470
tests still pass after the full dependency reinstall.

Separately, the owner recalled real uploaded video titles like "November
Rain - Guns N' Roses - (Play Along Lyrics & Chords)" and wanted that
format "fixed" into the program -- turned out this was never a fixed
template: `generate_video_metadata()` always let Claude phrase the
YouTube title freely, and reading every `work/*/youtube_state.json` on
disk showed genuinely inconsistent results ("Play Along Lyric + Chord
Video", "Lyrics & Chords Play Along", etc., across different songs).
Replaced free-form title generation with a new deterministic
`build_play_along_title()` in `youtube_metadata.py` -- Claude still
writes the description/tags, never the title. Deliberately did NOT wire
the combined "{title} - {artist} - (Play Along Lyrics & Chords)" string
into the GUI's own "Song title" field: that field doubles as
`run_pipeline`'s `--title` override, which flows into lyrics search
(`fetch_lyric_lines`) and the video's own filename via `slugify()` --
some real song titles already contain their own separators (e.g. "Ready
For Love / After Lights"), so a composed marketing-style string landing
there risked breaking lyric lookup or producing a garbled filename.
Instead added a live, non-editable preview line under the field
(`gui.py`'s `_update_youtube_title_preview`, driven by a new
`self._identified_artist` set alongside the title by
`_apply_identified_title`) showing the exact real upload title without
touching the override field's value. Verified end-to-end against a real
audio file ("Angie" by The Rolling Stones, from an existing `work/`
folder): Song title stayed "Angie", preview correctly read "Angie - The
Rolling Stones - (Play Along Lyrics & Chords)". All 472 tests pass.

## 2026-09-14 — First Windows setup: CUDA-or-CPU Demucs, cross-platform launcher and paths

A second owner set the project up on Windows 11 (RTX 5070) for the first time.
Environment findings: only Python 3.12 was installed, so the venv was built on a
`uv`-managed 3.11 (the crema/TensorFlow 2.15 constraint from 2026-09-13 holds on
Windows too). PyPI's torch wheels are CPU-only on Windows; the cu130 build from
download.pytorch.org was swapped in and the command is now documented in
`requirements.txt`. torchcodec refused to load against winget's default
`Gyan.FFmpeg` package -- that build is static and ships no DLLs; `Gyan.FFmpeg.Shared`
fixed it (also noted in `requirements.txt`).

Code was Linux-only in four places, each found by the test suite (27 failures on
first run) or by reading the launch path:
- `separate.py` hardcoded `-d cpu` for the original GT 1030 box (2026-09-06 entry).
  Replaced with `compute_device()`: `cuda` when `torch.cuda.is_available()`, else
  `cpu`, with a `LYRICVIDEO_DEVICE` env override. Verified live: a synthetic clip
  separated on both `cuda` and `cpu`. Machines without a GPU are unaffected -- the
  default `requirements.txt` install still yields CPU torch and the fallback picks it.
- `pipeline.py` `default_font()` only listed `/usr/share/fonts/...` candidates, so
  every render and settings-preview test died with "No default font found" (25 of
  the 27 failures). Added Windows Arial Bold / Segoe UI Bold.
- `gui.py` spelled out `.venv/bin/python` for self-update's pip install and the
  Relaunch button; both now go through the new `lyricvideo/venv.py` `venv_python()`.
  Added `run_playalongvideoproduction.bat` as the Windows launcher, and the update
  allow-list's bare-top-level rule now accepts `.bat` alongside `.py`/`.sh` so
  releases can update it.
- Git on Windows with `core.autocrlf=true` checked out `*.sh` and
  `.githooks/pre-commit` as CRLF, which bash rejects. New `.gitattributes` pins them
  to LF (and `*.bat` to CRLF).

Two tests can never pass on Windows and now skip there: the trailing-space
batch-folder test (NTFS silently strips a trailing space at mkdir time, so the
scenario from 2026-09-10 cannot be constructed) and the symlinked-destination update
test (symlink creation needs Developer Mode or admin). Final Windows run: 404 passed,
75 skipped, 0 failed.

## 2026-09-14 — Full codebase review: blank-background gap, GUI-thread network calls, silent Windows test skips

A full read-through of every module, script, launcher and test, with each
finding fixed the same day. Baseline on Windows before: 401 passed, 75
skipped, 3 failed (the align tests -- torchcodec couldn't find FFmpeg's
shared DLLs because the review's shell predated the winget install; with
that `bin/` on PATH all 6 pass, so purely environmental). After: 495 passed,
2 skipped (the two documented Windows-only skips), 0 failed.

- **73 render/assemble/chord-diagram tests silently skipped on Windows.**
  `tests/conftest.py`'s `test_font_path` fixture only knew the two Linux
  font paths, so every test taking it skipped -- a whole layer of the suite
  that had looked green here because it never ran. Added the same Windows
  candidates `pipeline.default_font()` already lists. That immediately
  exposed one test asserting a DejaVu-specific pixel span (Arial Bold wraps
  the same lyric onto fewer rows); it now asserts the real invariant --
  the current and next lyric blocks, told apart by color, never overlap --
  whatever font the machine renders with.
- **Instrumental background images the images stage never generated.**
  `pipeline._instrumental_chord_labels()` picked chords by whether their
  MIDPOINT fell outside every sung line, while `layout.build_image_timeline()`
  keyed segments by OVERLAP with each gap and used a generic `[Instrumental]`
  key for any sub-range the chord track didn't cover -- a key nothing ever
  generated an image for. Either mismatch rendered as the flat
  `fallback_color`, the exact "blank screen" class the owner complained about
  on 2026-09-10 (for the countdown, which was patched in isolation then).
  Fixed at the source: `layout.instrumental_caption()` is now the only place
  the caption string is spelled; `layout.instrumental_image_captions()` walks
  the identical gaps and micro-segments the timeline does and the images
  stage generates exactly that list; an uncovered sliver (a chord track
  starting a hair after 0, or ending a hair before the decoded audio does)
  adopts its neighboring chord's caption instead of the generic one, which
  now only exists for a song with no chord events at all; and
  `assemble_video()`'s `get_image` falls back to the nearest real image (the
  countdown's own rule) rather than a flat color for any key still missing.
  `pipeline.song_end_time()` supplies the horizon without decoding audio.
- **Demucs progress never reached the GUI log.** `separate_vocals()` used
  `subprocess.run`, and a child process inherits the OS-level stdout, not
  the Python `sys.stdout` object the GUI swaps for its log-widget writer --
  so the slowest stage of the pipeline showed nothing in the log (and, for
  a desktop-launched app, nowhere at all). `_run_demucs()` now pipes the
  child's stdout+stderr and relays whatever bytes are available through the
  current `sys.stdout`, so tqdm's `\r` updates stream live through the
  existing `_split_log_text` handling.
- **Network calls on the GUI thread.** `_refresh_youtube_status()` ran
  `load_credentials()` (a token refresh is a network call) and
  `get_channel_title()` (an API call) straight from `__init__`, so the main
  window couldn't finish appearing until YouTube answered -- or timed out,
  offline. `_update_upload_button_state()` did the same with `video_exists()`
  every time a video finished or New Song was clicked. Both now run their
  verification on a background thread (the way the 20-minute tick already
  did) and hand the result back via `root.after`; the upload button shows
  disabled while a check is in flight, and a request counter makes a newer
  request supersede a slower older one. Verified with a real Tk mainloop
  smoke script: construction returns in ~0.15s against a stubbed 1-second
  channel lookup, and the label/button states land correctly afterwards.
- **Resuming past `separate` with no stems on disk crashed.** A Redo, a
  batch "already done" song, or a CLI `--stage fetch_lyrics|align|detect_chords`
  read `htdemucs/<stem>/vocals.wav` unconditionally. Missing stems now
  re-run Demucs (deterministic, no API spend) -- the same self-heal the
  identify bootstrap already applied to a missing `song_info.json`. A
  resume at images/render never reads the stems and is left alone.
- **Fonts re-loaded from disk on every frame.** Every `draw_*` helper called
  `ImageFont.truetype` directly -- about 25 loads per frame with a typical
  chord legend, minutes of pure font loading per render. `render.load_font()`
  caches per (path, size) PER THREAD; FreeType faces are not safe to share
  between the GUI thread's live Settings preview and a worker-thread render.
- **`youtube_auth.load_credentials()` handed back unusable credentials** for
  a stored token that had expired with no refresh token; every later YouTube
  action then failed with an auth error. It now returns `None` (not
  connected) so the owner is prompted to reconnect instead.
- **`cut_release.sh` never shipped the Windows launcher.** `apply.py` had
  accepted a bare top-level `.bat` since the Windows-support commit, but the
  sync list only ever included the `.sh`, so a Windows install could never
  receive a launcher fix through Apply Update.
- Smaller: `pipeline` raises a clear RuntimeError (not KeyError) when
  `REPLICATE_API_TOKEN` is unset; `detect_chords._merge_short_events` no
  longer mutates the caller's events in place; `fetch_lyrics._lrclib_get`
  parses the response body once; Generate/Redo grey out the Start Batch
  button like Start Batch greys out theirs; the `requirements.txt` CUDA
  swap command had lost its line break; the two `branding/` scripts
  resolve fonts per OS instead of hardcoding the Linux DejaVu path; the
  countdown's background key is resolved once per render, not per frame.

Reviewed and deliberately left alone: `torchaudio.load` in `align.py`/
`pipeline.py` (torchcodec + FFmpeg shared DLLs are a documented environment
requirement, and Demucs itself needs the same stack); `chord_theory.
build_templates`/`diatonic_chords` (unused since the crema switch but
harmless and tested); the unbounded per-render image cache (a few hundred MB
for a long song, acceptable); `root.after` from worker threads throughout
`gui.py` (the app's established pattern, fine while `mainloop` runs).

## 2026-09-14 — Issue #3: a numeric lyric word ("31") aborted forced alignment

Owner reported (GitHub issue #3) `AlignmentError: word '31' has no alignable
characters after normalization`, from the align stage. Root cause: the
MMS_FA forced-alignment model's dictionary is a-z plus the apostrophe (and
its own `-` blank and `*` star tokens), and `_normalize_word_for_alignment`
stripped everything else from a lyric word and RAISED when nothing was left.
Lyric providers return numbers as digits ("31", "1975") and sometimes
symbol-only tokens ("&", "..."), so any such word killed the whole stage --
deterministically, since Redo re-fetches the same lyrics, leaving that song
unprocessable until the code changed. Nothing upstream converted digits.

Fix, alignment-spelling only (display text is untouched): digit runs are
spelled out as sung and joined into ONE aligner word so they still map to
one span -- "31" -> "thirtyone", 4-digit runs in 1100-1999 read as years
("1975" -> "nineteenseventyfive", "1905" -> "nineteenohfive"), 2010-2099
as "twenty ..."; ordinals ("31st" -> "thirtyfirst"); runs longer than four
digits or with a leading zero read digit by digit ("867-5309" the way it's
sung); "&" reads as "and"; and a word with nothing left ("...", a dash, an
emoji) becomes the model's `*` star token -- MMS_FA's documented wildcard
for unalignable audio, already enabled by the default
`get_model(with_star=True)` this code uses -- instead of an exception.
Guarded by a test that runs every kind of normalized spelling through the
REAL `MMS_FA.get_tokenizer()` (dictionary only, no model download), since
an unknown character is exactly the failure class this was.

## 2026-09-14 — Second full codebase review: silent GUI error dialogs, leaked ffmpeg readers, hidden intro line

A second complete read-through of every module, script, launcher and test
(the first was earlier the same day, above), this time backed by ruff
(`--select F,E9,B,PLE`) for the mechanical checks a read can miss. Baseline
before: 503 passed, 2 skipped. After: 522 passed, 2 skipped, 0 failed; ruff
reports nothing but optional `zip(strict=)` hints, deliberately left.

- **Three GUI error dialogs could never appear.** `_on_connect_youtube`,
  `_on_manual_upload` and `_on_approve_reply` each ran in a worker thread
  and, on failure, scheduled `messagebox.showerror(..., f"{e}")` via
  `root.after(0, lambda: ...)`. Python unbinds the `except ... as e` name
  the moment the except block ends, so by the time Tk ran the lambda it
  raised `NameError: cannot access free variable 'e'` -- swallowed by
  Tkinter's callback handler, invisible on a desktop-launched app. A failed
  connect, a failed manual upload (e.g. `uploadLimitExceeded`, hit live on
  2026-09-12), or a failed reply post simply showed nothing. Ruff's F821
  found it. The message is now formatted inside the except block and the
  lambda captures the string. Tests drive the real methods against a stub
  `self` with `threading.Thread` swapped for a synchronous stand-in.
- **Every render leaked an ffmpeg reader subprocess.** `assemble_video()`
  never closed its `AudioFileClip`; moviepy 1.0.3's own `Clip.close`
  docstring says it must not be done from `__del__`, so the reader (and
  its open handle on the source audio) lived until interpreter exit -- a
  batch run accumulated one per song, and on Windows each audio file stayed
  locked for the session. Closed in a `finally`, verified on both the
  success and the write-failure path.
- **The first lyric line was invisible for the whole intro.** The
  2026-09-10 stale-line gate (blank the "current" line's text whenever
  `_in_a_line()` is false) also fired before the first line had started,
  because `find_current_line_index` reports index 0 then. So the intro
  previewed line 2 as "next" while line 1 stayed hidden until its first
  word popped in already highlighted. The gate now applies only once the
  current line has begun; the intro shows line 1 unhighlighted at center
  with line 2 below, exactly the pre-gate layout, and the mid-gap blanking
  the owner asked for is unchanged (both covered by tests).
- **Plain lyrics were discarded when the audio duration was unknown.**
  `fetch_lyric_lines()` routed unsynced text through `plain_to_lines()`,
  which fabricates evenly-spread timing and returns `[]` for
  `duration <= 0` -- but only the TEXT is kept from this stage, so a file
  whose length `probe_duration` couldn't read lost lyrics the provider had
  actually found (sidecar `.txt` included). Plain text is now split into
  lines directly.
- **`minmaj7` relabeled as major.** crema's real vocabulary is pumpp's
  `3567s` QUALITIES table; `minmaj7` (minor third, major seventh) is in it
  and was missing from `_QUALITY_TO_TRIAD_OR_SEVENTH`, whose `.get()`
  default is `"maj"` -- so `A:minmaj7` displayed as A major. Mapped to
  `min7`/`min`; a test pins every pumpp quality to an explicit entry.
- **A song with no lyric text died with "no words to align".** The align
  stage now raises a RuntimeError naming the song and the exact
  `<stem>.lrc`/`.txt` sidecar to add, on a fresh run or a `--stage align`
  resume alike.
- **`combine_alignment` could reject a valid last word.** `align_words`
  resamples the stem with a ceil'd length, so a word sung to the final
  sample can end up to 1/16000 s past the original-rate duration and used
  to trip the `end > audio_duration` sanity check. The check now shares
  `MONOTONIC_TOLERANCE`.
- Smaller: `Settings.render_kwargs()` falls back to 1080p (with a warning)
  for an unknown resolution label instead of a KeyError that took down
  every render and the Settings window's preview; Generate and Redo check
  the audio file exists up front and name the path, instead of an obscure
  Demucs/ffmpeg failure minutes later; `assemble_video` caches backgrounds
  already scaled to the frame and `apply_ken_burns` skips its first resize
  when the input is already frame-sized (identical pixels, one fewer
  1080p resample per frame); unused imports removed from eleven test
  modules and `branding/generate_channel_banner.py`.

Reviewed and deliberately left alone this pass: CPU-only MMS_FA inference
(`align.py`; a full-song emission on GPU risks OOM and alignment is not the
slow stage); `torchaudio.load` of the whole vocal stem just for its duration
in `pipeline.py` (~85 MB transient, freed immediately); `next_chord_after`'s
linear scan per frame (a few hundred events); redrawing the chord legend's
diagrams on every frame (small draws, no measured cost); the `zip()`
`strict=` hints.

## 2026-09-14 — Issue #5: Demucs crashed on the GT 1030 box, "no kernel image is available"

The original Linux box (GeForce GT 1030, Pascal, compute capability 6.1) hit
`torch.AcceleratorError: CUDA error: no kernel image is available for execution
on the device` on its first Generate after the Windows-support commit earlier
today. That commit replaced `separate.py`'s hardcoded `-d cpu` (added
2026-09-06 for exactly this card) with `compute_device()`, which trusted
`torch.cuda.is_available()`. On that box it is True -- the venv carries the
`torch==2.14.0+cu130` wheel, and a CUDA device and driver exist -- but the
cu130 wheel is built for sm_75 and up only (CUDA 13 dropped Maxwell/Pascal/
Volta), so Demucs's first `th.arange(..., device="cuda")` had no kernel to run.
torch had printed a UserWarning saying as much into the subprocess's stderr,
right above the traceback in the issue.

- `compute_device()` now asks what the build was compiled for
  (`torch.cuda.get_arch_list()`) and what the GPU is
  (`torch.cuda.get_device_capability()`), and picks `cuda` only when the new
  pure-logic `cuda_build_supports_device()` says a kernel can run there --
  NVIDIA's cubin rule, the same one torch's own `_check_capability` applies:
  code built for sm_XY (or PTX compute_XY) runs on hardware of the same major
  X with minor >= Y. Arch-specific variants (`sm_90a`, `sm_100f`) count as
  their base; a list with nothing it can judge (ROCm `gfx` names, empty) is
  trusted rather than switching a working GPU off. When it falls back it
  prints why (GPU name, CC, the build's arch list) to stdout so the GUI log
  shows it, and names the `LYRICVIDEO_DEVICE=cuda` override. A probe that
  itself raises is treated as `cpu`.
- `separate_vocals()` retries once on CPU when an AUTO-picked GPU run exits
  non-zero (too little memory for the model, a driver/runtime mismatch --
  things only the real run reveals), logging the retry. An explicitly
  requested device (`device=` argument or the env override) is never
  second-guessed and fails loudly, as asked; a CPU failure is never retried.
- The `requirements.txt` comment claimed PyPI's torch wheel is CPU-only on
  Linux too; it isn't (Linux PyPI wheels are CUDA builds). Reworded, and it
  now says cu130 builds need a 7.5+ card and a GT 1030-class card wants the
  cu126 index -- torch's startup warning prints the exact command, and its
  own release table confirms cu126 x86_64 wheels carry sm_60.

Verified against the real cu130 wheel on the Windows box: arch list
`sm_75 sm_80 sm_86 sm_90 sm_100 sm_120`; the RTX 5070 (12.0) still picks
`cuda`, a simulated (6, 1) picks `cpu`. `tests/test_separate.py` grew from 9
to 29 tests. Not verified: an actual run on the GT 1030 box itself.

## 2026-09-15 — Redo audio for batch songs, and a retry-upload GUI for failed YouTube uploads

Two items triaged into `TODO.md`, both now implemented and resolved (the file is
empty again).

**Redo broke for any song originally processed via Batch.** `load_redo_inputs()`
read back the ORIGINAL absolute `audio_path` from the song's own
`lyrics_timed.json`, but a batch-run song's path points into the owner's "batch
music" staging folder -- emptied before the next batch run, by the owner's own
workflow, so an older batch song's source file was long gone by the time a Redo
was attempted (reported by the owner 2026-09-14). Fix (owner's own decided
approach): `run_pipeline()` now copies the source audio into the song's own
`work_dir` under its original filename, early (right after `work_dir.mkdir()`),
skipped once the copy already exists or the source doesn't (so it's a no-op, not
an error, for every existing test's fake `Path("audio.mp3")`). `load_redo_inputs()`
prefers that local copy over the external `audio_path`, falling back to the
original external path for a song generated before this fix existed -- an older
work dir keeps working exactly as before as long as its source file hasn't
actually been deleted yet.

**No way to retry a failed YouTube upload** (e.g. hitting the daily
`uploadLimitExceeded` cap) without a one-off script calling `schedule_upload()`
directly -- hit live 2026-09-12 during a batch run. New `list_pending_uploads()`
in `pipeline.py` (filesystem-only, same shape as `list_redoable_songs()`): a
`work/*` folder counts as pending when it has a rendered `<slug>.mp4` but no
`youtube_state.json` yet -- `schedule_upload()` only ever writes that file AFTER
a successful upload, so a missing one is already the right signal without a live
YouTube API call per song. The GUI's "Redo an Existing Song" section grew a
second row, "Retry a Failed Upload": a dropdown of pending songs, an "Upload"
button for the selected one, and "Upload All Pending" which loops through every
pending song, logging and skipping any individual failure and continuing to the
next (same behavior as the batch pipeline worker) rather than aborting the whole
retry run the moment the still-active daily cap rejects the first one. Both
buttons call the same `schedule_upload()` path as auto-upload and the existing
manual Upload button, and -- confirmed with the owner -- work regardless of the
`youtube_auto_upload` setting, same as that existing manual button: an explicit
click is a deliberate override, not something the auto-upload toggle should gate.

Built via the brainstorming skill's bounded path (short in-chat design, no spec
file) and TDD throughout; `tests/test_pipeline.py` and `tests/test_gui.py` grew
new coverage for `list_pending_uploads()`, the redo audio-copy fallback, and
`_retry_pending_uploads()`. Full suite: 557 passed.

## 2026-09-15 — Two videos published within hours instead of days: `compute_next_publish_slot()` never checked whether its own answer was still in the future

The owner noticed the night before that two of that batch run's videos (`Ready
For Love / After Lights`, `Purple Rain`) had gone fully public within 1.5-3
hours of upload, instead of landing on a future scheduled date days out like
every other video from that same run -- and had already corrected the schedule
on YouTube directly by the time this was reported.

Root-caused by pulling each affected video's real `status`/`snippet` straight
from the Data API (`videos().list`) and cross-referencing against each song's
own `youtube_state.json` `uploaded_at` timestamp, rather than guessing: real
settings at the time were `youtube_preferred_upload_hour=14`,
`youtube_min_days_between_uploads=1`. `compute_next_publish_slot()`
(`youtube_schedule.py`) walked forward day by day looking only for a DATE with
no scheduling conflict -- it never checked whether the resulting DATETIME (that
date at the preferred hour) was still ahead of `now`. A video uploaded late in
the evening that happened to find "today" still unclaimed (legitimate under the
gap-filling design -- e.g. an earlier video in that slot already published)
got back "today at 2pm", several hours in the past by 8pm; YouTube auto-
publishes a past `publishAt` almost immediately rather than holding it for a
future date. The function's own docstring had actually documented this as
deliberate ("in the past if `now` is already later than that today ... this
still means 'now'") for the very-first-video-ever case, but nothing
distinguished that intended case from this unintended one.

Fix: the slot search now also requires the candidate to be strictly after
`now`, rolling to tomorrow (re-checking for date conflicts there too) when
today's preferred hour has already passed, instead of ever returning a stale
time. Two new regression tests in `tests/test_youtube_schedule.py` pin the
exact failure (`now` at 8pm, preferred_hour 14, nothing claimed -> must return
tomorrow, not today) both with and without an already-claimed date in the way.
All 19 pre-existing tests in that file still pass unchanged -- every one of
them used a `now` before that day's preferred hour, so none exercised this
path. Full suite: 559 passed.

## 2026-09-15 — Folded the manual "Upload to YouTube" button into the Retry Upload controls

The owner had never once used the status-line manual "Upload to YouTube"
button. Its only capability the new Retry Upload feature (same day, earlier
entry) didn't already cover was forcing a fresh re-upload of a song that had
already posted successfully (a correction/re-post) -- so rather than keep two
separate upload UIs, folded that case into the one under Redo and deleted the
standalone button.

New `list_pending_uploads()`-style `list_rendered_songs()` in `pipeline.py`:
every `work/*` song with a rendered video, uploaded or not (unlike
`list_pending_uploads()`, which deliberately excludes anything already
recorded). The single-song "Upload" dropdown now uses this instead, so any
past song is reachable, not just the current session's just-finished one.
Clicking Upload on a song that already has a `youtube_state.json` now shows a
confirm dialog first ("upload again and create a duplicate?") -- a stray click
reaching any historical song is a real risk the old button never had, since it
only ever touched `self._last_work_dir`. "Upload All Pending" is untouched,
still scoped to `list_pending_uploads()` only, no confirmation needed (no
duplicate risk there).

Deleted `_on_manual_upload`, `_update_upload_button_state`,
`_upload_button_state_worker`, `_apply_upload_button_state`, the
`upload_button`/`upload_status_label` widgets, and their one remaining test
(`test_manual_upload_failure_shows_the_error_dialog`) -- replaced by three new
stub tests covering the confirm/skip-confirm/decline paths on the single
Upload button. Section header renamed "Retry a Failed Upload" ->
"Upload to YouTube" since it's the only upload UI now. TDD throughout; full
suite: 564 passed.

## 2026-09-15 — Made the Redo/Upload-to-YouTube/Pending-uploads lists scrollable

Owner complaint: the Redo and Upload-to-YouTube song pickers had grown too
long to be usable. Both were plain `CTkComboBox` dropdowns, which pop a
native OS menu (a plain `tkinter.Menu` under the hood, confirmed by reading
customtkinter's own `dropdown_menu.py`) -- on this owner's window manager
that menu could run off-screen once the song list got long enough, with no
scrollbar of its own. Separately, "Upload All Pending" acted on
`list_pending_uploads()` completely blind -- there was no way to see which
songs were pending, or to upload only some of them rather than all-or-nothing.

Fix: `_populate_song_radio_list()` rebuilds the Redo and Upload-to-YouTube
pickers as `CTkRadioButton` rows inside a `CTkScrollableFrame` fixed to a new
`SONG_LIST_HEIGHT` constant (420px, ~15 rows before it scrolls -- owner's
own number, after an initial back-and-forth from 10). Replaced "Upload All
Pending" with a new "Pending YouTube Uploads" panel: a `CTkCheckBox`
checklist over `list_pending_uploads()` (`_refresh_pending_uploads_list()`),
a "Select All" toggle, and an "Upload Selected" button
(`_on_upload_selected_pending()`) that reuses the existing
`_start_retry_upload()`/`_retry_pending_uploads()` path unchanged. A song
needs no special "remove from pending" handling -- `schedule_upload()` only
ever writes `youtube_state.json` after a real success, so a song simply
stops matching `list_pending_uploads()`'s filesystem scan on the next
refresh (called automatically after every upload batch finishes).

Three ~15-row scrollable lists don't fit in the existing 820px-tall window
alongside the song form and the log console -- offered the owner three
layout options (shrink each list, move lists into popups, or scroll the
whole left-hand column as one unit) and they picked the latter. Wrapped
everything above the log console in `left_scroll`, a `CTkScrollableFrame`;
`left` itself switched from a single pack()'d column to a 3:2-weighted grid
(`left_scroll` row 0, the log widget row 1), and the window's default height
grew from 820 to 900 to fit more comfortably on a normal monitor. The old
`retry_upload_combo`/`redo_combo`/`retry_upload_all_button` attributes and
the now-dead `_on_retry_upload_all` were removed; `retry_upload_all_button`
was renamed `upload_selected_button` throughout (`_start_retry_upload` still
disables/re-enables it during a run) and the one test that referenced it by
name was updated to match, plus three new stub tests for the select-all
toggle and the selected-only upload path. Full suite: 567 passed.

Verified live: launched a second GUI instance under the owner's real
session (careful not to touch the owner's own already-running instance,
mid-batch at the time -- confirmed by PID/start-time before touching
anything, and killed only the new instance by PID afterward, never by
window title, since both instances shared the exact same window title).
Screenshots confirmed the Redo list renders real, alphabetically-sorted
songs from `work/`, scrolls internally, and the overall layout isn't broken
or overlapping. Automated click/drag verification of the scrollbars and
checkboxes themselves was inconclusive -- synthetic `xdotool` input wasn't
reaching the window in this sandbox (confirmed via a plain radio-button
click that never registered, ruling out a scrollbar-specific bug) -- so full
interactive confirmation (scrolling, checking boxes, Select All, Upload
Selected) still needs a manual pass by the owner.

## 2026-09-15 — Added per-song Watch and Remove-from-list to the song lists

Follow-up to the same day's scrollable-lists work. Owner asked for two more
things on the Redo/Upload-to-YouTube/Pending-Uploads lists: a way to preview
a song's rendered video before deciding whether to upload it, and a way to
get a song off a list that's grown cluttered. The first ask for "delete a
song" turned out to mean something different once asked directly: "dont
destroy the file.. just remove it from the list" -- so this is a display
filter, not `shutil.rmtree`. A follow-up question narrowed the removal
scope further: per-list (a song dismissed from Pending Uploads should still
be reachable via Upload to YouTube, e.g. if the owner changes their mind
about it later) rather than one global hide-everywhere action.

Added `pipeline.song_video_path(work_dir) -> Path | None`, factored out of
the near-identical slugify+exists checks that `list_rendered_songs()` and
`list_pending_uploads()` already had -- both now call it instead of
duplicating the logic, and the GUI's new Watch button reuses it too.
`_on_watch_song()` hands the path to `_open_with_default_app()`
(`os.startfile` on Windows, `xdg-open` on Linux, `open` on macOS) -- no
in-app video player, just a hand-off to whatever the owner already uses.
Shows an error dialog if the song hasn't been rendered yet, or if the OS
open call itself fails (no registered player, etc).

New `lyricvideo/dismissed_songs.py` (same tiny-separate-JSON-file pattern as
`batch.py`'s `_STATE_FILE`, deliberately not part of `Settings`): a single
`~/.playalongvideoproduction/dismissed_songs.json` holding
`{"redo": [...], "upload": [...], "pending": [...]}`. `_populate_song_radio_list()`
and `_refresh_pending_uploads_list()` both filter their song list through
`load_dismissed(list_name)` before building rows. Each row is now built by a
shared `_build_song_list_row()` (selector widget + Watch + ✕, used
identically by the radio-list and checklist paths) -- the ✕ button calls
`_on_remove_song(list_name, slug)`, which confirms first (its dialog text
says explicitly that files aren't touched and the song may still appear
elsewhere), then `dismiss_song()`s it and calls the new `_refresh_song_list(list_name)`
dispatcher to rebuild just that one list rather than all three. There's no
"show hidden"/restore UI yet -- not asked for; the JSON file itself is
plain enough to hand-edit if ever needed.

New tests: `tests/test_dismissed_songs.py` (round-trip, per-list scoping,
idempotent dismiss, corrupt/missing file), three new `test_pipeline.py`
cases for `song_video_path()`, and new `test_gui.py` stub tests for
`_on_watch_song()` (found/not-rendered/open-fails), `_on_remove_song()`
(confirm/decline), and `_refresh_song_list()`'s three-way dispatch. Full
suite: 585 passed.

Verified live the same way as the scrolling-lists work earlier today:
launched a second GUI instance (none of the owner's own was running this
time), screenshotted the Redo list, and confirmed each row now shows its
radio button plus "▶ Watch" and "✕" cleanly laid out with no overlap
across ~15 real songs from `work/`. Did not attempt to click-verify the
buttons themselves, for the same synthetic-input-doesn't-reach-the-window
reason logged earlier today -- Watch/Remove still want a manual pass by the
owner.

## 2026-09-15 — Reverted the outer-scroll layout: nesting CTkScrollableFrame is broken

v2.0.9's outer-scroll fix (the previous entry above) shipped broken. The
owner's own long-running GUI session -- already open, mid-batch, untouched
by any of this session's testing -- hit it live within minutes of the
release: everything below the Redo list (Upload to YouTube, Pending
Uploads, Batch, the log console, even the right-hand YouTube panel column)
was permanently unreachable. Screenshotting the owner's actual live window
(read-only, never interacted with) confirmed it precisely: the outer
scrollbar was visibly present but never moved no matter how it was
dragged, trapping the whole rest of the page behind roughly the top third
of a single 420px-tall Redo list.

Root cause: `customtkinter.CTkScrollableFrame` binds mouse-wheel handling
via `bind_all` per instance and its ownership check
(`_check_if_valid_scroll`) returns False the instant it walks up the
widget tree and hits ANY OTHER `CTkScrollableFrame` instance, rather than
continuing further up to check if that one's own ancestor eventually
belongs to the outer frame. Nesting one `CTkScrollableFrame` inside
another is fundamentally unreliable in this customtkinter version -- see
[[feedback-never-nest-ctkscrollableframe]] memory. (The scrollbar-thumb-
drag path also never worked for the owner in practice; not fully
diagnosed once the nesting itself was identified as the thing to remove
rather than chase further.)

Fix: reverted `left_scroll` entirely -- `left` is back to a plain
pack()'d column, `log_widget` back to `.pack(fill="both", expand=True)`.
In the same pass, the owner asked for the three song lists to be CLOSED by
default rather than always open (a request that also happens to solve the
underlying space problem cleanly): new `_make_collapsible_section()`
builds a header button that `pack()`/`pack_forget()`s a content frame --
no nested scroll regions, no `bind_all` conflicts, just ordinary Tkinter
geometry management. Each of the three sections (Redo, Upload to YouTube,
Pending Uploads) now starts collapsed; clicking the header (▶ ↔ ▼) expands
it in place. `SONG_LIST_HEIGHT` went back up to 420 (~15 rows) since only
open sections compete for space now. Window default height bumped
900 -> 1000 for a bit more breathing room when a section is open; still a
plain resizable window (never was overridden), so anything that still
doesn't fit is one drag/maximize away, not trapped.

Verification this time: couldn't trust synthetic clicks (see the
GUI-automation-limitation memory), so built a throwaway script
(`verify_toggle2.py`) that constructs the real `LyricVideoGUI` instance
against a real (shown, not withdrawn) `ctk.CTk()` root, calls
`_make_collapsible_section()` for real, and invokes the toggle button's
`command` callback directly via `.cget("command")()` -- confirmed
`content.winfo_ismapped()` flips 0 -> 1 -> 0 across two toggles, with the
button label flipping ▶ ↔ ▼ in step. This exercises the real code path
end-to-end without depending on synthetic mouse events reaching the
window, unlike the visual-only screenshot checks used earlier today. Full
suite: 585 passed (no test changes needed -- this was a pure layout
revert plus new widget-construction code that isn't unit-tested the same
way, consistent with this file's existing GUI-construction methods).

Also fixed live during this investigation: the systematic-debugging task
the owner had queued (musical-intro/instrumental-gap line-display and
image-flicker bugs in `layout.py`/`render.py`) was interrupted mid-Phase-1
by this more urgent live breakage -- still open, not started on a fix,
Phase 1 investigation only partially done (see the interrupted
conversation for partial root-cause notes on `_in_a_line`'s per-gap
`min_hold_seconds` merging not bridging across a short inter-line pause
into the surrounding sung segments).

## 2026-09-15 — Fixed both display-gap bugs: premature next-line text, and image flicker on short pauses

Picked back up the interrupted systematic-debugging task from the entry
above once the layout crisis was resolved. Owner report: during a song's
musical intro, and during any mid-song instrumental break, the display
showed the SECOND lyric line the whole time instead of the first/current
one; separately, small natural pauses BETWEEN two sung lines (not real
instrumental breaks) were read as instrumental gaps, flashing a distinct
chord-following background image for a moment before reverting to the
next line's own image -- "all very chaotic."

Root-caused both with a synthetic repro script
(`build_scene`/`_in_a_line`/`build_image_timeline` called directly with
constructed `LyricLine`/`ChordTrack` data, matching this file's own test
style) before touching any code, then re-verified both fixes against a
REAL rendered song's `lyrics_timed.json` (`work/angie-rolling-stones/`) to
confirm the synthetic repro generalized.

**Bug 1 (premature next-line text):** `build_scene()`'s 2026-09-10 "stale
line" fix blanked the CURRENT line's text once its own plausible singing
was over, for the rest of the gap before the next line began -- but the
"upcoming" (distance_from_current=1) line was ALWAYS shown in full
regardless, with no visual distinction from a truly-current line (same
font, same color; only actively-sung words get the karaoke highlight, and
none are active on an unsung preview either way). So during any gap the
ONLY visible text was the next line, reading as "the display already
jumped to it" -- exactly the report, and NOT limited to the intro (the
2026-09-14 fix already correctly special-cased the pre-first-line case;
every OTHER inter-line gap still had this bug). Fix: once t moves past a
line's own `_plausible_line_end()`, `build_scene()` now advances "current"
to the next line early, so it displays in the exact same unsung/upcoming
style the intro case already used correctly -- one consistent code path
for "nothing is being sung right now, here's what's coming" instead of two
(one correct, one buggy). Strictly `t > plausible_end` (not `>=`): at the
exact instant a line ends, it's still that line's own final frame,
confirmed by `test_build_scene_ken_burns_progress_spans_gap_until_next_line_not_just_singing_end`
needing `scroll_progress == 1.0` right at that boundary. Only advances
onto a line with real timing data, and only past the LAST line (nothing
left to advance onto) does the old blanking behavior still apply, for the
outro.

**Bug 2 (image flicker on short pauses):** `build_image_timeline()`
processes each gap between sung lines independently, merging short
consecutive chord segments forward via `_merge_into_hold_blocks()` until
they reach `min_hold_seconds` -- but a gap with only ONE short micro-
segment inside it has nothing else in that same gap to merge with, so it
always got emitted as its own block regardless of `min_hold_seconds`,
by that function's own explicit, otherwise-correct design (a trailing
short run at the very end of the SONG has nowhere else to go either, and
should still be shown). Fix: `build_image_timeline()`'s new
`extend_into_gap_or_insert()` checks the WHOLE gap's duration before
calling into per-gap merging at all -- if it's shorter than
`min_hold_seconds` AND a previous segment already exists to extend, that
previous segment's own `.end` is pushed forward to swallow the gap
entirely, with no new segment inserted. The one real transition then
happens exactly at the next line's own start, once real singing resumes.
Deliberately does not touch the leading intro (no previous segment exists
yet there to extend) -- not what was reported, and a good default anyway
(a short intro's own instrumental image is still worth showing).

Two existing tests encoded the old (buggy) behavior as intentional and
needed rewriting rather than just extending:
`test_build_scene_blanks_stale_current_line_during_a_real_instrumental_gap`
(renamed `..._advances_to_the_upcoming_line_...`) and
`test_build_scene_still_blanks_a_finished_line_in_the_gap_before_the_next_one`
(renamed `..._still_blanks_the_last_line_during_the_outro`, since that's
the only case still covered post-fix). Four new tests added covering the
next-line advance (mid-gap, and with a further line after that as the new
"upcoming"), the short-pause text case, and three new image-timeline tests
(short gap holds through, long gap still gets its own segment, leading
intro unaffected). Full suite: 590 passed.

## 2026-09-15 — No lyrics at all during most of an intro or a solo

Follow-up to the same-day fix above. Once that shipped, the owner tried it
and had a different, more specific preference than "show the upcoming line
the whole time a gap lasts": "I don't want the lyric displayed in any just
music... right before vocals start the current and next line are
displayed, in intro and solos no lyrics." So the unsung-preview text
(current AND next line together) should stay hidden through most of an
intro or a mid-song solo, and only appear in the final stretch before
vocals actually resume -- not the instant the previous line finishes.

Asked one clarifying question (AskUserQuestion) on how many seconds of
lead-in to default to; owner picked 3 seconds over 5 or a custom value.

New `Settings.lyric_preview_lead_seconds: float = 3.0`, following the
exact same "owner-tunable *_seconds field" pattern as
`support_overlay_lead_seconds` (which gates when THAT overlay shows,
relative to the song's end, rather than a line's start). Added to
`render_kwargs()`, a new slider in `SettingsPanel`'s existing "Image
pacing" section (`settings_panel.py` field labels must stay short -- see
CLAUDE.md's own warning on this -- so "Lyric preview lead-in", not a
longer description), and threaded as a new `build_scene()` parameter
(default matches Settings' own default) through `assemble.py`'s per-frame
call. The countdown's own separate `build_scene(..., t=0.0, ...)` call in
`assemble.py` doesn't need it -- it only reads `scene.image_key`, never
`scene.lines`. `settings_preview.py`'s live preview pane builds a
hand-crafted fake `Scene` directly rather than calling `build_scene()` at
all, so it needed no changes.

Implementation: a new `hide_until_vocals_are_close` condition in
`build_scene()`, true whenever "current" is itself an upcoming (not yet
started) line AND `t` is still more than `lyric_preview_lead_seconds`
before that line's own `start_time`. Applied to BOTH the current and
upcoming slots in the scene_lines loop (unlike the pre-existing
stale-finished-line blank, which only ever applied to the current slot) --
deliberately independent of `is_current`, since the whole point is hiding
everything during the bulk of an intro/solo, not just one slot. Only
gates the "current is an unsung, not-yet-started line" case; once real
singing is underway (`in_a_line`/`current_has_started` both handle that
already) or past the last line (nothing left to gate against), this new
condition can never fire.

Three existing tests needed their sample `t` moved closer to the relevant
line's own start (they were unknowingly relying on the old
"show-immediately" behavior, now superseded by the 3-second default lead):
the gap-advance test, the third-line "upcoming" test, and the intro test
(renamed `..._close_to_the_intro_ending`). Added one new test covering
both halves of the request directly -- blank early in a real intro AND
blank in the middle of a real mid-song solo -- plus a settings render-
kwargs test update for the new dict key. Full suite: 591 passed.

Re-verified against the real "Angie" song's actual timing data (first
line starts at 18.90s): scene text is empty at t=0, 2, and 13.9-15.4
(all more than 3s before the first line), then shows the normal
current+next preview starting at t=16.4 (2.5s before) through the line's
own start -- confirms the fix's real-world behavior directly, not just
the synthetic unit tests.

## 2026-09-15 — Fixed the "always extremely slow" launch: lazy song-list widgets

Owner reported the app opening to a black, unresponsive window -- twice in
one session, once genuinely waiting 3 minutes before it finally rendered.
Not a hang: measured `LyricVideoGUI(root)` construction directly against
the owner's real 65-song `work/` folder and found it took **28 seconds**
by itself (vs. 0.05s for `list_redoable_songs`/`list_rendered_songs`/
`list_pending_uploads` combined -- the filesystem scan was never the
problem). Root cause: `_build_widgets()` eagerly populated all three song
lists (Redo/Upload to YouTube/Pending Uploads) at startup regardless of
each section being closed by default (the same-day collapsible-section
fix) -- building a `CTkRadioButton`/`CTkCheckBox` row plus a Watch and a
Remove button (4 widgets each) for every song, across all three lists,
whether or not the owner ever opened any of them. CustomTkinter widget
construction itself is what's slow here, not I/O -- each widget does real
per-instance theming/image work.

Fix: `_make_collapsible_section()` gained an `on_first_expand` callback,
invoked once, only the first time that section is actually toggled open
-- never during `_build_widgets()`. The three `_populate_song_radio_list`/
`_refresh_pending_uploads_list` calls that used to run immediately after
building each section now run lazily through this instead; the (cheap)
`CTkScrollableFrame` container itself still builds immediately so nothing
about the layout changes, it just starts empty until first opened.
Re-measured the same way: `LyricVideoGUI(root)` init dropped from 28s to
**2.6s** (real launch on the owner's machine should drop from ~3 minutes
to well under 30s). First-time expansion of the Redo list (50 songs) still
takes ~7.6s to build its rows -- a real, unavoidable CustomTkinter cost --
but now it's paid only by someone who actually opens that list, not by
every single launch regardless of use.

Verified end-to-end with a throwaway script constructing a real
`LyricVideoGUI` against a real (shown) `ctk.CTk()` root: confirmed 0
children in `redo_list_frame`/`pending_uploads_list_frame` before any
expand, invoked each section's toggle button's `command` directly (the
same technique from earlier today's collapsible-section verification),
and confirmed the expected row counts (50, then 9) after. Full suite:
591 passed (no test changes needed -- this is a pure startup-timing fix
to GUI-construction code this project's tests don't exercise directly,
same as the collapsible-section work itself).

## 2026-09-15 — fetch_lyrics' sidecar lookup used the wrong audio path

Owner report: a sidecar `.lrc`/`.txt` placed next to the song's audio in
`work_dir` (matching the "Ky Anthem-D1jMg1K_7gQ.txt" filename the error
message itself named) was never found, no matter what. Root cause:
`run_pipeline()` copies the source audio into `work_dir` (existing
behavior, for `load_redo_inputs()`), but every later stage -- including
`fetch_lyric_lines()`, whose sidecar check is `path.with_suffix(...)` on
whatever `audio_path` it's handed -- kept using the ORIGINAL external
`audio_path` parameter, never switching to the copy. A sidecar dropped
next to the `work_dir` copy (the natural, error-message-suggested place)
was checking the wrong directory entirely.

Fix: right after the existing copy-or-skip block, `audio_path` is
reassigned to `audio_copy_path` whenever that copy exists (freshly made or
already there from a prior run) -- every subsequent stage, not just
`fetch_lyrics`, now consistently uses the `work_dir` copy. Also
incidentally hardens the "batch staging folder already emptied" case
`load_redo_inputs()` was already special-cased for (see its own
docstring) for a same-run stage resume, not just a later Redo.

New test asserts `fetch_lyric_lines()` is called with the `work_dir` copy
path, not an external original location, by constructing the audio file
in a separate `external/staging/` directory and capturing what path the
mocked call actually receives. Full suite: 592 passed.

## 2026-09-15 — The lazy-loading fix didn't cover refresh, just launch

Same-day follow-up to the "always extremely slow" launch fix. The owner
hit a real, live freeze again -- this time after uploading songs from the
Pending Uploads checklist -- and separately reported the Pending list
showing completely blank while open despite real pending songs still on
disk. Confirmed the blank-list report against the real `work/` folder
(`list_pending_uploads()` returned 9 real songs) before touching any code.

Root cause of the freeze: `_refresh_retry_upload_options()` (called after
every single upload finishes, `_on_retry_upload_done` -> here) rebuilt the
Upload-to-YouTube list (up to ~50 `CTkRadioButton` rows) AND the Pending
list directly and unconditionally -- the exact same expensive
CustomTkinter widget-construction cost the launch-time fix (earlier the
same day) addressed, just re-triggered by a different event, and NOT
gated on whether either section was even open. A closed list nobody was
looking at still paid the full rebuild cost on the main thread, freezing
the window for a stretch each time.

Fix: `_make_collapsible_section()` now returns `(content, invalidate)`.
`invalidate()` rebuilds immediately only if the section is currently
expanded; otherwise it just marks the content stale, deferring the real
rebuild to the next actual open (reusing the same `on_first_expand`/
`populated` machinery, refactored slightly to share a `populate_now()`
helper between the toggle and invalidate paths). `_refresh_retry_upload_options()`
now calls `self._invalidate_upload_list()` / `self._invalidate_pending_list()`
instead of rebuilding directly. `_refresh_song_list()` (used by the ✕
Remove button) is left calling the direct populate functions -- removing a
row is only ever possible from an already-open list, so there's no
closed-list case to guard there.

The blank-list report was never fully root-caused to a specific exception
(no matching traceback found in `.xsession-errors` for the relevant
timestamps -- the ones present there were stale `_mouse_wheel_all`
failures from testing the NOW-REMOVED nested-scrollable-frame layout
earlier that same day), but a Tkinter callback exception mid-rebuild would
produce exactly this symptom (some rows destroyed, an exception before the
rest get built, nothing printed anywhere the owner would see on a desktop
launch) and was already the suspected mechanism behind other silent
failures logged earlier today. Hardened defensively either way:
`_populate_song_radio_list()` and `_refresh_pending_uploads_list()` now
wrap each row's construction in its own try/except (one bad row logs a
warning and is skipped, never aborting the rest of the list silently), and
an empty list now shows a "(none)" label instead of bare empty space, so a
genuinely-empty list can never again be mistaken for a broken one.

New test: `test_refresh_retry_upload_options_invalidates_rather_than_rebuilds_directly`
(stub-based, asserts `_invalidate_upload_list`/`_invalidate_pending_list`
are called instead of a direct rebuild). Verified the actual mechanism
against a real `ctk.CTk()` root + real `LyricVideoGUI` instance
(`verify_invalidate.py`, same direct-command-invocation technique as
earlier collapsible-section verification): invalidating a CLOSED list
measured 0.000s (no rebuild at all); opening it for the first time
populated all 9 real pending songs; invalidating it again while OPEN
rebuilt immediately (3.79s, expected -- the section is actually visible).
Full suite: 593 passed.

## 2026-09-17 — Channel organization (playlists + engagement comments) shipped, then hit a real YouTube quota wall on first backfill run

Built and shipped the channel-organization feature (see
`docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md`
and the matching plan doc): every future upload now automatically gets
added to an All playlist, one playlist per listed artist, and a Genre
playlist Claude picks from a shared, growing list; a drafted engagement
comment queues in a new GUI panel for owner approval, same pattern as
comment replies. Ten TDD tasks, 634 tests passing, confirmed the exact
request-body shapes for `playlists.insert`/`playlistItems.insert`/
`commentThreads.insert` against Google's own current API docs before
writing any code against them (two of the three things the owner asked
about -- pinning a comment, and end-screen/card links between videos --
turned out to have zero API support at all, confirmed the same way, and
were scoped out entirely rather than half-built).

First real run of `scripts/backfill_channel_organization.py` against the
owner's 67 already-uploaded videos surfaced two real bugs immediately:

**Bug 1: missing `.env` load.** The script never called `load_dotenv()`
(every other entry point that needs `ANTHROPIC_API_KEY` does this via
`gui.py`'s `_check_api_keys`) -- every single song failed instantly with
an Anthropic auth error before any YouTube call was even attempted. Fixed
by loading `.env` at the top of `main()`, same as the app does.

**Bug 2: no quota handling.** Re-run after the fix worked for real --
14 playlists were actually created live on the channel (confirmed by
reading `~/.playalongvideoproduction/youtube_playlists.json` afterward)
-- but creating a playlist costs real API quota (50 units), and adding a
video to one costs another 50, so a ~50-song backfill each needing up to
three playlist creates plus three inserts blew through YouTube's default
10,000-unit daily cap partway through. The script kept going anyway,
reporting the identical 429 `RATE_LIMIT_EXCEEDED` error as a "FAILED" for
every remaining song -- 50 failures logged for what was really one root
cause. A few of the very first songs also hit a transient 404 on
`playlistItems.list` for a playlist that had just that moment been
created via `playlists.insert` -- YouTube's own eventual-consistency lag,
not a bug; a later retry (once quota resets) resolves it on its own since
`organize_video()` is idempotent either way. Fixed the script to catch
`HttpError` specifically, check `status_code == 429`, and stop the loop
immediately with a clear "re-run after quota resets" message instead of
grinding through the rest. Genre classification results already written
to each song's `song_info.json` before the failure are preserved (checked
directly: `08-anyhow`'s `song_info.json` already had `"genre": "Blues"`
after the failed run), so a later re-run never re-spends Claude calls on
songs it already got partway through.

Quota resets at midnight Pacific Time; the owner will re-run the backfill
script after that to pick up where it left off. Both fixes shipped as
part of the same session, pushed and released as part of the ongoing
v2.0.x line.

## 2026-09-17 — Multiple-times-a-day upload scheduling, replacing "N days between uploads"

Owner wants to lean into aggressive early-channel growth ("only 5
subscribers is the perfect time to get many videos on my channel") and
schedule up to 10 public uploads per day, at real chosen times (e.g.
9:30), rather than the old one-a-day cadence. Flagged once, briefly, that
this runs counter to the "cap at 1-3/day" advice in an analytics
write-up the owner had pasted earlier in the same session -- owner's
call to make, not something to block on, and the feature was built as
asked.

**Settings redesign**, arrived at over a few back-and-forth exchanges:
first proposed a single unified `youtube_upload_times` list (its length
implicitly the per-day count); owner's own framing ("say 5 a day and the
times for them") made clear they wanted an explicit count control too, so
landed on two fields instead -- `youtube_uploads_per_day` (int, 1-10) and
`youtube_upload_times` (str). The count field drives NOTHING at schedule
time; it only exists so the Settings panel can generate sensible default
times when the owner moves it. The real source of truth `schedule_upload()`
reads is just `youtube_upload_times`'s own parsed length -- avoids ever
having the two fields disagree.

`youtube_schedule.py` gained three small functions: `parse_upload_times()`
(comma-separated "H:MM" -> sorted, deduped `time` objects, tolerant of a
malformed entry, falls back to a single default rather than ever leaving
scheduling with zero slots), `format_upload_times()` (the inverse, for
writing the regenerated list back into the settings box), and
`evenly_spaced_upload_times(count)` (owner request: "put in time defaults
depending on the number per day" ... "during the day" -- spreads `count`
times evenly across a fixed 9 AM-9 PM window, both endpoints included;
count=1 keeps the app's original 3 PM default for exact backward
compatibility). `SettingsPanel._slider()` gained an optional
`on_value_change` callback (guarded by the same `_suppress_change` flag
`_changed()` already uses) so moving the new "Uploads per day" slider
regenerates the times box live -- verified directly against a real
`ctk.CTk()` root (this project's established GUI-verification technique,
no synthetic clicks): dragging the slider to 5 filled the box with
"09:00,12:00,15:00,18:00,21:00"; hand-editing one time afterward stuck
until the slider moved again (confirmed it then got overwritten, as
designed); loading a `Settings` object with pre-existing custom times
via `load_from()` did NOT trigger a regenerate (the exact class of bug
the `_suppress_change` guard exists to prevent elsewhere in this file).

**`compute_next_publish_slot()` rewrite.** The old version tracked one
claimed DATE per video and blocked `min_days_between` days around each.
First redesign attempt matched claimed slots by exact (date, hour,
minute) only -- passed every new test but broke an old one
(`test_schedule_upload_public_spaces_past_a_date_already_claimed...`,
which expects a same-day claim at a totally different, unrelated hour to
still push a single-slot-per-day config to tomorrow). Root cause: exact-
time matching lets an oddly-timed claim (a manual Studio upload, or a
leftover from before the owner last changed their configured times)
silently NOT count against that day's capacity at all. Fixed by
splitting the check in two: a day is "full" once it holds as many total
claims (any hour) as there are configured times, and only within a day
that still has room does exact-time matching decide WHICH specific
configured time to use (so an early manual publish still frees its exact
slot back up for the next upload, preserving the original gap-filling
behavior from the 2026-09-13 fix). Hand-traced this against every new
test before running any of them; every trace matched, and the full run
confirmed it on the first try -- including, notably, that the OLD
pre-existing `schedule_upload()`-level tests (written for the single-
time-per-day case, values just swapped to the new settings field) kept
passing unmodified in their assertions, which is exactly what backward
compatibility for the count=1 case should look like.

`youtube.reserved_publish_dates()` (returned bare `date`s) became
`reserved_publish_datetimes()` (returns full local-aware `datetime`s, so
two configured times on the same calendar day are tracked as distinct
slots) -- same live-channel-truth philosophy as before, just finer
grained. `date` became a dead import in `youtube.py` and was removed.

**Not migrated:** the owner's real, currently-saved `settings.json` still
has the old `youtube_min_days_between_uploads`/`youtube_preferred_upload_hour`
keys. `Settings.from_dict()` already silently drops unknown keys and
fills in defaults for missing ones (existing tolerant-load behavior, no
new code needed) -- so it loads fine, but lands on the new defaults
(1/day at 15:00) rather than carrying forward the old "1 day apart, 2 PM"
values. The owner needs to open Settings and set their real desired count
and times once after this update; nothing crashes or silently misbehaves
in the meantime.

Full suite: 645 passed.

## 2026-09-17 — Automatic quota-cooldown retry, after the real backfill run hit YouTube's daily cap live

Owner asked for this directly after watching the channel-organization
backfill script hit YouTube's real 429 quota wall twice in one morning:
"we need a way to do this in the program. Like a retry setting in hours
till it allows uploads again." With multiple-times-a-day scheduling
shipped the same day, quota exhaustion stops being a rare one-off and
becomes a real recurring operational condition, so this needed to be
automatic, not something the owner has to notice and manually retry.

New `youtube.is_quota_exceeded_error(exc)` -- `isinstance(exc, HttpError)
and exc.status_code == 429` -- gives every caller one shared way to
recognize this specific failure instead of treating all upload errors
the same. New `youtube_quota_state.py` (pure persistence, same shape as
every other small state file in this app) holds a single `blocked_until`
timestamp. `Settings.youtube_quota_retry_hours` (1-48 slider, default 24
-- long enough to always cross YouTube's actual midnight-Pacific reset
regardless of what time of day the block started, while still matching
the owner's own "a setting in hours" framing rather than computing the
exact reset instant) drives how far out that timestamp gets set.

Three integration points: `_maybe_upload_to_youtube` and
`_retry_pending_uploads` both check the cooldown up front (skip / raise a
clear message rather than attempting a call already known to fail) and
both save a fresh cooldown the moment `is_quota_exceeded_error()` matches
an actual failure -- `_retry_pending_uploads` additionally `break`s its
loop right there instead of grinding through every remaining pending song
reporting the identical root cause. The existing 20-minute
`_youtube_periodic_tick` (already refreshing comments and connect-status
in the background) gained a third job, `_retry_pending_uploads_if_due()`:
once the cooldown has passed, it automatically retries whatever's still
in the Pending Uploads list -- gated on auto-upload being on, nothing else
actively running (Generate/Redo/Batch), and honoring dismissed songs the
same way the Pending list's own display already does -- so a quota-
exhausted day now recovers on its own next time the app happens to be
open, with no owner action required.

Refactored `scripts/backfill_channel_organization.py` to call the new
shared `is_quota_exceeded_error()` too, dropping its own separate inline
`HttpError`/`status_code` check -- same detection logic, one fewer
place it could drift out of sync.

Full suite: 663 passed.

## 2026-09-18 — Lyric accuracy check + a "Flagged for Lyrics Review" queue, ahead of a 100-song batch run

Owner asked directly, with a 100-song Batch run about to start: "Can you
analyze each song for accuracy before uploading to YouTube... reject any
questionable and set aside for my review... if what you have is not
accurate, search for the accurate lyrics... Better yet guarantee lyric
accuracy, if not search for accurate lyrics, possible?" Answered honestly
that an absolute guarantee isn't realistic (no ground truth to check
against, only a plausibility read), but a real automated check plus a
bounded multi-source retry is. Owner confirmed: "Build both now... And
maybe, auto redo the song until the lyrics are correc but yes build
both... Max number of times, then send to me for review." Design doc:
`docs/superpowers/specs/2026-09-18-lyric-accuracy-check-design.md`.

New `lyric_accuracy.py`: `check_lyric_accuracy(anthropic_client, title,
artist, lyric_lines, model)` sends the fetched lines to Claude and parses
a labeled `LOOKS_ACCURATE:`/`CONCERN:` reply -- same
extract-text/parse-labeled-fields helper pattern used throughout this
codebase's other Claude-call modules, duplicated locally rather than
shared (matches this codebase's existing per-module-independence
convention).

`fetch_lyrics.py` gained `fetch_lyric_lines_verified()`: tries, in order,
every one of 6 real distinct sources -- the sidecar file, lrclib, then
each of the four `syncedlyrics` providers individually (`Musixmatch`,
`NetEase`, `Megalobiz`, `Genius`, passed one at a time via its own
`providers=[name]` kwarg rather than letting `syncedlyrics` pick) --
stopping at the first source whose lyrics pass
`check_lyric_accuracy()`. This IS the "max number of times" the owner
asked for: bounded by the fixed size of the source list (6), not a
separate configurable retry counter, matching the `_MAX_GENERATION_ATTEMPTS`
precedent for image generation (a fixed constant, not an owner-tunable
Settings field). If none of the 6 pass, the first candidate found is kept
(better than nothing) and its concern text is recorded rather than
raising or blocking the pipeline. The old single-shot `fetch_lyric_lines`
is unchanged, still used wherever an accuracy check isn't wanted.

`Song` gained `lyrics_source`/`lyrics_accuracy_concern` fields (blank =
unchecked/passed); `pipeline.py`'s fetch_lyrics stage now writes a small
dict (`{"lines", "source", "concern"}`) instead of a bare list to
`lyrics_timed.json`'s precursor file, and the align stage reads either
shape so a `lyrics_timed.json` written before this change still loads.
New `pipeline.list_flagged_songs()` mirrors `list_pending_uploads()`
(same directory walk, same "not yet uploaded" exclusion) but selects on
a non-empty `lyrics_accuracy_concern` instead.

`gui.py`: `_maybe_upload_to_youtube()` gained an up-front check --
`load_song(...).lyrics_accuracy_concern` non-empty skips the upload
entirely (any exception reading the file is swallowed, since a
missing/corrupt file must never block an otherwise-normal upload) --
and `_retry_pending_uploads_if_due()` excludes flagged songs from its
automatic retry the same way it already excludes dismissed ones. A new
"Flagged for Lyrics Review" panel (same lazy on-first-expand
`CTkScrollableFrame` pattern as every other song list in this app) shows
each flagged song's own concern text with two buttons: Redo (sets the
Redo dropdown to that song and calls the existing `_on_redo()` -- a
fresh redo re-fetches lyrics through the same verified path, so a clean
result this time clears the concern on its own, no separate "clear
flag" mechanism needed) and Upload Anyway (`_start_retry_upload([slug])`
-- a deliberate owner override, reusing the existing manual-upload path
verbatim).

Real gap found while wiring this in: `_run_batch_worker` never told the
GUI thread to refresh the Pending/Flagged/Upload lists after each song --
`_on_batch_done` only ran once, at the very end of the whole batch. With
a 100-song batch about to run and flagging meant to surface "after every
song finished" (the owner's own words), a song flagged mid-run wouldn't
have shown up in the panel until the entire batch finished. Fixed by
having `_run_batch_worker` put a `("batch_item_done", None)` message on
the queue right after each song's own upload attempt; `_poll_queue`'s
dispatch calls `_refresh_retry_upload_options()` on it, same as it
already does after a single Generate/Redo. `_on_batch_done` also calls it
once more as a safety net.

A real test-writing lesson while building this: the first version of
`test_maybe_upload_to_youtube_skips_a_song_flagged_for_lyrics_review`
used a `_must_not_run`-style mock that raises `AssertionError` if called
-- but `_maybe_upload_to_youtube`'s own outer `except Exception` silently
swallowed that assertion, so the test passed even before the real guard
was implemented (for the wrong reason, twice). Fixed by switching to a
plain `calls = []` list and asserting `calls == []` after the call,
which is robust regardless of what the code under test catches
internally.

Full suite: 687 passed.

## 2026-09-18 — earlyoom killed the app mid-batch overnight; fixed the underlying memory growth

Owner: "the program crashed during the night sometime, can you find out
why?" Investigation (systematic-debugging skill, not a guess): `dmesg`
showed no kernel OOM kill or segfault, and the system never rebooted
(`uptime` showed no gap) -- ruled out a hardware/kernel-level event.
`journalctl -u earlyoom --since yesterday` had the real answer:
`earlyoom` sent SIGTERM to the app's `python` process at 00:08:17, VmRSS
~10001 MiB, with system memory down to 2.46% available. Cross-referenced
against `work/`'s own file timestamps to identify which song: #24 of an
overnight 100-song Batch run, "Money" by Pink Floyd -- Demucs had just
finished separating stems, lyrics had just been fetched, and the crash
landed ~28 seconds later, right as the align stage's MMS_FA model
would have been loading.

The obvious first guess -- the already-known "align OOMs on 20+ minute
tracks" limitation ([[project_long_track_memory_limit]], no chunking
yet) -- didn't fit: "Money" is a completely ordinary 6:34 (394s), not a
long track. The real pattern was in earlyoom's own periodic memory-
percentage log lines: available memory declined steadily across the
*whole* overnight run (46% free at 11 PM -> 35% free at midnight ->
crash 7 minutes later), not a single spike tied to one song. Checked the
codebase for any per-song cleanup between Batch iterations -- there was
none: no `gc.collect()`, no explicit `del`, nothing, anywhere in
`pipeline.py`/`batch.py`/`gui.py`'s Batch loop. Every song in a Batch
run shares the same long-lived Python process (a background thread, not
a subprocess), so CPython's own reference counting frees most per-song
objects immediately, but (a) reference cycles -- which torch
tensors/models commonly form -- need an explicit `gc.collect()` to catch,
and (b) even fully-dead memory that Python has released back to its own
allocator doesn't necessarily return to the OS: glibc's malloc keeps
freed arenas around for reuse rather than handing them back via
`sbrk`/`munmap`. Over dozens of songs, RSS climbs even though nothing is
a genuine reference leak -- until an entirely ordinary song's own normal
peak usage (align, already the heaviest per-song stage) tips an
already-elevated baseline past what's left.

Fix: new `lyricvideo/batch.py:release_memory()` -- `gc.collect()`
unconditionally, then (Linux only, guarded by `sys.platform`) loads
`libc.so.6` via `ctypes` and calls `malloc_trim(0)` to force glibc to
actually hand freed arenas back to the OS. Wrapped in a bare `except
OSError: pass` since this is a best-effort hygiene step on a rare
libc variant, never something that should be allowed to crash a batch
over. Wired into `gui.py`'s `_run_batch_worker` in a `finally` block
around each song's own try/except -- runs after EVERY song, success or
failure alike, since a song that fails partway through (e.g. Demucs
succeeds, align then raises) can still have allocated real memory before
failing. Deliberately scoped to Batch only (not Generate/Redo, which
don't run dozens of songs back-to-back in one process) per the owner's
own framing of the ask ("the underlying memory growth" in a long Batch
run).

Tests: `release_memory()` itself tested by monkeypatching `gc.collect`
and `ctypes.CDLL` (a fake libc object records the `malloc_trim(0)`
call) -- one test per platform branch, plus one confirming a missing/
unusual libc is swallowed rather than raised. `_run_batch_worker`
wiring tested directly against `LyricVideoGUI` with a stub `self`
(`_gui_stub()`) and a fake `run_pipeline` that raises for one item of
two -- asserts `release_memory` fires for both the succeeding and the
failing item.

Full suite: 691 passed.

## 2026-09-18 — Quota-exceeded halts every YouTube call, plus a real daily upload cap

Owner concern, raised directly: "im a little worried the massive
'exceeded quota' from youtube might see it as an attack, so when the
quota has been exceeded, everything, including comment checks, all
that needs to be stopped until the program sees the exceeded quota has
been lifted." The existing 2026-09-17 automatic-quota-cooldown-retry
cooldown (see that dated entry above) only covered the two upload
paths -- `_check_youtube_comments_worker`
(comment scanning), `_youtube_status_text` (the connect-status
refresh, via `get_channel_title`), and `organize_video()` (playlist/
engagement-comment calls) all kept firing on the unchanged 20-minute
tick even during a quota outage, each capable of producing its own
fresh 429 across every song/video in `work/`.

Design discussion (brainstorming skill, bounded path) settled two
separate questions. First: should the halt also block deliberate
manual actions (Connect, Upload, Check Now, Approve)? Owner's answer:
"block everything with a popup window showing quota exceeded..
continue anyways?" -- landing on a hybrid, not a blanket block:
automatic triggers (the periodic tick) go fully silent during a
cooldown, while a manual click gets `_confirm_quota_override_if_blocked()`,
an `askyesno` naming the retry time, and only proceeds on an explicit
Yes. `_on_connect_youtube` itself was deliberately left out of this
gate -- `youtube_auth.connect()` is pure OAuth (`InstalledAppFlow`),
never a Data API call, so there's nothing there to protect; the status
refresh that follows a successful connect already self-gates.

Second: `_youtube_status_text()` (called at launch, post-connect, and
every periodic tick) used to always call `get_channel_title()`. Rather
than special-case every caller, the cooldown check moved inside the
function itself, before the real call -- one change covers all three
call sites for free and keeps the status label informative ("YouTube:
quota exceeded, retrying after <time>") instead of going stale.
`_check_youtube_comments_worker`'s existing per-video isolation
(2026-09-10, one video's `commentsDisabled` must never block the rest)
got one more branch: a 429 specifically is NOT like other per-video
failures -- every remaining video would fail identically right now, so
it `break`s the whole scan and engages the same global cooldown,
rather than logging the identical warning once per remaining video.

Mid-conversation, the owner pivoted to a second, related but
independent ask: "i think i want a limit on how many songs i upload a
day, so i dont run out of quota... in the settings, number of uploads
per day" -- then clarified this meant the PROGRAM's automated upload
volume, not a count of manual clicks, after noticing "right now if all
are selected, it tryes to [upload] them all." Investigation found
`Settings.youtube_uploads_per_day` already existed but, per its own
code comment, "drives nothing at schedule time" -- it only seeded
`evenly_spaced_upload_times()`'s defaults for the Settings panel.
Each `videos.insert` call costs ~1600 of YouTube's 10,000-unit default
daily quota, so an unthrottled "Select All" on a large Pending Uploads
list could exhaust an entire day's quota in one run.

New `youtube_upload_count_state.py` gives `youtube_uploads_per_day` a
second, real job: a local `{date, count}` counter
(`~/.playalongvideoproduction/youtube_upload_count.json`) of this
app's own successful `upload_video()` calls, rolling over at the local
day boundary. Explicitly verified this doesn't repeat the 2026-09-13
publish-slot-counter mistake (documented earlier in this file, under
the multiple-times-a-day-scheduling and channel-organization entries):
that counter broke because it tried to *predict* a schedule the
channel itself could also mutate (a manual
publish, a Studio edit); this one only records a fact -- how many
times this app itself called upload -- that nothing outside the app
can invalidate. `_uploads_remaining_today()` gates both
`_maybe_upload_to_youtube` and the loop inside `_retry_pending_uploads`;
once exhausted, remaining slugs land in a new `results["deferred"]`
list rather than all being attempted.

Asked directly whether the cap should also throttle YouTube's
"Scheduled" backlog (uploading more per day than there are
`youtube_upload_times` slots spreads publish dates further into the
future every day) -- owner: "no i dont want a cap on scheduled
backlog. unless youtube has one" and "keep it independent. not hard to
match uploads and what goes public." The upload cap and publish-time
pacing stay fully separate features on purpose: raising one doesn't
require raising the other.

Existing tests asserting an exact `{"succeeded": ..., "failed": ...}`
dict from `_retry_pending_uploads` needed a `"deferred": []` key added
throughout; a new autouse fixture (`_no_upload_cap_by_default`, mirroring
the existing `_no_quota_block_by_default`) isolates every gui test from
the real `youtube_upload_count.json` on disk, and `_gui_stub()` grew a
default `_confirm_quota_override_if_blocked=lambda: True` so every
pre-existing manual-action test keeps behaving exactly as before this
gate was added.

Full suite: 720 passed.

## 2026-09-18 — Playlist propagation race: a freshly-created playlist 404'd on the very next call

Owner saw a live error while testing: `HttpError 404 playlistNotFound`
on a `playlistItems` call, and correctly guessed the cause unprompted:
"i bet its a playlist that hasnt been created yet." Confirmed against
`youtube_playlists.py`: `get_or_create_playlist()` calls
`create_playlist()` (`playlists().insert()`), and `organize_video()`
immediately calls `add_video_to_playlist()` (`playlistItems().insert()`)
on that brand-new id in the very next line -- a known Google API
eventual-consistency lag, where a resource just created can still 404
for a moment before it's fully queryable elsewhere. Most likely to hit
on the first video for a brand-new artist/genre playlist, or a
channel's first-ever organized upload -- exactly when a playlist is
guaranteed to have JUST been created rather than reused from cache.

Fix: new `_add_video_to_playlist_with_retry()` wraps the three
`add_video_to_playlist()` call sites in `organize_video()` (All,
per-artist, Genre), retrying up to 3 times with a 2-second delay
specifically for a 404, using the same `HttpError`/`status_code`
detection style as `is_quota_exceeded_error`. Anything else --
including a persistent 404 that never resolves, or an unrelated error
like a quota-exceeded 429 -- still raises immediately, since waiting
would never fix a genuine failure and a 429 needs to reach gui.py's
quota-halt logic (2026-09-18, above) without delay.

Tests: three new cases in `test_youtube_playlists.py` (transient 404
resolves by the 3rd attempt; a persistent 404 still raises after
exhausting retries; a 429 propagates immediately with zero sleep
calls), monkeypatching `time.sleep` so none of them actually wait.

Full suite: 723 passed.

## 2026-09-18 — Daily upload cap split into its own field, undoing a reuse of youtube_uploads_per_day

Earlier the same day, the new daily upload cap (previous entry above)
was wired onto the EXISTING `Settings.youtube_uploads_per_day` field,
reasoning that it already meant "uploads per day" and already had a
Settings-panel slider. That slider's `on_value_change` callback
(`_regenerate_upload_times`, from 2026-09-17) auto-overwrites
`youtube_upload_times` with N evenly-spaced defaults every time it
moves -- fine when the slider only meant "how many time slots you
want," but now that the same slider ALSO meant "the raw upload
ceiling," moving it to protect quota (e.g. up to 7) silently destroyed
an intentionally different publish-schedule slot count (e.g. 5),
exactly contradicting the owner's own explicit "keep it independent"
instruction from the original design conversation.

Owner caught this live while testing: "the schedule is gone for how
the uploads are scheduled.. say i want 7 uploads a day, but only want 5
scheduled for public a day. i dont have that" -- then, after a first
attempted fix (decoupling the slider from `_regenerate_upload_times`
entirely) changed the existing scheduling UI's behavior: "worked good
before.. all i want added was the upload cap per day, everything else
the same.. i dont know why you fucked with that part."

Correct fix: leave `youtube_uploads_per_day` and its slider (still
wired to `_regenerate_upload_times`) completely untouched, exactly as
they were before this whole feature started. Add a brand new, fully
independent `Settings.youtube_max_uploads_per_day` (default 5, own
slider "Daily upload cap", no `on_value_change`) as the real ceiling
instead. `gui.py`'s `_uploads_remaining_today()` now reads this new
field; `_maybe_upload_to_youtube`/`_retry_pending_uploads` are otherwise
unchanged from the earlier entry's design. Renamed throughout
`youtube_upload_count_state.py`'s docstring, `youtube_schedule.py`'s
module docstring, and every test that constructed
`Settings(youtube_uploads_per_day=N)` for a cap scenario (now
`youtube_max_uploads_per_day=N`).

Lesson: reusing an existing field because it "already has the right
name" isn't free when that field already drives other behavior --
should have asked whether a new field was warranted before wiring the
new cap onto it, rather than discovering the conflict live in the
owner's own testing session.

## 2026-09-18 — Approving an engagement comment failed with a raw 403 on a still-scheduled video

Owner clicked Approve on a drafted "Born to Run" engagement comment (the
Channel Organization feature's own queue) and got a modal HttpError dump:
`commentThreads.insert` returning 403, reason `forbidden`, "The comment
thread could not be created due to insufficient permissions."

Diagnosis ruled out the usual scope/token suspects with real checks rather
than guesses: the stored OAuth token's `scopes` field matched
`youtube.force-ssl` exactly (the only scope the app ever requests), wasn't
expired, and the very same credentials already worked for uploads and
`commentThreads.list`. Querying the actual video via the app's own
connected client (`videos().list(part="status", ...)`) showed the real
cause: `status.privacyStatus: "private"` with `publishAt:
"2026-09-19T15:00:00Z"` -- the video was still a day away from actually
going public. `is_video_public()` (`youtube.py`) already exists and is
already used to skip comment *reads* on a still-scheduled video, with a
docstring explicitly warning that such a video "never accepts comment
reads" -- YouTube applies the identical restriction to comment *writes*,
but `_on_approve_comment` (`gui.py`) never called that guard before
posting.

Fix: `_on_approve_comment`'s worker now calls `is_video_public()` first and
shows a plain "still scheduled/private, try again after it publishes" info
dialog instead of attempting the post -- mirroring the guard the
comment-reading path already had. `tests/test_gui.py` gained a case
asserting Approve does not call `post_top_level_comment` when the video
isn't public yet, and the existing happy-path/failure-path approve-comment
tests were updated to mock `is_video_public` returning `True` (a direct,
un-mocked call against the fake test client would otherwise raise).

Lesson: `is_video_public()`'s own docstring already said the restriction
applies to comment reads; the same file's `post_top_level_comment` should
have been guarded the same way from the start rather than only being
caught live once a real scheduled upload reached the queue.

## 2026-09-18 — CLAUDE.md staleness audit (queued 2026-09-15, escalated 2026-09-18)

Doug asked for a real pruning pass over `CLAUDE.md` on 2026-09-15 during an
unrelated bug fix; it kept getting deferred because in-session pressure was
always just "shave a few bytes off the paragraph I'm already editing" rather
than a real look at the rest of the file. By 2026-09-18 the file was sitting
at 39966/40000 bytes before any edit that day, and every commit needed a
byte-fight just to land a one-line pointer.

Forced by hitting the wall again on an unrelated engagement-comment bug fix
(previous entry above), the audit finally happened. Found and fixed:
- The YouTube-fields enumeration under "Update Available Feature" said
  `Settings` "gained seven YouTube fields" and listed eight of them by
  name -- `settings.py` actually has nine now (`youtube_quota_retry_hours`,
  added 2026-09-17 for quota-cooldown retry, was never added to the list).
  Replaced the exhaustive enumeration with a pointer at `settings.py`'s own
  YouTube block, so this can't go stale again the same way.
- The Channel Organization section's closing paragraph claimed the
  interactive OAuth `connect()` flow and live comment/engagement-comment
  posting were "NOT yet verified" -- false as of this same session, which
  exercised both against the real connected channel (see the entry above).
- A handful of parenthetical `(HISTORY date: full incident narrative)` asides
  scattered through the render-stage description were trimmed to plain
  `(HISTORY date)` pointers -- the narrative already lives in this file's own
  dated entries; repeating it in CLAUDE.md was the "narrative snuck into
  CLAUDE.md instead of the history file" pattern the queued audit was meant
  to catch.

Net effect: CLAUDE.md freed real headroom instead of staying pinned at the
ceiling. Didn't attempt a full line-by-line rewrite of the (very long,
genuinely load-bearing) pipeline-stage mechanism descriptions -- those are
current and accurate, just dense; a deeper pass is still worth doing in a
future session with more time, per the byte trend re-approaching the
ceiling.

Full suite: 723 passed.

## 2026-09-18 — A failed image generation could write a plain color to disk before the fix-up pass ever ran

Owner spotted a log line during the 100-song batch: `WARNING: falling back
to a plain-color background for line 183d42ece37968be ... substitute_
fallback_images() will replace this ... unless every single one of them
failed`, for a "Life in the Fast Lane" lyric ("They had one thing in
common, they were good in bed") -- almost certainly rejected by Replicate's
content-safety filter, which fails identically on every retry (not a
transient hiccup `_MAX_GENERATION_ATTEMPTS` retries would ever get past).

Traced it: the image DID get fixed by `substitute_fallback_images()` before
that song's render (confirmed by reading the actual final PNG off disk --
not a flat color), so this specific case never reached the finished video.
But when asked "what kind of logic is that" -- write a flat color to disk
at all when the song already has other real, usable images sitting right
there -- the honest answer was: `get_or_generate_image()` only sees images
generated strictly before it in `pipeline.py`'s per-line loop, so at the
moment of failure it can't yet know the true chronologically-nearest
neighbor (which might be a later line, not yet generated); the two-phase
design deferred to a full post-pass specifically to get a better match.

Owner's response: correctness of the *eventual* placement doesn't matter if
a plain color can reach a render at all -- "of course not, it ruins the
video... just use the previous one." Fair: an immediate, slightly-less-
optimal substitution that guarantees no flat color is ever written beats a
theoretically-better one that depends on a second pass always completing.

Fix: `get_or_generate_image()` (`imagery.py`) gained a `previous_image`
parameter -- on total failure, if a previous real image exists, its bytes
are copied in immediately and a "reusing the previous image" warning is
logged; the plain-color path is now reachable only for a song's very first
image (nothing real exists yet to reuse). `pipeline.py`'s images stage
threads a `last_real_image` variable through both the sung-line loop and
the instrumental-caption loop, updating it via `is_fallback_image()`
after each call and passing it as `previous_image` to the next one.
`substitute_fallback_images()` is untouched and still runs afterward --
it can still improve on an immediate same-image reuse with a
chronologically closer neighbor once the whole song's images are known,
but a flat color reaching a render is no longer possible in the common
case (any failure after at least one real image already exists).

Two new tests in `test_imagery.py` cover both branches (reuses a real
`previous_image` when given one; still falls back to plain color when the
given `previous_image` doesn't exist, i.e. no predecessor yet). Full
suite: 726 passed.

## 2026-09-18 — An interrupted Batch item redid Demucs from scratch on the next Start Batch

Told the owner that restarting the app mid-Batch would kill the pipeline
partway through "with no way to resume" (documented, existing behavior).
Owner's reply left no ambiguity: "that needs to be fix.. that no resume..
of course i want to resume the last one that failed along with all the
rest."

Checked what actually happens today: `resolve_batch_items()` (`batch.py`)
only tracks `already_done` (does the final mp4 exist). Restarting "Start
Batch" on the same folder already naturally skips fully-done songs and
continues through the remaining ones -- that part already worked. But the
one song that was mid-pipeline when the app died is never `already_done`
(no mp4 yet), so `_run_batch_worker` (`gui.py`) called `run_pipeline()`
with no `start_stage` for it, meaning "start over from `identify`" --
silently redoing `separate` (Demucs), the single slowest, most
expensive stage in the entire pipeline, even when its output was already
sitting on disk untouched.

`run_pipeline()` already fully supports resuming from any later stage
via disk artifacts (CLI `--stage`, and the GUI's Redo/already-done-batch
paths already use `start_stage="fetch_lyrics"` deliberately) -- the gap
was purely that a freshly-interrupted, never-completed item never got
offered that same resumption.

Fix: `BatchItem` gained a `resume_stage` field (default `"identify"`).
`resolve_batch_items()` sets it to `"fetch_lyrics"` when both
`htdemucs/<audio_stem>/{vocals,no_vocals}.wav` already exist for that
item -- the exact same path convention `run_pipeline()` itself uses, so
this can never disagree with what the pipeline would independently
discover. `_run_batch_worker`'s not-already-done branch now passes
`start_stage=item.resume_stage` instead of always defaulting to
`"identify"`. No change to the already-done branch (still backs up and
resumes at `fetch_lyrics` unconditionally, as intended for a deliberate
full regenerate).

Practical effect: closing the app (or a crash) mid-Batch is now safe to
just restart from -- click Start Batch on the same folder, choose "skip
already-done," and the interrupted song resumes past Demucs instead of
paying for it twice, while every other remaining song proceeds normally.

Three new tests (`test_batch.py` x2, `test_gui.py` x1) cover: resume_stage
is "fetch_lyrics" when both stem files exist, stays "identify" when only
one exists (separate() itself interrupted partway), and
`_run_batch_worker` actually threads `resume_stage` through to
`run_pipeline`'s `start_stage` per item. Full suite: 729 passed.

## 2026-09-19 — A Batch kept retrying uploads after YouTube said the upload limit was hit

Symptom (owner's live log): after one song failed with `ResumableUploadError: <HttpError 400 ... "The user
has exceeded the number of videos they may upload." ... 'reason': 'uploadLimitExceeded'>`, the NEXT songs in
the same Batch each attempted (and failed) their own upload, even though the 2026-09-18 quota-cooldown and
daily-cap guards exist specifically to stop that.

Root cause: both guards were keyed on things this error never triggers. `is_quota_exceeded_error()` (the one
predicate every cooldown-recording call site in `gui.py` routes through) only matched HTTP **429**; YouTube
signals its per-channel upload ceiling as HTTP **400** + reason `uploadLimitExceeded` (confirmed against
the official `videos.insert` docs -- a distinct error from the 429 API quota). Unrecognized, it fell into the
generic "upload failed" warning branch: no `save_quota_blocked_until()`, so the next song's
`load_quota_blocked_until()` check saw nothing. The daily cap (`youtube_max_uploads_per_day`) didn't help
either: `record_upload()` only counts *successful* uploads, so failed attempts never advance it (by design --
it's an owner-side ceiling on successes, not a reaction to YouTube refusing).

Why no test caught it: every `test_gui.py` quota test stubs `is_quota_exceeded_error` to `lambda e: True`,
and `test_youtube.py` only tried a 429 and a 404 -- the real predicate was never run against the real error
shape.

Fix: `is_quota_exceeded_error()` now also returns True for a 400 whose `error_details` carry reason
`uploadLimitExceeded` (a 400 with any other reason is still False). Every existing call site therefore starts
recording the cooldown (`youtube_quota_retry_hours`) and halting, with no per-site change. Side effect worth
knowing: the cooldown is global (by the 2026-09-18 "halt all YouTube API activity" design), so hitting the
upload limit also pauses comment scanning/playlist organizing for that window, not only uploads.

Tests: two predicate tests in `test_youtube.py` (real `ResumableUploadError` shape -> True; 400 with another
reason -> False), plus `test_gui.py::test_maybe_upload_to_youtube_stops_retrying_after_a_real_upload_limit_
exceeded_error`, which uses the real error AND the real predicate across two consecutive songs and asserts the
second never attempts an upload (verified to fail without the fix). Also fixed an unrelated time bomb found
by the full run: `test_youtube_status_text_skips_the_api_call_while_quota_blocked` hardcoded
`blocked_until = 2026-09-19 06:00 UTC`, which stopped being "in the future" that morning -- now relative.
Full suite: 732 passed.

## 2026-09-19 — YouTube Settings: two clearly named sliders, right defaults, and early-publish frees a slot

Owner's intended model, stated plainly after a round of confusing labels:
- **Maximum publish per day** (`youtube_uploads_per_day`, default 5): a slider that auto-fills the
  **Scheduled publish times** box with that many evenly spaced times (9:00-21:00). The box's own length is
  what `compute_next_publish_slot()` schedules against.
- **Maximum uploads per day** (`youtube_max_uploads_per_day`, default 7): the real cap on upload calls per
  calendar day (`_uploads_remaining_today`); with 20 songs ready, 7 upload today and the rest wait for a later
  day (picked up by the 20-minute tick / next Batch song). Publish scheduling is separate: uploads spill
  onto later days' slots, 5 a day.
Field names are unchanged (only panel labels and defaults), so existing `settings.json` files still load.
Default `youtube_upload_times` is now the five-time list so it agrees with the slider's default
(`test_default_publish_times_match_the_default_maximum_publish_per_day`). An earlier same-day change removed
the publish-times slider (mistakenly, it did have a purpose) and it is restored here.

Verified with tests: 7 uploads at 5 publish times -> 5 today + 2 tomorrow; 20 pending with cap 7 -> exactly 7
upload, 13 deferred.

Bug found while verifying "publishing a scheduled video by hand opens a slot": that worked when the video
was scheduled for a LATER DAY (its old slot frees), but NOT for one scheduled for later TODAY -- the early
publish counts as a claim at its real publish time (an off-slot moment like 10:30), which filled one of
today's five capacity counts, so today looked full and the freed 12:00 slot was never reused (next upload
went to tomorrow). Fix: `compute_next_publish_slot()` ignores a claim that has already happened AND sits at
a non-slot minute; a still-scheduled odd-hour video (future claim) still counts against its day, unchanged.

## 2026-09-19 — Lyrics are now checked against the audio (Whisper), not just against a title

Symptom (owner): some finished videos show lyrics that are not the right lyrics in the right order for the
recording ("not one of these songs went to my review"). Root cause: nothing ever compared the lyric TEXT to the
AUDIO. `align.py` squeezes any text onto the vocals without error, and `lyric_accuracy.py` only asks Claude if
the text looks like real lyrics for that title -- and `fetch_lyric_lines_verified()` stopped at the first source
that passed (50 of 53 checked songs stopped at lrclib; 0 of 139 songs were ever flagged; 86 predate the check).
Design: `docs/superpowers/specs/2026-09-19-verify-lyrics-against-audio-design.md`.

What was built: `transcribe.py` (faster-whisper, cached `work_dir/transcript.json`), `lyric_audio_match.py`
(pure scoring), `fetch_lyric_lines_verified(audio_check=...)` (a source passes only if it matches what is
SUNG; none passing keeps the best match, flagged, with the unmatched line numbers in the concern), and
`pipeline._build_audio_check` (falls back to the old Claude text check if Whisper can't run). Flagged songs use
the existing machinery: no auto-upload, listed in "Flagged for Lyrics Review". `faster-whisper` added to
`requirements.txt` (dry-run: it changed none of the TensorFlow/crema pins).

Real findings, each pinned by a regression test (measured on the owner's songs, not assumed):
1. Whisper's speech VAD deleted singing ("Like a Prayer": 61 words of ~660 -> correct lyrics scored 8%). VAD off,
   `temperature=0` (also ~5x faster).
2. Language auto-detect heard "Billie Jean" as Portuguese (19%). Forced English (`LYRICVIDEO_WHISPER_LANGUAGE`).
3. `small` looped on loud rock ("wild, wild, wild") and scored 19-62% on correct lyrics; `medium` 53-78%.
   `medium` is the default (~70 s a song on this 4-core, no-GPU machine; `LYRICVIDEO_WHISPER_MODEL`).
4. Common words match by chance (a different song "matched" 35%): scored on content words only.
5. Parenthesized backing vocals / "whoa" are optional: not required to be heard, but count as support when
   heard ("Every Breath You Take": ignoring them made a 26-line section one unsupported run), and they still
   explain what was sung. A fade-out vamp (3+ identical lines) is excused if that phrase matched elsewhere.
6. A sung verse missing from the file is invisible from the lyric side, so the check also measures the longest run
   of sung content words no lyric line explains (Whisper loops are trimmed to two copies for that measure only).

Thresholds (`lyric_audio_match.py`): coverage >= 70%, no run of > 3 unmatched lines, no > 12 unexplained sung
words. Controlled corruption of 8 real songs that pass untouched (0 wrongly rejected): lines from another song
8/8 (4 or 6 lines), a 6-line verse missing 8/8, words changed 7/8 (6 lines) and 6/8 (4 lines), verse and chorus
swapped 4/8, a 4-line verse missing 3/8. Real songs, first 18 in the folder: 12 pass, 6 held for review
(every-breath-you-take, 08-anyhow, all-along-the-watchtower, all-i-wanna-do-is-make-love-to-you,
back-in-the-saddle, beat-it) -- each is either a real mismatch or a recording Whisper can't hear (08-anyhow:
"oh oh oh" loops for a minute), and only a listen can tell which.

Limits: this cannot certify word-for-word perfection; it catches wrong editions, wrong/missing/out-of-order
sections of roughly 5+ lines, and holds anything it cannot confirm. A displaced chorus inside a run of identical
choruses, and a 1-2 line error, are not detectable. Existing songs were NOT re-checked retroactively.

## 2026-09-19 (later) — Re-checking existing songs, and why an AI "repair" can only be a suggestion

Two follow-ups to the Whisper lyric check. (Owner: "yes both".)

**1. `lyricvideo/verify_lyrics.py`** -- `python -m lyricvideo.verify_lyrics [--flag] [--report FILE]` re-checks every
finished song's saved lyrics against its vocal stem (86 of the songs predate any check). Report-only by default;
`--flag` writes a concern onto a not-yet-uploaded song's `lyrics_timed.json` (so it skips auto-upload and shows in
"Flagged for Lyrics Review"). Never overwrites an existing concern, never touches an uploaded song (reported as
`mismatch-uploaded`), one broken song never stops the run, results append to the report as it goes and a re-run
resumes. Each song's transcript is cached in its own work folder, so a later Redo reuses it. Run over the owner's
folder in the background with `--flag`; the durable result is the flags themselves.

**2. `lyricvideo/lyric_reconcile.py`** -- Claude is shown the numbered lyrics + the transcript and returns small EDITS
(replace/insert/delete), never lyrics from memory; code validates each edit (new text must come from the
transcript, supported lines can't be touched, overlaps dropped, applied bottom-up). Result: **it works mechanically and is
unsafe.** On four real held songs the repair raised the audio match (Every Breath You Take 80->91%, Watchtower
65->79%, Back in the Saddle 53->87%) by replacing CORRECT lyrics with Whisper's fluent mishearings ("No reason to
get excited / The thief, he kindly spoke" became "I'm going to sing a song"; "Crazy horse saloon" section became
"I'm sorry / I'm sorry"). Text built from the recognizer's words matches the recognizer by construction, so
"passes the audio check" proves nothing for a repair, and Whisper's own per-segment confidence (avg_logprob,
compression ratio) did not separate its good segments from its bad (a wrong segment scored as confidently as a
right one). So the repair is SUGGESTION-ONLY: `pipeline._build_reconcile` saves it as `work_dir/lyrics_suggested.txt`,
`fetch_lyric_lines_verified` mentions it in the concern ("A possible fix (...) ... NOT applied"), and the video's lyrics
are never changed. Also found: with `max_tokens=2000`, Sonnet 5 (thinking on by default) spent the whole budget
reasoning and returned no text; the request now uses `max_tokens=16000` and `output_config={"effort": "medium"}`.

Consequence to remember: a genuinely accurate lyric file cannot be generated from Whisper's output on loud/produced
recordings; the reliable fixes are a better lyrics source or a sidecar `.lrc`/`.txt` the owner supplies (which the
audio check then verifies).

## 2026-09-19 (evening) — AI judge, upload hold, and the replace-on-YouTube report

Owner asks, in order: (1) "AI should be able to take the whisper file plus the lyrics file and figure out the
proper lyrics"; (2) stop uploads until the videos are analyzed; (3) check the older songs too, including which
need replacing on YouTube.

**AI as JUDGE, not copyist (`lyric_arbiter.py`).** Copying Whisper's words into the lyrics measurably made them
worse (see the entry above). Judging works: Claude is shown the numbered lyric file + transcript + the unmatched
stretches (and the text of any sung-but-unexplained words) and returns a verdict per stretch --
`recognizer_error` (lyrics coherent, transcript garbled/looping/a phonetic mishearing), `lyrics_wrong`
(transcript clearly holds different coherent lyrics) or `unsure` -- plus whether a sung section is missing. It writes
no lyrics. `confirmed` only if EVERY stretch is a recognizer error and no section is missing; forgotten/unsure/
unparseable stays held. Measured on real data: songs whose lyrics I believe right but Whisper can't hear -> 3 of 4
confirmed (the 4th, Beat It, was held over a possibly-missing section); 8 of 8 deliberately corrupted songs (another
song's lines, or a verse removed) were NOT confirmed, with the wrong range pinpointed. `fetch_lyric_lines_verified(
arbiter=...)` accepts a confirmed source as `<source>+ai-confirmed` (concern ""), and adds the judge's reasons to
the held note otherwise; `pipeline._build_arbiter` wires it (only when Whisper ran). Sonnet 5 request: max_tokens
16000, `output_config={"effort": "medium"}` (thinking is on by default and ate a 2000-token budget).

**Upload hold (`verify_lyrics.UNCHECKED_HOLD`, `--hold-pending`).** A non-empty concern already keeps a song out of
both auto-upload paths and the running app re-reads it from disk each time, so writing a hold marker onto every
waiting, unchecked song stops uploads NOW with no restart (39 songs held; 4 already-verified stayed free). The
analysis processes held songs first and releases each one that verifies (concern cleared) or replaces the hold with
the real reason. New songs need the app restarted to run the in-pipeline check.

**Older songs (`verify_lyrics`).** Uploaded songs that fail are now flagged too (`flagged-uploaded`; they are not on
the pending review list, which is for songs not yet uploaded). `--recheck-flagged` re-judges flags the audio check
alone wrote (recognized by describe_mismatch's phrases; a concern from the older Claude text check is never touched).

**`replace_report.py` (read-only).** Lists uploaded songs still carrying a concern, grouped: still scheduled (not yet
public, easiest to swap) / already public (most viewed first) / no longer on YouTube, with links. Respects the quota
cooldown (no YouTube calls while it is active). It never deletes, edits or uploads: replacing a video needs correct
lyrics first, which the check cannot reliably produce, and deleting is the owner's per-song call.

## 2026-09-19 (night) — First monitored Batch item ("Night Moves") found three real bugs

The owner had every step of the first Batch item logged (song #66, "Night Moves", Bob Seger; the log, every model
call and frames from the video are in `~/night-moves-run-log/`). The video came out wrong despite being an easy song:

1. **Provider credit lines were shown as lyrics.** NetEase's lyrics began `作曲 : Bob Seger` / `作词 : Bob Seger`
   ("Composer:" / "Lyricist:"). The aligner stretched the first across the whole 17 s intro and the font (no CJK)
   drew empty boxes; a stray full-width "（" at a line's end drew a box too. `fetch_lyrics._clean_lyric_lines` now drops
   credit lines (a credit word followed by a colon, ASCII or full-width, so "Written by the wind" is kept) and
   NFKC-normalises full-width punctuation, dropping a trailing unclosed "(".
2. **The "best" source was chosen by coverage alone.** NetEase (a shorter 40-line version omitting a section, 38
   unexplained sung words, coverage 0.82) beat lrclib (the complete 63 lines, failing only a 5-line run, coverage
   0.78). `lyric_audio_match.audio_match_badness` scores how far each candidate is past EACH limit (coverage, unmatched
   run, unexplained words) and the least-bad source is kept.
3. **The AI judge called a correct line wrong** because Whisper had skipped it ("the transcript lacks the weren't-in-love
   clause"), and the suggested fix would have deleted three real lines. Both prompts now say a line merely missing from
   the transcript, or merged into a neighbour, is not evidence the lyric is wrong (`lyrics_wrong` needs transcript words that
   CONTRADICT the file's). Re-checked on real data: all 8 deliberately corrupted songs are still rejected; the judge's
   answers on right-lyrics/hard-to-hear songs vary between runs (3/4 then 2/4 confirmed) and the misses err toward holding.

Re-run of Night Moves with the fixes: lrclib chosen, judge confirms every unmatched stretch as a recognizer error,
accepted (`lrclib+ai-confirmed`), no credit lines. Other observations (not bugs): Replicate returned transient 503s while
polling (handled) and one NSFW false positive on an innocent prompt (retried).

## 2026-09-19 (late) — "Ironic": a damaged audio file made a broken video that was uploaded

Picking the next song to redo (worst timing) found `ironic`: aligned line times 0.1 s..43 s for a 230 s song, vocal
and instrumental stems only 43 s. Re-running Demucs gave 43 s again: the source `10 Ironic.m4a` is a PARTIAL file
(4.2 MB for 3:49 of ALAC; ffmpeg: "partial file", "Packet corrupt") that the app accepted because its tags claim 3:49.
Consequences: the aligner squeezed all 42 lines into the first 43 s, chords covered only 43 s, and the rendered video's
audio is real music for ~40 s then a constant loud signal (identical RMS in every 10 s window) for ~3 minutes -- and that
video (`Lcft28xNVIs`) was uploaded 2026-09-16. Scan of all 141 songs: Ironic is the only one with short stems.

Fix: `separate.stems_look_complete()` (both stems readable and within max(3 s, 3%) of the song's length, else incomplete);
`separate_vocals(expected_seconds=...)` raises `SeparationError` naming a damaged audio file as the likely cause;
`run_pipeline` passes the song's duration and also re-runs Demucs when saved stems exist but are truncated. Ironic cannot
be redone until the owner supplies a good copy of the audio.

Also found while ranking songs by agreement between our aligned line times and Whisper's own timestamps: several songs are
badly early (Girls Just Want to Have Fun: 9% of matched lines within 3 s, median gap 10.8 s; lines 20-30 s early from
line 3 on although lrclib and the aligned text have the same 56 lines). Cause not yet fixed: the whole-song CTC alignment
drifts when the audio holds repeated/extra sung material the text lacks. Candidate fix: anchor alignment windows to
Whisper's segment times.

## 2026-09-19 (night) — Lyric timing drifted 20-50 s on repeated choruses; fixed with anchored alignment

Owner asked for every possible fix for songs whose lyrics are "wildly off", and for anything unfixable to be set aside for
review. Ranking all songs by agreement between our aligned line times and Whisper's own timestamps found several badly early
(worst: Girls Just Want to Have Fun, 9% within 3 s; lines 20-50 s early). Root cause: `align.py` runs the whole song as ONE
CTC pass; when the audio has repeated choruses/ad-libs the text lacks (or the text repeats lines more than the audio), the
pass drifts and nothing notices. Measured against lrclib's own timestamps: Girls 4/56 lines within 2 s (45 more than 5 s off),
Every Breath You Take 14/77, The Chain 19/35.

What was built (each part test-first; real-data checked):
- `transcribe.py`: Whisper WORD timestamps saved in `transcript.json` (older caches are redone).
- `anchors.py`: `line_anchors` (in-order word matching gives each lyric line a coarse position); `drop_words_in_silence`
  (Whisper hallucinated "just wanna, just wanna" at 222-228 s of a track silent after ~220 s and anchored a line there);
  `combine_anchors` (lrclib's line timestamps settle WHICH copy of a repeated chorus was heard: anchors whose offset from lrclib
  agrees form runs; a run is believed only if long enough for its distance from the dominant offset -- three wrong copies at
  -33 s were first mistaken for a section shift); `plan_windows`.
- `align.py`: `prepare_alignment` (ONE model pass, shared), `align_blocks`/`align_words_anchored` (each block's words are only
  allowed inside its window; a window the aligner rejects is widened, then words are spread evenly), `vocal_loudness`.
- `sync.py`: `sync_agreement` (share of lines within 2 s of their anchor after allowing <= 2.5 s of consistent bias -- Whisper's
  first-word times run ~2 s early), `decide_alignment` (keep the whole-song result if >= 85% agree, else the anchored one if
  >= 70%, else the song is SET ASIDE with a concern; nothing to check against = unchanged behavior).
- `pipeline._align_lyrics` wires it; `lyric_lines.json` now carries `line_times` (from `fetch_lyric_lines_verified(times_out=)`).
Results (whole-song -> anchored, lines within 2 s of lrclib): Girls 4/56 -> 55/56; Every Breath 14/77 -> 72/77; The Chain
19/35 -> 29/35; Wild Horses 34/40 -> 39/40. Controls that were already right (Night Moves 55/63, Eleanor Rigby 32/34) keep the
whole-song alignment untouched.

Also: owner lyrics editing. "Flagged for Lyrics Review" rows now have Watch and Edit Lyrics (also for set-aside songs already on
YouTube, e.g. the damaged Ironic); Edit Lyrics saves `lyrics_owner.txt` (`owner_lyrics.py`), which the next Redo uses verbatim
(no online source, AI or audio check overrides it; Whisper words are still fetched for anchoring; timing is still checked).
Limits: sync can only be verified when Whisper/lrclib give enough anchors (>= 4 lines and 25%); songs whose lyrics come from a
source without timestamps, on loud recordings Whisper cannot hear, are left on the whole-song alignment.

## 2026-09-19 (night, later) — Redo record, live status page, comma spacing

- `redo_log.py` (`~/.playalongvideoproduction/redone_songs.json`): every redo is recorded -- started by `backup_song_outputs()`
  (so the GUI's Redo, a Batch regenerate and scripts all log it), finished by `run_pipeline()` with the song's concern.
  Records whether the song was already on YouTube; `youtube_replacements_pending()` lists finished redos of uploaded songs whose
  new version has not yet replaced the old one (`mark_replaced_on_youtube`). A redone song that is NOT on YouTube needs nothing:
  the redo replaced its local file in place (old one in `redo_backup_<time>/`) and the normal upload flow picks it up.
  `tests/conftest.py` redirects `LOG_FILE` so tests never touch the real record.
- NetEase writes some lines without a space after a comma ("Oh,when the working day is done"); `_MISSING_SPACE_RE` restores it
  ("1,000" is untouched).
- Redo of Girls Just Want to Have Fun (timing check: whole-song alignment agreed 3% -> anchored 89%; 90% agree with Whisper's own
  word times, 69/76 within 2 s of NetEase's timestamps, none >5 s off). The lyric source changed to NetEase (76 lines; the old
  lrclib text lacked the repeated ending choruses), so the redo generated 47 new images -- a redo only reuses images whose lyric
  text is unchanged.

## 2026-09-19 (later): the lyrics source's own line times as a second timing opinion

- `sync.decide_alignment(..., source_times=line_times)`: when neither alignment reaches `TRUST_AT_LEAST` (70%) against Whisper's
  word times but the better one still agrees with Whisper on >=50% (`WHISPER_MIN_SUPPORT`), it is kept if `source_timing_agrees()`:
  the lyrics source's own timestamps (lrclib/NetEase, saved as `line_times` in `lyric_lines.json`) agree on >=85% of lines within
  3 s, after allowing up to 3 s of consistent offset, and NO line is more than 5 s away. Never used to rescue an alignment Whisper
  contradicts (<50%), or when the source has no times.
- Real case: Back in the Saddle (loud rock) -- Whisper anchors confirmed only 67-70% and the song was set aside, yet NetEase's own
  times agreed on 31/33 lines within 3 s, none >5 s off (offset +0.2 s). Old (drifted) The Chain measured 60% within 3 s and lines
  up to 32 s off, so the check separates good from bad timing on the songs available.
- The 86 songs made before the anchored alignment have lyrics verified but NO word-timed transcript, so their timing has not been
  checked yet (only redone songs were).

## 2026-09-19 (evening): per-word timing precision replaces the 2 s line check

- Owner watched Go Your Own Way: lyrics right, but the green highlight was "close, but not exact", sometimes a line behind the
  singer, and "in general the songs in the past seemed better". Other songs (Night Moves, Cracklin' Rosie) were "spot on".
- Measured per WORD against Whisper's own word times (share of words starting within 0.5 s of where they were heard):
  spot-on songs 86-96% (Night Moves 89, Cracklin' Rosie 88, With or Without You 86, Eleanor Rigby 96); the loose ones 32-69%
  (Girls 34, The Chain 32-39, Go Your Own Way 41 after the redo / 69 before, Every Breath You Take 49, Money 53 / 62).
  The old gate (`sync.py`) let all of them pass: a line "agreed" within 2 s plus up to 2.5 s of allowed bias, and Whisper's
  first-word times are noisy on loud recordings.
- Regression found: for Go Your Own Way and Money the redo REPLACED a better whole-song alignment with the anchored one
  (anchored squeezed lines, e.g. two lines 0.08 s apart). Neither method wins everywhere.
- `precision.py`: `match_words` (in-order match of lyric words to heard words), `measure`, `blend` (per line, the candidate
  closer to what was heard; a dynamic-programming pass so the mix always stays in order), `choose_alignment` (best of
  whole-song / blended / anchored; ties -> whole-song; out-of-order candidates skipped). Passes at >=70% of matched words within
  0.5 s AND <=15% of lines clearly off (>1 s); otherwise the song is SET ASIDE and the reason names the lines (1-based).
  Needs >=20 matched words, else the old `sync.decide_alignment` still applies (incl. its source-line-time rescue).
- Result on the existing redos: Go Your Own Way blends to 79% and lines 1-26 land within 0.1 s; the 6 remaining bad lines
  (27, 28, 30-33, 36) sit in guitar solos / the fade-out where the file lists lines that are not sung as written, which no
  aligner can time -- so it is set aside with those lines named. The Chain (51%), Money (62%), Girls (34%), Every Breath
  (49%) are set aside too. Also: the older ~41 waiting songs have only whole-song alignments and no word-timed transcript.
- Known limit: `match_words` pairs lyric words with heard words by in-order text matching, so a repeated phrase can be paired with
  the wrong copy (Back in the Saddle: "I'm back in the saddle again" x6 gives fake errors of 40-60 s). That over-flags (a song is
  set aside for the owner to watch, never silently accepted). A time-aware match (using lrclib line times) would fix it.

## 2026-09-20: the precision measure over-flagged; corrected (owner deleted three good videos on my numbers)

- The first version of `precision.match_words` paired lyric words with heard words purely in order, so a line sung six times
  but heard twice paired later copies with earlier ones (fake 40 s "errors"). It flagged 48 of 139 songs; the owner deleted
  I Want to Hold Your Hand, Come Together and Girls from YouTube on those numbers. Rescored with the fix, I Want to Hold Your
  Hand is 79% (2/37 lines off), Come Together 90% (1/32), Faith 80% (5/46); only Girls (38%) is really loose.
- `match_words(..., near=[candidate start times])`: a word only pairs with a heard word within 4 s of where SOME candidate put
  it; words with no plausible partner have no evidence (not wrong evidence). The pipeline passes both candidates' times.
- Consequence: a line placed where Whisper heard nothing near it is invisible to word evidence (Go Your Own Way's lines 27 and
  34 sat in guitar solos). `silent_lines()` checks the vocal stem instead, per WORD (a line stretched across a solo whose
  words are sung is not silent; quiet hums/backing sit at 33-47% voiced and are fine). Any line with <15% of its words in
  singing sets the song aside, whatever its share.
- Result on the 139 uploaded/waiting songs: 32 imprecise (was 48); 25 holds released, 10 newly held.
- Credit/metadata lines seen at the START of songs: Desperado and Space Oddity kept NetEase's "作词/作曲/制作人" lines and Desperado
  a trailing "Recorded at Island Studios in London" (now filtered: recorded/mixed/mastered at); Vogue had "♪" lines (a line with
  no word characters is dropped); Wild Horses opened with "Rolling Stones - Wild Horses" (`_drop_header_lines`: title + artist
  only, first three lines). Older songs keep the stored lines until redone.
- Credits at the START of songs, second pass (owner: "if its not part of the audio, its a credit, right?"): a pattern list cannot
  keep up (Girls opened with "By. DanChu"), so `lyric_audio_match.drop_unsung_leading_lines()` drops any leading line (at most 4,
  never the last) that its own source stamped within the first 3 s while nothing is heard or sung until 3+ s later. Needs the
  source's line times (NetEase/lrclib); applied in the fetch stage, logged ("Removed lines that are not part of the song's audio").
  The pattern filters stay for plain-text sources: `_BY_CREDIT_RE` (first 3 rows only), "Recorded/Mixed/Mastered at", symbol-only
  lines, and title/artist header lines.
- `precision.blend` bug (Respect redo failed with `AlignmentSanityError`): mixing lines from two valid alignments could leave a word
  ending after the next one starts (58.53 s vs 58.27 s), which `combine.py` refuses. The blend now cuts the earlier word where
  the next begins; a property test covers it.
- Credits at the END too (Desperado redo, 2026-09-20): eight "Lead Vocals : Don Henley" ... "Strings : London Philharmonic Orchestra" lines
  were stamped 205-212 s, after the last lyric (188 s) and after Whisper's last heard word (194 s); the precision check found them
  in silence and set the song aside. `lyric_audio_match.drop_unsung_trailing_lines()` mirrors the leading rule: a trailing line
  stamped more than 3 s after the last heard/sung moment is dropped (trailing run only, at most 12, one line always remains).
  A scan of every stored song found only Desperado affected.

## 2026-09-20: cleared-for-upload record; held songs are no longer pending checkboxes

- Owner: "keep track of all the videos that have been cleared for upload, including all on the pending upload list, if there bad
  remove them ... they can be redone". `cleared_log.py` keeps an append-only history in `~/.playalongvideoproduction/cleared_songs.json`
  (latest entry decides): `run_pipeline()` records every finished run as cleared (no concern) or removed (with the reason), so a
  redo that fixes a song clears it again by itself.
- Gap found: the Pending YouTube Uploads checklist listed held songs too, so Select All + Upload Selected could send a bad video
  (the auto-retry path already skipped them). `list_pending_uploads()` now leaves out any song with a concern; held songs stay in
  Flagged for Lyrics Review, where Upload Anyway is a deliberate override.
- Backfill on 2026-09-20: 102 cleared (33 waiting to upload, 69 on YouTube), 38 not cleared.

## 2026-09-20: alignment memory no longer grows with song length

- Owner's redo queue: 'Tuesday's Gone' (7.5 min) was SIGTERMed by earlyoom at 10.3 GB RSS during the align stage (other jobs were
  running too); 'November Rain' (9 min), 'Ballad of Dwight Fry' and 'Sympathy for the Devil' were at the same risk, and 20+ minute
  tracks always died (see the long-track memory memory). Cause: `prepare_alignment` fed the whole vocal stem through wav2vec2 at once.
- `align._emission_in_pieces()` runs the model in 75 s pieces with 4 s of padding either side, drops the padding's frames and joins
  them, so frame k is frame k of a single pass (same count, same timing). Measured with the real MMS model: peak memory one pass
  100 s = 4.3 GB, 200 s = 6.5 GB (superlinear), pieces at 200 s = 3.9 GB (model alone 3.2 GB) -- flat for any length. Word timings vs
  one pass on 150 s of Night Moves: median 0 ms, 94% within 40 ms, none over 0.26 s (30 s pieces moved a hummed "Mm-mm" by 3 s, so
  keep pieces long). Shortened CLAUDE.md's "HISTORY 2026-09-" references to "HISTORY 9-" to stay under the size budget.
- Also this session: the queue runner let a song's process eat the next line of the queue file ('the-chain' became 'chain'); fixed by
  reading the list first and giving each song `< /dev/null`. November Rain and Janie's Got a Gun had lost their source files from
  the batch folder; identical-length copies (same duration to 6 decimals) were found in the Plex folder and copied into each
  song's work dir, where redo looks first. All the Young Dudes and Wouldn't It Be Nice audio was not found anywhere.

## 2026-09-20: automatic sync gate (`timing_gate.py`); "Like a Prayer" slipped through the old timing check

- Incident: "Like a Prayer" was cleared by the app (82% precise, 9/73 lines off), uploaded, and went public -- the owner watched it and
  found the middle half massively out of sync. Order-based line anchors showed the lyrics running 26 s ahead by line 41 and ~85 s by
  line 57. Cause: `precision.match_words(near=starts)` pairs each word with the heard word nearest where WE placed it, so a line dropped
  on the wrong repeat of a chorus looked precise; it also compared only 300 of 566 words. The 9-20 "false alarm" fix (37% -> 82%) had hidden
  a real drift. My first replacement (an order-based drift scan) over-flagged repeated-chorus songs, and an OCR frame check on the mp4 read
  garbage (86% "bad" on a spot-on song) -- both thrown out; each new measure was validated on songs the owner had judged before use.
- Owner's standard: a line is in sync when its words start within 0.5 s of where they are sung; a song passes at >=90% of judged lines.
  Measured per line (median of its words' offsets) with the search for the sung word limited to +-2.5 s (a search radius, not a tolerance --
  nothing sung elsewhere can excuse a line). Lines the recognizer barely heard are unjudged; under 8 judged lines the song cannot be
  checked. Validation: spot-on songs 94-98% (Night Moves, Desperado, Tiny Dancer, With or Without You, Eleanor Rigby, Cracklin' Rosie);
  Like a Prayer 51%, Billie Jean 30%, You Can't Always 64%, Girls 68%, The Chain 60%, Go Your Own Way 75%. Whisper word starts are
  themselves ~0.2-0.3 s uncertain, so no bar above ~90% is sensible (Night Moves sits right on it).
- Wiring: `_align_lyrics` scores whole-song/anchored/blended with `settle_alignment` and keeps the best (the old precision/sync checks
  still run first; their concern survives a passing gate, a failing gate's reason replaces it); no transcript / too few judged lines =
  set aside as "could not be checked". `list_pending_uploads`/`list_flagged_songs` call `hold_if_timing_fails` so an older song whose saved
  timing fails is held (concern written into lyrics_timed.json, recorded removed in cleared_log) before any upload path can send it; an older
  song with no transcript is NOT held there (only a fresh render holds on "could not be checked"). Upload Anyway goes through
  `_retry_pending_uploads` and is deliberately not guarded. `python -m lyricvideo.timing_gate [--hold]` reports every song (live videos are
  only reported). Not done: a better aligner (MMS_FA is the weak link; see TODO.md) -- expect many new songs to be set aside until then.
- Real run over `work/`: 80 of 139 songs pass, 59 fail. Uploads were turned OFF by the owner until this is resolved (memory note).

## 2026-09-20 (later): the pass mark is a setting; the Upload list shows only passing videos

- Owner: "i want only videos that pass 90% to be available in the upload to youtube section ... this 90% should be in a setting because i
  may want to change that later to like 95% with better code." He also confirmed the Flagged panel's **Upload Anyway** stays (a deliberate
  override; untouched).
- `Settings.timing_pass_percent` (int, default 90; "Quality check" section, slider 50-100). The gate reads it from ONE place:
  `timing_gate.use_pass_share_from(fn)` -- the GUI registers `lambda: self.settings.timing_pass_percent / 100` (the LIVE settings, so a moved
  slider applies at once; `_on_settings_changed` also invalidates the Upload/Pending/Flagged lists when it moves); a fresh render gets it
  explicitly (`run_pipeline` -> `_align_lyrics(needed=...)`); `python -m lyricvideo.timing_gate --percent N` (default: the saved setting).
  `SyncReport.needed` carries the bar it was judged against, so the reason text says "95% are needed".
- A moving bar means a written hold can go stale, so `hold_if_timing_fails` now keeps the hold in step: it holds a failing song, RELEASES a
  song this check held that now passes (cleared_log records it), and never touches a concern from another check (`is_gate_concern`: the whole
  text must start with the gate's own words -- a lyric-text concern, alone or combined with a timing one, is left alone). An older song
  with no transcript keeps whatever it had.
- Upload to YouTube list = `list_uploadable_songs()`: rendered songs (uploaded or not) that POSITIVELY pass the current bar with no other
  concern; a song that fails or cannot be checked is left out and the section shows "N videos hidden: below X%". Read-only (never writes to
  a live video). Measured on the real folder: 73 of 140 offered at 90%, 0.5 s to build.
- Also this session (owner, evening): deleted from YouTube the 8 videos that scored under 80% on the prototype numbers (08-anyhow, Lucy in
  the Sky, Good Vibrations, Rock and Roll Never Forgets, Ready for Love, Brown Sugar [was scheduled], Where the Streets Have No Name, I'm
  Eighteen -- all still fail the FINAL gate, though Ready for Love/Where the Streets score 83%/86% there, the prototype counted "oh oh"
  filler-only lines as wrong). Their local songs are held; state files renamed `youtube_state.deleted-on-youtube.json`. 9 live videos still
  fail 90% (Back in the Saddle, Come as You Are, Here Comes the Sun, Crazy, Big Ten Inch Record, Yesterday, Born to Run, Alone, Dreams
  [scheduled]) -- owner: leave them alone for now.

## 2026-09-20 (night): Whisper default is now large-v3

- Owner: "im good with a slower build if its more accurate ... just do the large one, dont need to test. can always revert back."
  `transcribe._DEFAULT_MODEL` medium -> `large-v3` (faster-whisper 1.2.1 can fetch it; ~3 GB, pre-downloaded). Measured medium on this 4-core
  CPU: 45 s per 120 s of vocals (0.38x real time; ~1.5 min for a 4-minute song); OpenAI's table lists medium ~2x large's speed, so large is
  ~3 min for a 4-minute song (an estimate; not measured). Not benchmarked for accuracy on singing -- Whisper is speech-trained, so more lines
  heard is expected, not proven. **The download is blocked from this network:** the 3 GB weights come from `us.aws.cdn.hf.co` (DNS resolves,
  connections fail, like github.com) and the xet transfer sat at 0 bytes, so `_effective_model_name()` uses `medium` (with a printed warning and
  the one-line download command) until large-v3 is on disk, then switches by itself; an explicit LYRICVIDEO_WHISPER_MODEL is never swapped,
  and transcript.json records the model that really ran. Revert with LYRICVIDEO_WHISPER_MODEL=medium in .env (no code change). The cache check compares the model
  name, so every saved transcript.json (medium) is redone the next time a Redo/Batch/verify_lyrics run needs it; the timing gate only READS
  transcript.json and keeps working on the old ones. Context: failing songs are mostly a lyric-TEXT problem (choruses; text not matching
  the vocals), so a bigger recognizer helps hear/anchor more lines but is not the whole fix -- see the memory note on why songs fail the gate.

## 2026-09-20 (late): large-v3 tried and reverted; medium stays the Whisper default

- Owner asked for large-v3 ("slower build is fine if more accurate; dont need to test"); I switched it and downloaded it (v2.0.45), then a test
  showed it is worse for this app, so the owner said "lets use medium". Measured on this 4-core CPU: (1) the SAME saved, spot-on videos score
  98% -> 85% (Cracklin' Rosie) and 97% -> 66% (With or Without You) against a large-v3 transcript, because its word starts run ~0.35-0.5 s
  EARLIER than medium's (median placed-minus-heard offset -0.09/-0.14 s medium vs +0.26/+0.38 s large-v3) -- the 0.5 s bar was calibrated
  on medium; (2) NOT fewer lyric words after all: the raw counts (257 vs 477, 167 vs 272) were medium's "ba ba ba" / "oh oh oh" runs; on content
  words With or Without You is 72 medium vs 75 large-v3 with 70 shared (my first "large hears fewer words" and the earlier "410 vs ~300"
  -- words heard vs words matched -- were both wrong); (3) 86 s vs 45 s per 120 s of vocals. Like a Prayer's redo scored 60% vs large-v3 but 71% vs
  medium (83% after the owner fixed its lyric text). The transcript cache records the model name, so a song transcribed by large-v3 is
  re-transcribed by medium on its next Redo. Reverted with `git revert` of the large-v3 commit (the download-fallback code went with it);
  the 3 GB weights remain in ~/.cache/huggingface and LYRICVIDEO_WHISPER_MODEL=large-v3 still selects them. Rule going forward: a model
  change needs the gate re-validated on the owner's judged songs first.
- Root cause of the failing Like a Prayer was found the same evening: v2.0.45's IPv4 outage (a manual IPv4 address the owner added switched
  Wi-Fi off DHCP) had made GitHub/Hugging Face unreachable; automatic DHCP fixed it (router is on 10.0.0.x).

## 2026-09-20 (late): "Mark Verified" -- the owner's own verdict overrides the automatic check

- Owner, on Like a Prayer (a hard song; 83% vs medium after his lyric fixes, with the last lines flagged where Whisper heard "I'm a prisoner"
  for the backing vocals): "if i decide its a good video its a good video." The gate is only an automatic yardstick (Whisper's ears).
- `owner_verified.py`: `mark_verified()` writes `owner_verified.json` in the song's work folder (NOT the song's concern field, which other checks
  own) with a fingerprint (sha256) of the exact `lyrics_timed.json` watched, the automatic score and the bar; `verification()` returns it only
  while that fingerprint still matches, so a Redo (new timing file) voids it by itself; `upload_label()` is the Upload-list row text
  ("name  ✔ verified by you (83% automatic)"). Verified songs: offered in the Upload and Pending lists, dropped from Flagged for Lyrics
  Review, never re-held by `hold_if_timing_fails`/`--hold`, and recorded in cleared_log ("verified by the owner"). It overrides ANY concern,
  timing or lyric text -- the owner looked at the whole video. Flagged rows (songs not on YouTube) get a green "Mark Verified" button with a
  confirm box that shows the automatic score ("83%, 90% needed"); Upload Anyway is unchanged. No "un-verify" button: redo the song.
- Whisper stays on medium (large-v3 tested and reverted the same evening -- see the entry above); a model choice in Settings was deferred.

## 2026-09-21: an Upload Anyway that the daily limit skips is remembered (it used to vanish)

- Owner clicked Upload Anyway on Like a Prayer with today's upload count already 7/7: the app said "left pending ... they'll upload automatically
  over the next few days", but nothing was recorded -- a held song is not in the Pending list and the auto-retry skips flagged songs, so it
  would never have uploaded. Fix: in `_retry_pending_uploads`, a song the limit defers that `needs_review()` (held and not verified) is marked
  verified (`_record_owner_verification`; only Upload Anyway can send a held song there, so the click IS the approval); results gain an
  "approved" list only when non-empty (existing callers/tests compare the dict). It then shows in Pending YouTube Uploads with its place kept.
  The result message no longer promises an automatic upload unless auto-upload is on ("they stay in Pending YouTube Uploads for you to
  upload"). Same session: Like a Prayer's stale youtube_state.json (he deleted the video by hand; the Flagged row's "already on YouTube"
  test is just that file existing, the app never re-checks YouTube) was renamed `youtube_state.deleted-on-youtube.json` so the row showed
  Mark Verified / Upload Anyway. Billie Jean and You Can't Always Get What You Want (also deleted by hand) still have stale records -- asked.

## 2026-09-21: a song that fails the sync check is HELD BEFORE its video is made

- Owner: a 60% song still went through chords, AI images (Replicate cost) and a 15-25 minute render, then landed in review anyway --
  "from now on that song will be held for review before it makes the video ... so i can get good videos made hopefully 100% of the time."
  Rule confirmed: >= the pass mark (Settings.timing_pass_percent, default 90) continues; below it is held; one setpoint, no second threshold.
- `pipeline.HeldBeforeVideo` (a RuntimeError with `.concern`) is raised inside the align stage right after lyrics_timed.json is saved when
  the gate's own concern is set (`is_gate_concern(timing_concern)`: "not precise enough" or "could not be checked"; a lyric-TEXT concern still
  makes its video as before). Before raising: a video left from a previous run is moved to `<slug>.previous.mp4` (a redo backup of it exists),
  `held_before_video.json` (`HELD_MARKER`) is written, and the redo/cleared records are completed (`_record_finished`, extracted from the end of
  run_pipeline). No chords, images or render happen. The marker is removed when the render stage finishes.
- Callers: `_run_worker` puts ("held", concern) -> `_on_held_before_video` (plain dialog: "No video was made", the reason, where to find it);
  `_run_batch_worker` records `results["held"]` (only when non-empty) and carries on -- the biggest saving is in Batch, no more image bills
  and renders for failing songs; the batch summary shows "Held for review (no video made): N"; the CLI prints it and exits normally.
  `batch.resolve_batch_items`: a held song counts as already processed (skipped unless the owner chooses to regenerate).
- `list_flagged_songs` includes songs with the marker even with no mp4. Their Flagged row has no Watch / Mark Verified / Upload Anyway (nothing
  to watch or upload) and a green **Render Anyway** button: `_on_render_anyway_flagged` confirms, then runs `run_pipeline(start_stage=
  "detect_chords")` with the saved timing (the align/hold block is skipped), so the video is made, the marker cleared and the song stays
  flagged for the owner to watch and Mark Verified. Redo (Edit Lyrics -> Redo) re-analyses from the lyrics and can now pass.
- Also: the gate's message shows one decimal when the score is not whole ("only 89.7% ... 90% are needed"), so a fail can no longer read like
  a pass (Dreams looked like "90%" and failed).

## 2026-09-21 (later): Remove from review (hide only), two-row review buttons, wider default window

- Owner: some songs are too hard to fix and had no way out of Flagged for Lyrics Review. "remove not delete" / "dont delete anything".
  A gray **✕ Remove** button on every review row -> confirm ("Nothing is deleted...") -> `dismissed_songs.dismiss_song("flagged", slug)`, the
  same display-filter store the Redo/Upload/Pending lists' ✕ use (`_visible_flagged_songs`). Files untouched. `batch.resolve_batch_items` counts a
  removed song as already processed (a Batch leaves it alone); a Redo (`_run_worker`) or a Batch regenerate calls the new `undismiss_song`, so a
  song the owner redoes comes back to review (and stays there only if it fails again).
- Owner: "the default gui size needs to be what i have it set at now ... upload anyway is cropped out", then "maybe even bigger" / "or 2 rows".
  Measured his open window with xdotool: 1552x1000 (code was 1400x1000). Default is now 1600x1000. The review row's six buttons made one ~596 px
  row, so the row is now two: Watch/Edit Lyrics/Redo, then Render Anyway or Mark Verified + Upload Anyway, then Remove. A REAL-window test
  (hidden CTk root, first in the suite; skips without a display) builds the actual row and asserts no button row is wider than 480 px.

## 2026-09-21 (afternoon): a 96% song was set aside by an older check's leftover rule -- one-line fix

- Owner's batch log for "You Can't Hurry Love": "Sync check: 96% of 47 lines" (a pass) followed by "SET ASIDE FOR REVIEW ... only 94% of the words ...
  lines 58 sit where nobody is singing". `settle_alignment` let the OLDER precision/sync check's concern (`earlier_concern`, here a cut-off last lyric
  fragment timed in silence) survive a passing gate. Fix: `settle_alignment` returns only the gate's own concern, so a song is set aside only when
  it is below the pass mark (owner: "all its doing is setting aside the videos [below] the setpoint ... that's all"). The older checks still only
  suggest which alignment to prefer. One test replaced (an older concern no longer survives a pass). The log still prints the older "Timing check:"
  line beside the "Sync check:" line (two different numbers) -- cosmetic, left as is.
- A bigger rework (counting lines timed in silence inside the gate's percentage, a `Song.silent_lines` field, dropping the older log lines) was written
  and tested, then SET ASIDE in `git stash` ("bigger sync-gate rework") because the owner wanted no large changes to a program that works well.
- Process mistake: `cut_release.sh` ran after a BLOCKED commit (CLAUDE.md size/hook) and published v2.0.51 with the v2.0.50 code and notes for a change that
  was not in it; the notes were corrected to say "No code changes... published by mistake". Release commands are now chained with && after commit+push.

## 2026-09-22: review buttons for Flagged for Lyrics Review -- Play MP3 and Whisper Text

- Owner request, while reviewing a full census of the ~265 processed songs (128/264 passed the timing gate; 143 set aside for review across the
  timing gate, the lyrics-text-match check, and one damaged source file): "on the non rendered held for review songs... i need the lyric text, the
  whisper text (you may need to create it) and the mp3 file... as buttons to be able to review the songs." Brainstormed as bounded (existing panel,
  existing row/button pattern): the lyric text was already covered by the existing Edit Lyrics button (no new button needed), so the actual gap was
  hearing the song and seeing what Whisper heard.
- Added, TDD: `transcribe.load_transcript_text(work_dir)` (the cached transcript's plain text, or None -- mirrors `load_transcript_segments`/
  `load_transcript_words`) and `pipeline.whisper_text_for(work_dir, model=None)` (that cached text if present, else `transcribe_vocals()` on the
  song's own vocal stem, same `htdemucs/<audio-stem>/vocals.wav` convention `run_pipeline` itself uses -- `model` injectable for tests). A quick
  audit found all 144 currently-flagged songs already have a cached `transcript.json` (the checks that flagged them needed one), so in practice this
  path is always instant; the fresh-transcription branch exists for a rare older song without one.
  Test files: `tests/test_transcribe.py`, `tests/test_pipeline.py`.
  - `_render_one_flagged_song` (`lyricvideo/gui.py`): a rendered song keeps its `▶ Watch` button; a song with no video yet gets `▶ Play MP3` in its
    place (`load_redo_inputs()` for the audio path, `_open_with_default_app()` -- the same OS-hands-off convention Watch already uses). Every row
    also gets a `Whisper Text` button: a small read-only popup (same dialog treatment as Edit Lyrics) that calls `whisper_text_for()` off the GUI
    thread and fills the box via `root.after`, guarded by `dialog.winfo_exists()` in case the owner closes it mid-transcription.
  - Owner mid-design, on Play MP3: "not sure i need the mp3 in the rendered videos, but i guess it doesnt hurt anything" -- so it was dropped from
    rendered rows (Watch already has audio) rather than shown everywhere.
  - The existing real-window test (`test_no_row_of_review_buttons_is_wider_than_a_narrow_window_can_show`) still passes with the added buttons
    (widest row ~394 px against its 480 px budget) -- no new test needed for layout, since it already covers any row this method can produce.

## 2026-09-22 (later): a finished Batch left the app insisting a video was still generating -- "this program will not close....wtf!!"

- Owner report, live: the app wouldn't close, and repeatedly clicking the X did nothing new. Checked the actual running process (`ps`,
  `/proc/<pid>/task/*/wchan`, `wmctrl`) rather than guessing: 0% CPU, main thread idle in Tcl's normal `poll_schedule_timeout` wait, exactly one
  window, no hidden dialog -- not a real deadlock. File timestamps under `work/` showed a 7-song Batch had genuinely finished over two hours
  earlier (last video 09:59:57); the owner separately confirmed "the batch was finished."
- Root cause: `_poll_queue`'s per-tick dispatch (`lyricvideo/gui.py`) had no exception handling around any single queued message's GUI update.
  After every batch item, `_refresh_retry_upload_options()` runs `invalidate()` on the Upload/Pending/Flagged sections, which rebuilds a section
  immediately (not lazily) if it happens to be open -- and if that rebuild throws for any reason, the exception propagates straight out of
  `_poll_queue`, so the closing `self.root.after(100, self._poll_queue)` line never runs. With no later tick left to ever read the batch worker's
  own eventual `("batch_done", ...)` message, `self._running` stays stuck `True` forever, even though the worker thread had already finished
  writing every file. `_on_close_window` then keeps asking "A video is currently being generated. Quit anyway?" -- correct code, acting on stale
  state -- so a cautious owner declines it every time, which reads exactly like "the program will not close."
- Immediate unblock: confirmed nothing was mid-render (no file writes in 10+ minutes) and killed the stuck process directly (`kill -TERM`) at the
  owner's go-ahead so they could relaunch once the fix shipped, rather than asking them to keep fighting the stale dialog.
- Fix: `_poll_queue`'s per-message dispatch was pulled out into its own function, `_dispatch_queue_message` (a plain module-level function, not a
  method -- `_poll_queue`'s own tests pass a bare stub as `self`, not a real `LyricVideoGUI`, so a new method wouldn't be visible on them; same
  reason `_open_with_default_app`/`_slugify` etc. are already free functions). The branches that used to `return` straight out of `_poll_queue`
  ("done"/"held"/"error"/"batch_resolved"/"batch_done") now `raise _StopPolling`, a tiny sentinel exception `_poll_queue` catches to stop this
  tick without rescheduling -- any OTHER exception is caught, logged as a warning, and the loop continues to the next queued message, guaranteeing
  the trailing `if self._running: self.root.after(100, self._poll_queue)` always runs. A new test
  (`test_poll_queue_keeps_polling_even_when_a_queued_messages_handler_raises`) reproduces the original bug with a `_refresh_retry_upload_options`
  that raises and asserts the poll still reschedules itself. `self._running` itself was never touched by this fix -- it was always correctly
  reset by `_on_batch_done`; the bug was purely that nothing was left running to ever call it.
- Note for later: this specific incident's own trigger (what actually threw inside that batch's list-refresh) was never identified -- the app
  has no persistent log file, so nothing survived to inspect after the process was killed. If it recurs, the new warning print
  (`WARNING: could not handle a 'batch_item_done' GUI update: ...`) will finally show up somewhere the owner can see it, which the silent version
  of this bug never did.

## 2026-09-22 (later still): Whisper Text split one row per lyric line

- Owner: "can you make the whisper text line by line like the lyrics texts? would make it a lot easier to figure
  out." Asked which "line by line" was meant -- Whisper's own natural pauses (quick, already-cached `segments`,
  but the count/boundaries wouldn't necessarily match the lyrics) vs. one row per LYRIC line (more work, but line
  N always lines up with line N of the lyrics for a direct side-by-side read). Owner picked the latter.
- Added `pipeline.whisper_lines_for(work_dir, model=None) -> list[str]`: for each lyric line, gathers the heard
  words within that line's own `[start_time - SEARCH_SECONDS, end_time + SEARCH_SECONDS]` window (the exact same
  window `timing_gate.check_sync()` already searches when scoring a line, reusing `SEARCH_SECONDS` and
  `HeardWord` rather than reinventing the windowing), joins them, and reads "(nothing heard)" for a line with
  nothing nearby; a blank lyric line (no words) is skipped, matching `load_editable_lyrics()`'s own convention.
  Ensures a transcript is cached first via the existing `whisper_text_for()` (transcribing fresh only for the
  rare song without one already). `lyricvideo/gui.py`'s Whisper Text popup now joins these lines with `\n`
  instead of showing `whisper_text_for()`'s single flat block -- `whisper_text_for` itself is unchanged and no
  longer imported by `gui.py` at all (nothing else there used it).
