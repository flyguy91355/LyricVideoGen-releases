# Verify Lyrics Against the Audio (Whisper) — Design

**Date:** 2026-09-19
**Status:** Approved by owner in chat ("whisper plus the lyrics file ... go ahead and build this fix and test it")

## Problem

Some finished videos show lyrics that are not the correct lyrics in the correct
order for the recording (a different edition/arrangement, verses or choruses in a
different order, a wrong stretch). The forced aligner (`align.py`) cannot notice:
given ANY text it squeezes it onto the audio without error. The 2026-09-18
accuracy check (`lyric_accuracy.py`) only asks Claude whether the text looks like
real lyrics for that title/artist; it never hears the audio, and it stops at the
first source that passes (of 53 songs made since, 50 stopped at lrclib, and
nothing was ever flagged).

## Design

1. **Transcribe the isolated vocal stem** with `faster-whisper` (CPU, int8, model
   `medium`; ~70 s a song on the owner's 4-core machine, no usable GPU). Result cached in `work_dir/transcript.json`. Module:
   `lyricvideo/transcribe.py`.
2. **Score any candidate lyric text against that transcript** —
   `lyricvideo/lyric_audio_match.py`, pure logic (no I/O). In-order word matching
   (difflib), per-line support, a repeated line counts as supported when an
   earlier identical line is (Whisper collapses repeated lines), plus the longest
   run of consecutive unsupported lines. A candidate PASSES only if overall
   coverage (>= 70%), the worst run of unmatched lines (<= 3) and the longest run of sung
   words no lyric explains (<= 12) are all within bounds.
3. **Use it as the source gate.** `fetch_lyric_lines_verified()` takes an optional
   `audio_check`; when given, a source passes on the audio match alone (the Claude
   text check is the fallback used only when no transcript could be made). It
   still tries sources in order, but "passes" now means "matches what is sung".
   If none pass, the source with the best audio match is kept and flagged, with a
   concern that names the unmatched line numbers. Flagged songs already skip
   auto-upload and appear in "Flagged for Lyrics Review" (unchanged machinery).
4. **Degrades safely:** if Whisper cannot load or run (package missing, model
   download blocked), the pipeline logs a warning and uses the previous Claude
   text check, never failing the song.

## Out of scope (possible follow-ups)

- Automatically repairing an unmatched stretch from the transcript.
- A Settings field for model size/thresholds (module constants for now).

## Testing

- Unit tests for the matcher with crafted transcripts: correct text passes;
  repeated choruses pass; a swapped verse/chorus, a block of another song's
  lines, and changed words each fail; localisation of the bad lines.
- Fake-model tests for transcription + cache reuse.
- `fetch_lyric_lines_verified` tests: picks the audio-matching source over an
  earlier non-matching one; keeps and flags the best when none match; falls back
  when the audio check is unavailable.
- Calibration and controlled-corruption runs against real songs in `work/`.

## Findings while building it (each has a regression test)

Measured on the owner's real songs, not assumed:

1. **Whisper's speech VAD deletes singing.** With the default `vad_filter=True`,
   "Like a Prayer" produced 61 words (of ~660), so correct lyrics scored 8%.
   Sung vocals are transcribed with the VAD off and no temperature-fallback retries
   (`temperature=0`): 45 s instead of 267 s for a 6-minute song.
2. **Language auto-detect misfires.** "Billie Jean" was heard as Portuguese and
   transcribed in Portuguese (19%). The language is forced to English
   (`LYRICVIDEO_WHISPER_LANGUAGE` overrides; `auto` restores detection).
3. **`small` cannot hear loud rock vocals; `medium` can, mostly.** On six rejected rock
   songs `small` scored 19-62% on lyrics that were correct (it loops on "wild, wild,
   wild"); `medium` scored 53-78%. `medium` is the default (`LYRICVIDEO_WHISPER_MODEL`).
4. **Common words match by chance.** Lyrics for a different song still "matched" 35% via
   the/you/a, so scoring uses content words only.
5. **Backing vocals and vocalizations** ("(Just like a prayer)", "whoa oh-oh") are not
   required to be heard; they were flagging songs whose lead vocal matched.
6. **A missing verse is invisible from the lyric side** (all remaining lines still
   match), so the check also measures the longest run of SUNG content words no lyric line
   explains: 0-3 on correct songs, 13-21 with a 6-line verse removed.

## Limits (be honest about what this can and cannot certify)

- It cannot prove lyrics are word-perfect: a recognizer that is itself imperfect on sung
  words can only catch what differs enough to show, chiefly wrong editions, wrong or
  missing sections, and wrong order. A single wrong word inside a matching line passes.
- A song whose vocals Whisper cannot hear well (loud rock, fade-out ad-lib vamps) cannot be
  verified either way and is held for review, which is the intended failure direction.
- A displaced chorus inside a run of identical choruses is not detectable.
