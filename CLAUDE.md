# LyricVideoGen

Generates synced lyric+chord "play along" videos from a tab PDF and an audio
file: scrolling lyrics with chord names timed over the words they change on,
composited over an AI-generated, Ken-Burns-panned background image that
changes per lyric line to follow the song's meaning. Full design in
`docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md`; the
build plan (all steps checked off) is in
`docs/superpowers/plans/2026-09-06-tab-pdf-video-generator.md`.

## Running it

- **GUI (normal use):** double-click the `LyricVideoGen` desktop icon, or run
  `./run_lyricvideogen.sh` from the repo root. Supply a title, audio file, and
  either a tab PDF or a chords-text file, then click Generate. Built with
  Tkinter (`lyricvideo/gui.py`).
- **CLI (staged/resumable, useful for debugging one stage):**
  ```bash
  cd /home/doug/LyricVideoGen
  .venv/bin/python -m lyricvideo.pipeline --audio <path> --tab-pdf <path> \
      --work-dir <dir> --title "<title>" [--stage separate|parse|align|images|render]
  ```
  `--stage` resumes from a later stage using artifacts already written to
  `--work-dir` by an earlier run — useful since `separate`/`align`/`images`
  are the slow/expensive stages.
- Requires `ANTHROPIC_API_KEY` and `REPLICATE_API_TOKEN` in `.env` at the repo
  root (both are set locally; see `.env.example` for the template). No
  Alpaca/trading credentials are involved — this is a separate, unrelated
  project from AITrading despite living alongside it on this machine.

## Pipeline stages (`lyricvideo/pipeline.py`, `STAGES`)

1. **separate** (`separate.py`) — Demucs two-stem split of `--audio` into
   vocals/instrumental (CPU). Output path convention
   (`work_dir/htdemucs/<audio_stem>/{vocals,no_vocals}.wav`) is what makes
   `--stage` resumption work — later stages look for the file at that same
   path rather than re-running Demucs.
2. **parse** — extracts structured `(lyric line, [word_index, chord] pairs)`
   data from the input. Three input paths, in order of preference:
   - `--chords-text-file`: owner-typed plain chord-over-lyric text
     (`plaintext_chords.py`) — pure deterministic parsing, **no Claude call at
     all**. Use this when vision-based chord mapping keeps hitting Anthropic
     content-filtering on a song's lyrics (confirmed real and
     non-deterministic on some songs).
   - `--tab-pdf` with a real text layer: `pdf_parse.py` (pdfplumber),
     deterministic, no Claude call.
   - `--tab-pdf` that's scanned/image-only (raises `NoTextLayerError`): falls
     back to `vision_parse.py`, which requires `--lyrics-file` (owner-supplied
     plain lyrics). Claude only returns `[word_index, chord]` position pairs
     against text you already gave it — **it never generates or alters lyric
     text itself.**
3. **align** — forced word-level alignment (`align.py`) against the isolated
   vocal stem; `combine.py` merges alignment timing back onto the parsed
   lines/chords; `instrumental_chords.py` times chords that fall in
   instrumental (no-lyric) gaps between lines by anchoring to real
   frame-to-frame chroma novelty peaks in the no-vocals stem, with a minimum
   time-spacing constraint between chosen boundaries (rejecting candidates
   too close to each other OR to the gap's own start/end) so one sharp
   transition's smeared neighboring frames can't crowd out every other real
   chord change — see the 2026-09-08 history entry for the concrete failure
   mode this fixed.
4. **images** — `imagery.py`: one Claude call summarizes the whole song's
   gist once (`summarize_song_gist`), then each *unique* lyric line gets its
   own generated background image (Replicate), cached by line text so a
   repeated chorus reuses its image instead of paying to regenerate it. Also
   reuses any `images_backup_*/` archive left in the work dir before
   generating new images — back up rather than delete `images/` if you want
   to regenerate render-only changes without re-paying for images.
5. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling
   lyrics + chord flashes + Ken Burns pans over the audio into the final
   1080p mp4 (`work_dir/<slugified-title>.mp4`).

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
(`version.py`), a GitHub Releases API client against the public,
unlisted `flyguy91355/LyricVideoGen-releases` repo (`release_client.py`,
httpx-based), and allow-listed archive extraction/copy
(`apply.py` — allows `lyricvideo/`, `tests/`, `docs/`, `requirements.txt`,
`CLAUDE.md`, a bare top-level `*.py`/`*.sh`; denies `.env`, `songs/`,
`work/`, `.venv/`). `gui.py` checks once on launch (background thread) and
shows a clickable banner if a newer release exists; clicking it opens a
dialog with the release notes and an Apply Update button (confirms first,
then downloads/reinstalls-dependencies-if-changed/copies/writes the new
VERSION) followed by a Relaunch Now button. No severity tiering, no
periodic re-check, no manual "Check Now" button — see the spec for why.
Cut a release with `scripts/cut_release.sh <version-tag> <notes-file>` (the
releases repo itself was created 2026-09-08, public/unlisted, no source
code — just synced snapshots + release notes).

## Tests

```bash
cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/ -v
```
