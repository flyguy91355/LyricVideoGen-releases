# Tab-PDF-Driven Lyric Video Generator — Design

**Date:** 2026-09-06
**Status:** Approved by owner, ready for implementation planning

## Purpose

Owner wants a program that produces YouTube-style "play along" videos like
[this reference video](https://www.youtube.com/watch?v=40cJoHY7oVI) — scrolling
lyrics with guitar chord names synced in time over the song — but with an
added twist the reference video doesn't have: an AI-generated background that
changes per lyric line to visually follow the meaning of what's being sung,
instead of a plain static background.

This spec covers **v1: the tab-PDF-driven path only**. A fully audio-only
path (deriving lyrics via speech recognition and chords via audio analysis,
with no PDF input) is explicitly deferred — see Out of Scope.

## Scope

**In scope (v1):**
1. Input: one audio file (owner-supplied, local path) + one tab PDF
   (owner-supplied, local path) for the same song.
2. Extract the PDF's chord-over-lyric text (chord symbols positioned above
   specific words) into structured, ordered data: lyric lines, and which
   word within each line each chord symbol sits above.
3. Determine real timing for that known lyric/chord text against the real
   audio via forced alignment on the isolated vocal stem, producing
   word-level timestamps.
4. For each unique lyric line, generate an image prompt (via Claude, no
   owner input) capturing that line's meaning in the context of the song,
   render it via a cheap image-generation model, and cache it — repeated
   lines (e.g. a repeated chorus) reuse the same cached image rather than
   regenerating.
5. Render a 1080p 16:9 video: scrolling lyric lines, chord names positioned
   above the exact word they change on, composited over a Ken-Burns-panned
   version of that line's background image, muxed with the original song
   audio.
6. Staged/resumable CLI: each pipeline stage writes its artifact to a
   per-song working folder; a later stage can be re-run alone (e.g. after
   hand-correcting a parsed lyric line) without re-running expensive earlier
   stages.
7. First real test case: the owner's own "Wish You Were Here" audio
   (`/home/doug/GuitarTrainer/data/songs/bceed218-dc5f-4091-a534-8242a999e552/original.mp3`)
   and tab PDF (`/home/doug/GuitarTrainer/Tab Music/wish you were here tab.pdf`,
   also present under `~/Downloads`) — both already on this machine from
   prior GuitarTrainer work. Owner supplies audio/tab for songs they have
   lawful access to; this spec does not embed or ship any real song's lyric
   text as sample/test data (see Testing).

**Explicitly out of scope (v1):**
- **Audio-only fallback** (deriving lyrics via ASR and chords via audio
  chord-detection when no tab PDF is supplied) — deferred to a later spec.
  v1 requires a tab PDF for every song it processes.
- Six-line ASCII note-grid tab sections (instrumental solos etc.) — this
  program only needs chord-over-lyric sections, since it's producing a
  chord/lyric play-along video, not a note-by-note highway. A PDF section
  that isn't chord-over-lyric text is simply not usable as input for this
  tool.
- Scanned/image-only PDFs (no real text layer) — same OCR-is-out-of-scope
  boundary GuitarTrainer's tab-import spec already established.
- AI-generated *video* clips for the background (only stills + Ken Burns
  pan/zoom, per owner's explicit choice on cost/complexity grounds).
- Auto-uploading to YouTube — this produces a local `.mp4` file only.
- Chord fingering diagrams — only chord *names* are shown, matching the
  reference video.

## Approach

### PDF parsing — reuse GuitarTrainer's proven technique, new code

GuitarTrainer's `2026-09-02-plaintext-tab-import-design.md` already
confirmed (against a real owner-supplied PDF, coincidentally this same
"Wish You Were Here" file) that tab PDFs found in the wild are typically
**text-layer PDFs**, and that naively extracting the text stream loses the
column alignment that says which word a chord symbol sits above. The fix
proven there — read each character's real page coordinates (`pdfplumber`
exposes per-character bounding boxes) and bucket by X-position rather than
trust document-order text — is reused here as a technique (new
implementation, separate project, no shared code).

Parsing walks the PDF page by page looking for a "chord line" (short tokens
matching a chord-symbol grammar: root note + optional quality/extension,
e.g. `Em7`, `C/G`, `A#dim`) immediately followed by a "lyric line" (ordinary
words). Each chord's X-position is matched to the nearest word (by
horizontal overlap/proximity) in the lyric line below it, producing an
ordered structure per line: `(lyric_text, [(word_index, chord_symbol)])`.

**Failure modes, handled explicitly (not lumped together):**
- No text layer at all (scanned/image PDF) → clear error, no OCR attempted.
- Text layer present but no chord-line/lyric-line pairs found anywhere →
  clear error naming what was expected, so the owner knows this PDF isn't
  the right shape rather than getting silent garbage.

### Forced alignment — reuse GuitarTrainer's chosen tool

Same tool GuitarTrainer's `2026-09-02-synced-lyrics-design.md` chose and for
the same reason: `torchaudio`'s CTC-based forced-alignment pipeline (`MMS_FA`
bundle) run on the **vocal stem** (isolated via Demucs first, since
background instruments confound text-to-audio alignment). Alignment-only,
not full ASR, since the text is already known from the PDF — cheaper and
more reliable than a full speech-recognition decode.

Because the PDF gives word-level chord placement and MMS_FA gives word-level
timestamps, chord changes land on the exact word/timestamp they're written
above in the source tab, not just an approximate line start. This is the
key accuracy win over a pure audio-analysis approach, and directly serves
the "must be in sync" requirement.

