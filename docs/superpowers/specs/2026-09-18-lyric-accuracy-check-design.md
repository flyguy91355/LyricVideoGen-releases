# Lyric Accuracy Check + Review Queue — Design

**Date:** 2026-09-18
**Status:** Approved by owner (iterated live in chat), ready for implementation

## Purpose

Owner is about to batch-produce 100 songs unattended and wants a safety
net against a wrong lyrics match (wrong song/edition, garbled text, wrong
language) reaching YouTube unnoticed. Every song should be automatically
sanity-checked; anything that can't be confirmed accurate after trying
every real lyrics source available should render anyway (so the owner has
a real video to look at) but skip auto-upload and land in a review queue
instead.

## Scope

**In scope:**
1. A small Claude call, `check_lyric_accuracy()`, that reviews already-
   fetched lyric text against the song's title/artist and judges whether
   it looks like a real, correctly-matched, complete set of lyrics —
   flagging a wrong-song match, garbled/corrupted text, wrong language, or
   suspicious incompleteness. This evaluates already-fetched text; it does
   not ask Claude to reproduce full copyrighted lyrics as a ground truth.
2. The fetch_lyrics stage tries up to 6 real, distinct sources in order —
   sidecar file, lrclib.net direct query, then `syncedlyrics` against
   Musixmatch, NetEase, Megalobiz, and Genius individually (confirmed via
   `syncedlyrics.search(..., providers=[name])`) — running the accuracy
   check after each, stopping at the first one that passes. If none pass,
   the best (first non-empty) candidate is kept and flagged, never
   blocking generation.
3. `Song` gains `lyrics_source` and `lyrics_accuracy_concern` (blank =
   clean), persisted through `lyrics_timed.json` same as everything else
   Song already carries.
4. A flagged song never auto-uploads (checked in `_maybe_upload_to_youtube`
   and filtered out of the automatic quota-cooldown retry), but still
   fully renders. A new "Flagged for Lyrics Review" GUI list (same
   collapsible/lazy pattern as the other song lists) shows the concern
   text with two actions: **Redo** (reuses the existing Redo flow
   verbatim, which re-runs fetch_lyrics fresh through the same multi-
   source check) and **Upload Anyway** (reuses the existing manual retry-
   upload path for that one song, bypassing the flag deliberately). A
   song drops off this list once it's either uploaded or a later
   fetch clears the concern — no separate "dismiss" action needed.

**Out of scope / explicit limits:**
- This is not a guarantee of word-for-word accuracy. It catches wrong-
  song matches and garbled/incomplete text well; it cannot verify exact
  correctness the way a human proofreading against a lyrics sheet could.
- No new Settings field for the source list or retry cap — matches the
  existing precedent (`_MAX_GENERATION_ATTEMPTS` for image retries) of a
  fixed internal constant, not an owner-tunable slider, since the source
  list is a fixed technical capability (there are only 5 real
  `syncedlyrics` providers plus lrclib-direct and the sidecar file).

## Components

- **`lyricvideo/lyric_accuracy.py`** (new): `check_lyric_accuracy(anthropic_client,
  title, artist, lyric_lines, model="claude-sonnet-5") -> tuple[bool, str]`.
- **`lyricvideo/fetch_lyrics.py`**: `_fetch_syncedlyrics_hit` gains an optional
  `providers` passthrough; new `fetch_lyric_lines_verified(audio_path, title,
  artist, duration, alt_titles, anthropic_client, model=...) -> tuple[list[str],
  str, str]` (lines, source, concern) tries the 6-source list. Existing
  `fetch_lyric_lines` (single-shot, no check) stays as-is for anything
  still exercising it directly in tests.
- **`lyricvideo/models.py`**: `Song.lyrics_source`/`Song.lyrics_accuracy_concern`,
  wired through `_song_from_dict` tolerantly (default `""` for legacy
  files, same pattern as every other optional Song field).
- **`lyricvideo/pipeline.py`**: fetch_lyrics stage calls the verified
  fetch and writes `{"lines": [...], "source": ..., "concern": ...}` to
  `lyric_lines.json` (tolerant read of the old plain-list format too);
  align stage carries `source`/`concern` into the `Song` it builds. New
  `list_flagged_songs(work_root) -> list[str]`, same shape as
  `list_pending_uploads` plus the concern check, naturally excluding an
  already-uploaded song so the list shrinks on its own once handled.
- **`lyricvideo/gui.py`**: `_maybe_upload_to_youtube` skips auto-upload for
  a flagged song; `_retry_pending_uploads_if_due` filters flagged slugs out
  of what it auto-retries (a MANUAL retry/upload click is a deliberate
  owner override and stays unfiltered, matching the app's existing
  manual-override precedent). New "Flagged for Lyrics Review" panel; its
  Redo button just sets `redo_song_var` and calls the existing `_on_redo`
  handler, its Upload Anyway button reuses the existing
  `_start_retry_upload([slug])` path — no new upload/redo mechanics.

## Testing

Same conventions as everywhere else in this codebase: `check_lyric_accuracy`
tested against a fake Anthropic client; `fetch_lyric_lines_verified` tested
by monkeypatching each per-source fetch function and the accuracy checker
to control which source "wins"; pipeline-level tests cover the legacy-
plain-list read path; GUI tests follow the existing stub-based pattern for
the new panel's Redo/Upload Anyway handlers and the auto-retry filter.
