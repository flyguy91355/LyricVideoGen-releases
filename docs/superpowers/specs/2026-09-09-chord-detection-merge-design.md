# MP3-Only Pipeline: LyricChord's Chord/Lyric-ID Guts + This Program's Sync/Visuals — Design

**Date:** 2026-09-09 (revised twice — see history below)
**Status:** Drafted, pending owner review

## Purpose

This project was named LyricVideoGen through 2026-09-09, then renamed in place to
**PlayAlongVideoProduction** on the owner's decision that this merge should be the
project's own forward version progression (continuing its real git history, `VERSION`,
and Update Available mechanics) rather than a permanently-separate fork. Before the
rename, the repo's full prior state (through the OCR-groundwork commit) was pushed to
GitHub at `flyguy91355/LyricVideoGen`, which now stays frozen there as a retrievable
snapshot of "the existing program." Going forward, this repo lives at
`/home/doug/PlayAlongVideoProduction`, pushed to a new GitHub repo,
`flyguy91355/PlayAlongVideoProduction`.

(An earlier same-day attempt forked LyricVideoGen into a separate, detached
`/home/doug/PlayAlongVideoProduction` directory. The owner clarified they wanted an
in-place rename instead; that fork — including its own copy of this spec — was
deleted. This file reconstructs and supersedes it.)

The merge itself combines the strongest half of two of the owner's own projects, and
**drops the tab-PDF/chords-text input entirely** in favor of LyricChord's "just drop an
MP3" approach:

- **From LyricChord** (`/home/doug/Lyric+Chord`, MIT licensed, untouched by this work):
  the owner is specifically impressed by two things — its chord detection ("spot on")
  and that it needs nothing but the audio file to work. Both come from the same set of
  modules: `metadata.py` (resolve title/artist from tags/filename/online consensus so
  lyrics can even be looked up), `lyrics.py` + `vocal.py` (fetch synced lyric text
  online), and `chords/local.py` + `theory.py` (detect real chords from the audio
  itself). All of this is ported in.
