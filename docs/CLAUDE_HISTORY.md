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
