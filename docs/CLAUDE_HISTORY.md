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