- **From this program's existing pipeline** (kept, unchanged in this area): Demucs
  vocal separation + real forced word-level alignment against the isolated vocal stem
  (the owner loves how well its lyrics track the actual vocals — better than trusting
  LyricChord's third-party LRC timestamps directly), AI-generated per-line background
  images with Ken-Burns panning, the overall look of the lyric block over that
  background, and the **Redo an Existing Song** feature (reusing cached Demucs stems
  and images on a re-run — owner-requested 2026-09-09 to carry forward unchanged in
  spirit, adapted to the new stage list; see below).
- **Display**: LyricChord's NOW/NEXT/segmented-timeline chord bar replaces the
  above-word chord flash (owner-confirmed 2026-09-09).

## Scope

**In scope:**

1. **New stage: `identify`** (ported from LyricChord's `metadata.py`) — resolve
   title/artist/duration: ID3/Vorbis/M4A tags → filename parsing → lrclib-artist-
   consensus + MusicBrainz-by-duration lookups when the artist is still unknown.
   Depends only on the raw audio file; runs first, before `separate`.
2. **New stage: `fetch_lyrics`** (ported from LyricChord's `lyrics.py` + `vocal.py`) —
   sidecar `.lrc`/`.txt` next to the audio → lrclib (edition-consensus voting across
   every matching-length record, with `vocal.py`'s narrow vocal-onset-rise check to
   disambiguate disagreeing first-line candidates) → `syncedlyrics` aggregator
   fallback. Produces plain lyric-line **text** (LRC timestamps, when present, are
   discarded in favor of step 4's real alignment — text only). Needs `identify`'s
   resolved title/artist plus the raw audio file (for the vocal-onset re-read); does
   not depend on `separate`.
3. **`align` stage (existing, unchanged internals)** now aligns `fetch_lyrics`'s output
   against the Demucs vocal stem instead of tab-parsed text.
   `align_words(vocals_wav_path, words: list[str])` already takes a flat word list and
   has no idea where the words came from, so this is a clean swap, not a rewrite.
4. **New stage: `detect_chords`** (ported from LyricChord's `chords/local.py` +
   `theory.py`) — harmonic separation → CQT chroma → beat-sync → template match →
   key-aware Viterbi decoding, run on Demucs's own `no_vocals.wav` (cleaner than
   LyricChord's own full-mix harmonic/percussive split, since vocals are already fully
   removed). Produces one `ChordTrack` for the whole song, independent of lyrics.
   `chords/__init__.py`'s sheet/Ultimate-Guitar cascade is **not** ported — there is no
   sheet input anymore, so `get_chords` always calls `detect_chords` directly (this is
   also literally what LyricChord itself does for a sidecar-less, online-chords-off
   MP3, so "exactly like the new program" holds).
5. `ChordEvent`/`ChordTrack` data models (ported from LyricChord's `models.py`) added
   to `lyricvideo/models.py`.
6. **Render/display**: LyricChord's bottom chord panel (NOW box, NEXT box + countdown,
   duration-proportional segmented timeline lane) ported into `render.py`'s existing
   1920×1080 Ken-Burns frame as a new translucent panel, plus a Key/BPM header badge.
   The lyric block's own look (scrolling text over the Ken-Burns background, including
   the 2026-09-09 word-highlight/chord-flash color refinements) is unchanged.
   **Explicitly NOT ported** (owner-confirmed 2026-09-09): LyricChord's title/artist
   text header. Only the chord bar and the Key/BPM badge are added; no song title or
   artist name is drawn into the video frame anywhere.
7. **Instrumental-gap background images now follow the chord, not a frozen lyric
   line** (owner-requested 2026-09-09, second request). Today, `build_scene`'s
   `image_key` is `line_hash(current.text)`, and during an instrumental gap `current`
   stays pinned to the last-sung line (nothing updates it until the next line's
   `start_time`), so the background silently freezes for the whole gap even though it
   changes correctly, line-by-line, whenever vocals are present. Fix: `build_scene`
   additionally takes the `ChordTrack` and detects "in a gap" as `t` falling outside
   every line's `[start_time, end_time]` window (before the first line, between one
   line's `end_time` and the next line's `start_time`, or after the last line's
   `end_time`); while in a gap, `image_key` is derived from the chord active at `t`
   instead of the frozen line text. The `images` stage additionally generates one
   image per distinct chord label that actually occurs during a gap (deduped the same
   way repeated lyric lines already dedupe — same chord label anywhere in the song
   reuses one image), using a synthetic caption (e.g. `"[Instrumental — chord: Am]"`)
   in place of line text as the prompt driver, so Claude still gets real content to
   generate from.
8. **Deleted entirely** (superseded, not just unused):
   - `pdf_parse.py`, `vision_parse.py`, `plaintext_chords.py`, `ocr_parse.py`,
     `ocr_clean.py` (confirmed unreferenced by any other source file — already
     orphaned before this merge) — the
     tab-PDF/chords-text lyric+chord extraction path (including its not-yet-wired OCR
     fallback groundwork). No longer any input to parse.
   - `instrumental_chords.py` — chroma-novelty gap timing for already-known chords;
     superseded by full-song `detect_chords`, which assigns real chords through
     instrumental sections too, not just timing boundaries.
   - `SceneWord.chord`/`chord_active`/`chord_flash`, `Scene.instrumental_chord`/
     `instrumental_chord_flash` in `layout.py`; `_draw_line_with_chords`'s chord-label
     half and `_draw_instrumental_chord` in `render.py`.
9. **GUI simplification** (`gui.py`): remove the "Tab/chords PDF" and "chord-over-lyric
   text file" rows and the `--lyrics-file` vision-fallback field entirely. "Song title"
   becomes an optional override, pre-filled once `identify` resolves it, rather than a
   required owner-typed field. The only required input for a NEW song becomes the
   audio file.
10. **Redo an Existing Song, preserved and adapted** (owner-requested 2026-09-09,
    explicitly so cached images can be reused): today it resumes at `"align"`, reusing
    `parsed_tab.json` and the Demucs stems (`list_redoable_songs`/`load_redo_inputs`
    read the original `audio_path`/title back off `lyrics_timed.json`). With
    `parsed_tab.json` gone, the redo path resumes at `"fetch_lyrics"` instead (reusing
    the Demucs stems from `"separate"` and, as always, `get_or_generate_image()`'s
    content-hash cache making an unmodified `images/` dir a free no-op at the `images`
    stage). `backup_song_outputs()` and `prepare_images_for_fresh_regeneration()` carry
    over unchanged — neither touches tab/chord data.
11. **New dependencies**: `mutagen` (tag reading), `requests` (LyricChord's lyrics/
    metadata modules use `requests`, not `httpx`; kept as-is rather than rewritten),
    `syncedlyrics` (fallback aggregator), `scikit-learn` (a `librosa` dependency
    LyricChord pins directly). `librosa`/`scipy`/`numpy` are already dependencies here.
12. **Tests**: remove tests tied to deleted modules (`test_pdf_parse.py`,
    `test_vision_parse.py`, `test_plaintext_chords.py`, `test_instrumental_chords.py`,
    `test_ocr_parse.py`, `test_ocr_clean.py` if OCR-only); add unit tests for
    `identify`, `fetch_lyrics`, and `detect_chords` (style ported from LyricChord's own
    `tests/`); add tests for the new NOW/NEXT timeline lookup and the instrumental-gap
    image-key selection; update `test_layout.py`/`test_render.py`/`test_combine.py`/
    `test_pipeline.py`/`test_gui.py` for the new stage list, simplified GUI fields, and
    the adapted redo resume-point.
13. `CLAUDE.md` rewritten for the merged pipeline's real current-state behavior once
    implemented (today it still accurately describes the pre-merge tab-PDF pipeline).

**Out of scope:**

- Ultimate Guitar online-chords lookup and chord-sheet parsing/alignment
  (`chords/online.py`, `chords/sheet.py`) — not needed once there's no sheet input and
  audio always wins on chord identity.
- AcoustID audio-fingerprint identification (`metadata.py`'s 4th-tier fallback) — needs
  an API key and the external `fpcalc` binary; skip unless tags+filename+lrclib/
  MusicBrainz all fail to identify a song, which should be rare. Can be added later if
  it turns out to matter.
- Any change to image generation, Ken-Burns rendering mechanics, or the Update
  Available feature's actual code — carried over unchanged. (The Update Available
  feature's `flyguy91355/LyricVideoGen-releases` distribution repo is deliberately
  still the pre-rename name — see `docs/CLAUDE_HISTORY.md`'s 2026-09-09 rename entry —
  renaming/recreating it is a separate decision for whenever a release is next cut.)
- Any change to LyricChord's own repo.
- Renaming the internal Python package (`lyricvideo/`) to match the project name.

## Architecture

```
STAGES = ["identify", "separate", "fetch_lyrics", "align", "detect_chords", "images", "render"]

audio file ──► identify (metadata.py: tags/filename/lrclib/MusicBrainz)
                    │
                    ├──► title/artist/duration ──► fetch_lyrics (lyrics.py + vocal.py:
                    │                               sidecar/lrclib/syncedlyrics; the
                    │                               raw audio file is also re-read here,
                    │                               for vocal-onset disambiguation)
                    │                                        │
                    │                                        ▼ lyric-line TEXT
                    └──► separate (Demucs) ──┬──► vocals.wav ──► align (forced
                                              │                   alignment of that text)
                                              │                          │
                                              └──► no_vocals.wav         │
                                                       │                 │
                                                       ▼                 │
                                                 detect_chords            │
                                             (librosa, full song)        │
                                                       │                 ▼
                                                       └──────────► images ──► render
                                                          (ChordTrack passed straight
                                                           through, independent of
                                                           the lyric Scene/words)
```

`identify` needs only the raw audio file. `fetch_lyrics` needs `identify`'s resolved
title/artist (to search lrclib/syncedlyrics) plus the raw audio file itself (for
`vocal.py`'s onset-disambiguation re-read) — it does **not** depend on `separate`.
`detect_chords` depends on `separate`'s `no_vocals.wav`. `align` depends on both
`separate`'s `vocals.wav` and `fetch_lyrics`'s text. `pipeline.py`'s `STAGES` list
stays a simple sequential list (not a concurrent scheduler) — the order above
(`identify` → `separate` → `fetch_lyrics` → `align` → `detect_chords` → `images` →
`render`) satisfies every dependency in one straight line, matching the existing
`start_idx`-gated resumption model (`--stage <name>` skips everything before it, same
as today). Redo an Existing Song resumes at `"fetch_lyrics"`.

## Song Identification & Lyrics

Ported close to verbatim from LyricChord:

- `metadata.py`: `read_tags` (mutagen) → `parse_filename` → (if artist still unknown)
  `lrclib_artist_for_title` + `musicbrainz_lookup` consensus. Produces a `SongInfo`
  (title, artist, duration, source, alt titles for lyric-search fallback).
- `lyrics.py`: `fetch_lyrics(info)` tries, in order: a sidecar `.lrc`/`.txt` next to the
  audio file; `fetch_lrclib` (parallel queries across title variants, edition-consensus
  voting via `choose_lyrics_candidate`, using `vocal.py`'s `vocal_onset_rise` — run
  against the ALREADY-DECODED audio the owner supplied, no extra fetch — to break ties
  between disagreeing first-line candidates); `fetch_syncedlyrics` as a last resort.
  Only the resulting line **text** is used; any LRC timestamps that come along for the
  ride are discarded once `align` produces real ones.
- If nothing is found anywhere, the song renders with the AI-generated background/
  Ken-Burns pans and detected chords but no lyric text — same graceful-degradation
  behavior LyricChord already has for an unfindable/instrumental track.

## Chord Detection

Ported near-verbatim from LyricChord's `chords/local.py` on `no_vocals.wav`:
harmonic/percussive split, CQT chroma, beat-sync, template match against
12-root × {maj, min, 7, min7, maj7}, Krumhansl-Schmuckler key estimate with a diatonic
prior, Viterbi decoding, silence→"N" pass, short-segment merge. `theory.py` ported
as-is. Result: `ChordTrack(events, key, bpm, source="librosa")` spanning the full song.

## Display

LyricChord's `_draw_chords` + landscape `chord_box`/`now_box`/`next_box`/`lane_box`
geometry ported into `render.py` as a new translucent panel near the bottom of the
frame (this program today draws bare stroke-outlined text with no panel — needed here
for legibility over arbitrary Ken-Burns imagery). NOW box, NEXT box + countdown,
duration-proportional scrolling timeline lane, Key/BPM header badge. Lookup against
playback time `t` is a `bisect` over `ChordTrack.events`, fully independent of which
lyric line/word is on screen.

## GUI

`gui.py`'s form loses the "Tab/chords PDF" and "chord-over-lyric text file" rows and
the vision-fallback lyrics-file field. "Song title" changes from a required owner-typed
field to an optional override, auto-filled once `identify` resolves it (editable before
Generate, in case auto-ID gets it wrong — same escape hatch LyricChord itself doesn't
strictly need but costs nothing to keep). The only required input for a new song
becomes the audio file. The "Redo an Existing Song" section (dropdown, "Generate new
images" checkbox, Redo button) stays, targeting the new `"fetch_lyrics"` resume point.

## Testing

- New unit tests for `identify` (`parse_filename`, `artist_consensus`,
  `rank_musicbrainz` — all pure functions, ported test style from LyricChord),
  `fetch_lyrics`/`choose_lyrics_candidate` (edition-consensus logic), and
  `detect_chords`/`theory.py` (ported from LyricChord's `tests/test_audio_and_render.py`
  and `test_pipeline.py`).
- New unit tests for the NOW/NEXT/segment-in-view lookup as a pure function of
  `(ChordTrack, t)`.
- Removed: `test_pdf_parse.py`, `test_vision_parse.py`, `test_plaintext_chords.py`,
  `test_instrumental_chords.py`, `test_ocr_parse.py`, and OCR-only cleanup tests.
- Rewritten: `test_layout.py`, `test_render.py`, `test_combine.py`, `test_pipeline.py`
  (new `STAGES` list and stage dependencies), `test_gui.py` (simplified form fields,
  adapted redo resume-point).
- Full suite run (`.venv/bin/python -m pytest tests/ -v`) before considering the merge
  complete.

## Open Questions / Owner Decisions Already Made

- Audio-detected chords always win; no sheet/online-chords cascade ported. (Confirmed.)
- MP3-only input — tab-PDF/chords-text dropped entirely, lyric text fetched online
  instead. (Confirmed 2026-09-09.)
- Full LyricChord-style bottom bar, no above-word flash retained. (Confirmed.)
- Key/BPM header badge included. (Confirmed.)
- This program's forced alignment kept as the sync mechanism, now applied to fetched
  (not typed) lyric text. (Confirmed.)
- AI background images + Ken-Burns look kept unchanged. (Confirmed.)
- Redo an Existing Song kept, adapted to resume at `fetch_lyrics`. (Confirmed 2026-09-09.)
- New dependencies (librosa-family already mostly present; mutagen/requests/
  syncedlyrics newly added) accepted. (Confirmed.)
- **Project identity: in-place rename (LyricVideoGen → PlayAlongVideoProduction),
  continuing this repo's own git/version history — not a separate fork.** (Confirmed
  2026-09-09, superseding the earlier same-day fork decision.)
- LyricVideoGen's own GitHub repo left untouched as a frozen pre-merge snapshot.
- AcoustID fingerprinting and Ultimate-Guitar online chords explicitly deferred/out of
  scope unless the owner asks for them later.
- Releases-repo renaming deferred to whenever a release is next actually cut.