**Honest uncertainty, stated plainly to the owner on first use, not hidden:**
forced-alignment models are trained/benchmarked on spoken audio; singing is
acoustically different (sustained notes, vibrato, stylized delivery). This
is a real open question only a real test resolves — same caveat
GuitarTrainer's spec already carries, inherited here rather than
re-litigated. A sanity check (alignment output must land within the audio's
real duration, in monotonically increasing order) catches an obviously
broken alignment; anything short of "obviously broken" is not otherwise
scored automatically — the owner's own eyes/ears on the first real render
of "Wish You Were Here" is the actual test.

### Background image generation

For each unique lyric line's text (exact-duplicate lines, e.g. a repeated
chorus, are deduplicated and reuse the same generated image), Claude is
given the full lyric text for context plus that specific line, and asked to
write an image-generation prompt capturing that line's meaning/imagery —
fully automatic, no owner input into individual prompts. That prompt is sent
to a cheap image-generation model via Replicate (Flux Schnell or SDXL,
owner's choice on cost grounds) and the resulting PNG is cached to disk
keyed by a hash of the line text, so re-running the pipeline (or hitting the
same line twice in one song) never regenerates an image unnecessarily.

If generation fails for a given line (API error, bad output), retry once,
then fall back to a plain-color background for just that line rather than
aborting the whole render.

### Rendering

Frames are composited in Python with Pillow — a slow continuous pan/zoom
(Ken Burns) applied to the current line's background image, the current
window of scrolling lyric lines drawn on top (matching the reference
video's scrolling-lines layout), and chord name labels drawn above the exact
word position they apply to at that timestamp. Assembled into video via
moviepy/ffmpeg and muxed with the original song audio.

This was chosen over two alternatives:
- **Headless-browser/CSS-driven rendering** (build the frame as an HTML/CSS
  page, screenshot each frame) — more visually flexible for complex
  animation, but adds a browser-automation dependency this fairly
  standard layout doesn't need.
- **Pure ffmpeg filter-graphs** (`drawtext`/`zoompan` only, no Python frame
  loop) — fastest, but multi-line scrolling text with per-word chord label
  positioning is fragile and hard to express reliably in ffmpeg's filter
  syntax.

Pillow compositing gives full control with plain Python, and the layout math
(what's on screen at time `t`) is unit-testable independent of actually
rendering pixels.

## Data Model

- `ChordWord`: `word: str`, `chord: str | None` (the chord symbol placed
  above this word in the source PDF, if any).
- `LyricLine`: `words: list[ChordWord]`, `start_time: float`,
  `end_time: float` (from forced alignment), plus per-word timestamps once
  alignment resolves them.
- `Song`: `title`, `audio_path`, `vocal_stem_path`, `lines: list[LyricLine]`,
  `image_cache: dict[str, Path]` (line-text-hash → generated image path).

Intermediate artifacts persisted per-song under a working folder:
`vocals.wav`, `parsed_tab.json` (PDF extraction output, pre-alignment),
`lyrics_timed.json` (post-alignment, the real `Song.lines` data — hand-
editable before rendering), `images/<line-hash>.png`, `final.mp4`.

## Error Handling

- PDF has no text layer → clear error, no OCR attempted.
- PDF text layer present but no chord-over-lyric blocks found → clear error
  naming the expected shape.
- Forced-alignment output fails the sanity check (out of audio's duration
  range, or non-monotonic) → clear error rather than proceeding to render a
  silently-wrong-synced video.
- A single line's image generation fails after one retry → falls back to a
  plain-color background for that line only; render continues.
- Any pipeline stage's artifact already exists on disk and `--stage` targets
  a later stage → prior stages are skipped, not re-run.

## Testing

- PDF chord/lyric parsing: unit tests against small hand-written synthetic
  PDF fixtures (invented placeholder lyric text and chords, never a real
  song's actual lyrics — same rule GuitarTrainer's test suite already
  follows).
- Forced-alignment integration: wrapper/interface tests use synthetic
  spoken-word audio with known invented text, verifying error-handling and
  the sanity-check logic, not real singing-alignment accuracy (which, per
  the Honest Uncertainty note above, only a real test can judge).
- Frame-layout math (what text/chords/image are on screen at time `t`) is
  pure-function unit tested independent of actual Pillow rendering.
- Real end-to-end verification is manual: render "Wish You Were Here" from
  the owner's own audio + tab PDF, judged by eye/ear whether lyrics and
  chords are actually in sync.

## Dependencies (new project)

`pdfplumber` (PDF text/coordinate extraction), `torch` + `torchaudio`
(forced alignment, MMS_FA), `demucs` (vocal stem separation), `pillow` +
`moviepy` (frame compositing/video assembly), `replicate` (image
generation), `anthropic` (image-prompt writing). CPU-only per owner's
choice — Demucs + forced alignment will be slow on this hardware (same
order of magnitude as GuitarTrainer's existing CPU-only pipeline), accepted
as a known tradeoff rather than adding cloud ML costs beyond the
already-approved image-generation spend.

## Note on Output Use

The rendered video embeds the original copyrighted song's audio. This spec
only produces the local file — it's the owner's call, same as any cover/
play-along video, whether/how to publish it and how to handle any resulting
platform Content ID claim. Not a blocker for building the tool, just stated
plainly rather than left implicit.
