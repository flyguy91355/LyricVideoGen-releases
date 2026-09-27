# Whisper Hotwords for Alignment Anchoring -- Design

**Owner's problem:** Whisper (the speech recognizer that anchors the forced-alignment aligner) sometimes never
recognizes a word correctly anywhere in a song -- confirmed on "Boris the Spider," where it never once heard the
name "Boris" (transcribed as "Where is"/"God is"/"Who is" the spider throughout) and turned a fast repeated chorus
into a run of nonsense "b-b-b-b..." tokens. Whichever lines Whisper garbles get no anchor (or a weak one) for the
aligner, and the aligner can only interpolate blindly across the resulting gap -- which produced a real, audible
defect: one word ("crawly,") got an 8.3-SECOND duration, and the song's final word ("spider") got a 7.4-second
duration, both stretched to fill an anchor-less gap. Editing the Whisper Text review box after the fact (the
2026-09-27 feature) only fixes the SYNC-CHECK'S SCORE for an already-placed line; it carries no timing information,
so it cannot fix the word's actual placement in the video.

**Owner's direction (2026-09-27):** "I don't want to have to mess with these videos, I want the program to get it
right. [Editing after the fact] makes me do it" -- he wants the alignment to come out right the first time,
automatically, not a manual catch-and-correct workflow. Restated once the two-part shape below was first
proposed: "the lyrics it finds should be available to Whisper at the very beginning" -- confirming this must cover
every song's FIRST, automatic pass, not only a song the owner has already caught and corrected once.

**The mechanism:** faster-whisper (already a dependency, `faster-whisper>=1.0`, installed 1.2.1) supports a
`hotwords` parameter on `WhisperModel.transcribe()` -- confirmed by reading the installed library's own source
(`site-packages/faster_whisper/transcribe.py`), not assumed from memory: hotwords are tokenized once and prepended
to the decoding prompt for EVERY internal window of the transcription (`hotwords=options.hotwords` is threaded
through `generate_segments`'s own per-window loop), so a hint recurs throughout a whole song, not just its opening
seconds (unlike `initial_prompt`, which only affects the first window). The docstring: "Hotwords/hint phrases to
provide the model with. Has no effect if prefix is not None" (this project never passes `prefix`). Hotword tokens
are capped internally at `self.max_length // 2 - 1` = 223 tokens for the `medium` model (`self.max_length = 448`),
past which the hint is SILENTLY dropped, no warning. Measured against the owner's real library (2026-09-27
review): over half his songs' full lyrics exceed 223 tokens (median ~230; a dense rap track can run 1000+), so a
long song's later verses are not part of the hint at all -- a known, accepted limitation, not fixed by this spec
(see `lyric_hotwords()`'s own docstring). The hint still helps whatever DOES fit.

Giving Whisper the song's own real lyric vocabulary as hotwords lets it correctly recognize a word like "Boris"
where it is actually sung, instead of guessing a phonetically-similar phrase -- producing a REAL anchor for the
aligner to use, rather than papering over a bad one afterward.

**Circularity risk, found in the post-implementation whole-branch review (2026-09-27) and fixed before release:**
for a FETCHED (unconfirmed) candidate, hinting the SAME transcription that `_build_audio_check` uses to verify
that candidate primes Whisper toward the very text being judged -- the coverage/timing bars that check was built
to enforce were calibrated on UNHINTED transcripts, so a wrong or mismatched candidate becomes more likely to
falsely PASS the check that exists to catch it, not just occasionally slip through. This does not apply to Part 1
(the owner's own confirmed text needs no independent check) or to Part 2's ANCHOR use of the hint (a real anchor
for the aligner, not a verdict on which candidate is right) -- only to using the SAME hinted transcript for
BOTH purposes on an unconfirmed candidate. See "Part 2" below for the fix: verification and anchoring use two
separate transcriptions.

## Scope: two parts, both in scope, Part 1 lands first for the fastest possible proof it works

### Part 1 -- owner-edited lyrics

When the owner has typed/corrected a song's lyrics by hand (`Edit Lyrics`, saved to `lyrics_owner.txt`), the
confirmed real text is already a local variable (`owner_lines`) at the exact point `run_pipeline`'s fetch_lyrics
stage calls `transcribe_vocals(vocals_path, work_dir)` (`lyricvideo/pipeline.py`, the
`if owner_lines is not None:` branch, currently ~line 1381). This is the SAME transcription call that also caches
the transcript `pipeline._align_lyrics`/`timing_gate` use for anchoring and scoring, and that
`pipeline.whisper_lines_for` shows in the Whisper Text popup -- one hotword-informed transcription benefits all
three. Zero extra Whisper calls, zero extra cost: this is the SAME one call, just given a hint.

### Part 2 -- fetched lyrics, decoupled: unhinted verification, hinted anchoring

When lyrics come from an online source (lrclib/syncedlyrics/sidecar) and the owner never edits them, `_build_audio_check()`
transcribes the vocal stem and that transcript is what decides whether a candidate matches what is sung. This
transcription stays **permanently unhinted** -- exactly as before this feature existed -- so that independent
check can never be primed with the very candidate it is judging (see the circularity risk above).

Once `fetch_lyric_lines_verified()` returns an ACCEPTED `lines_text` (verified, or the least-bad candidate held
for review -- either way, this is what the song's lyrics ARE from here on), a SEPARATE, second transcription is
made, hinted with that accepted text, purely to give the aligner a real anchor:

```
lines_text, lyrics_source, lyrics_concern = fetch_lyric_lines_verified(
    audio_path, ..., audio_check=audio_check, reconcile=reconcile, arbiter=arbiter, times_out=times_out, **trim_kwargs,
)
# ... credit-line trimming happens here, unchanged ...
if owner_lines is None and lines_text:
    transcribe_vocals(vocals_path, work_dir, hotwords=lyric_hotwords(lines_text))
```

`transcribe_vocals`'s own cache is keyed by `hotwords` (see `lyricvideo/transcribe.py` below), so this second call
is a genuine cache MISS against the unhinted transcript `_build_audio_check` just wrote, and overwrites
`transcript.json` with the hinted version -- which is what the align stage's anchors
(`pipeline._align_lyrics`'s `load_transcript_words(work_dir)`) read afterward. This costs one EXTRA ~70 s Whisper
call for a fetched-lyrics song (not doubled for Part 1: the owner's one hinted call already serves both
purposes, since there is no independent check to keep separate from it). `lyric_hotwords([])` gives `""` --
i.e. no hint, i.e. today's exact behavior -- for the rare case `lines_text` is empty (nothing found anywhere).

An earlier draft of this design fetched a candidate cheaply (`fetch_lyric_lines()`, no audio, no Whisper call)
and hinted `_build_audio_check` with THAT before any candidate was verified, meaning to satisfy the owner's "the
lyrics it finds should be available to Whisper at the very beginning" -- the whole-branch review caught that this
is exactly the circularity risk described above, since it hints the same transcript the check then judges the
SAME hint's own source candidate against. The decoupled design above still satisfies the owner's real goal (no
manual catch-and-correct step, works on a song's first automatic pass) without that risk -- it just gets there on
a second pass, once a candidate is actually accepted, rather than by guessing beforehand.

## Part 1 design

### `lyricvideo/transcribe.py`

- New pure function `lyric_hotwords(lines: list[str]) -> str`: the given lines, exact-text deduplicated
  (case-insensitive, first occurrence kept, so "Boris the spider" appearing 6 times contributes once), joined with
  a single space, stripped. Blank/whitespace-only lines are dropped. An empty `lines` list gives `""`. No length
  capping here -- faster-whisper's own token truncation (see above) already bounds the worst case, and a song's
  own lyric vocabulary is nowhere near that limit.
- `transcribe_vocals(vocals_path, work_dir, model=None, hotwords: str = "")`: new keyword-only-by-convention
  parameter (positional-or-keyword like `model`, defaulting to `""` = no hint, today's exact behavior). Passed
  straight through to `model.transcribe(..., hotwords=hotwords or None)` (faster-whisper treats `""` and `None`
  the same internally via its own `if hotwords and not prefix` check, but passing `None` for an empty string is
  the more explicit choice and matches the parameter's own documented `Optional[str]` type). The cache write gains
  a `"hotwords": hotwords` field.
- `_read_cache(cache_path, vocals_size, language, hotwords)`: gains the `hotwords` parameter; the validity check
  gains `and data.get("hotwords", "") == hotwords` -- a cache written before this feature existed (no `"hotwords"`
  key at all) is treated as `hotwords=""`, so every existing song's cached transcript is reused unchanged UNLESS a
  real hotwords value is now being requested for it (an owner-edited song's next Redo), which correctly forces a
  fresh, hotword-informed transcription instead of silently reusing the old, unhinted one.
- `whisper_text_for(work_dir, model=None)` (used by the Whisper Text popup): unchanged in behavior for a song
  that already has a cached transcript (returns it as today); for a song needing a fresh transcription, it has no
  way to know the lyrics text on its own -- it stays hotwords-free (`hotwords=""`) at this call site. This is
  acceptable: it only fires for a rare song with no transcript at all yet, and the SAME work_dir's `align` stage
  runs `transcribe_vocals` with real hotwords first in the ordinary pipeline flow, so by the time a song is old
  enough to be in the review list, its cached transcript already has them.

### `lyricvideo/pipeline.py`

- Part 1: the `if owner_lines is not None:` branch's `transcribe_vocals(vocals_path, work_dir)` call becomes
  `transcribe_vocals(vocals_path, work_dir, hotwords=lyric_hotwords(owner_lines))`.
- Part 2: `_build_audio_check(vocals_path, work_dir)` is UNCHANGED (stays unhinted, permanently -- see the
  circularity risk above). After `fetch_lyric_lines_verified(...)` returns and the credit-line trim runs, in the
  `owner_lines is None and lines_text` case: `transcribe_vocals(vocals_path, work_dir,
  hotwords=lyric_hotwords(lines_text))`, wrapped in try/except like every other Whisper call site here (never
  stops the pipeline).

### Validation (required before either part is considered done, not optional polish)

`transcribe.py`'s own comments already warn that a Whisper-model-behavior change needs re-validating against the
owner's own already-judged songs before it can be trusted ("Any model change needs the gate re-validated on the
owner's judged songs first"). This checks ONE narrow thing -- whether a song's own TIMING SYNC SHARE gets worse --
not whether a song is flawless overall (many already have unrelated, pre-existing imperfections this never looks
at, confirmed with the owner, 2026-09-27). Concretely, before shipping either part:

1. A script (`scripts/revalidate_hotwords.py`) that: finds every `work/` song with saved word timings, whether
   owner-edited (Part 1) or fetched-and-accepted (Part 2) -- force-transcribes each one fresh with the new
   hotwords-aware call (a scratch, in-memory re-transcription; never overwrites the real `transcript.json`),
   hinted with that song's OWN currently-saved lyric lines (the decoupled design means this is the SAME hint
   source for both parts now: the accepted text, never a fresh network fetch), applies that song's own saved
   `whisper_owner.json` corrections to BOTH the before and after heard-word lists (matching what a real Redo's
   own re-score does -- comparing a corrected "before" against an uncorrected "after" would be apples to oranges),
   recomputes `timing_gate.check_sync`'s share against the EXISTING saved word placements (read-only -- this does
   not re-run alignment, only re-checks whether the NEW transcript still confirms the SAME already-placed words),
   and reports any song whose share DROPS versus its current, already-saved share, OR that could be scored before
   but not after (its own distinct "could not re-score" status, never silently folded into "no regression"). A
   drop or an unscorable-after on any song blocks shipping until understood; an improvement or no change is
   expected and fine. Note what this validation CANNOT see: it only catches a score getting WORSE, never a wrong
   candidate scoring INFLATED (the circularity risk above) -- that risk is addressed structurally, by keeping
   verification unhinted, not by this script.
2. Only after that report shows no regressions does the corresponding part ship (commit, push, release) --
   matching how every other change in this project is verified before being called done. This is a REAL cost (one
   ~70 s Whisper call per song checked), run once per part, not on every future change.

### Tests

- `tests/test_transcribe.py` (existing file): `lyric_hotwords` dedup/order/blank-handling unit tests;
  `transcribe_vocals` passes `hotwords` through to the injected fake model and into the cache; `_read_cache`
  treats a hotwords-less legacy cache entry as `hotwords=""` and invalidates on a real mismatch.
- `tests/test_pipeline.py`: the owner-edited-lyrics branch calls `transcribe_vocals` with
  `hotwords=lyric_hotwords(owner_lines)`; the fetched-lyrics branch's `_build_audio_check` call stays unhinted,
  and a SECOND `transcribe_vocals` call after `fetch_lyric_lines_verified` returns passes the accepted
  `lines_text` through as hotwords (fakes capturing kwargs, as this project's tests already do elsewhere in this
  file).
- `tests/test_verify_lyrics.py`: a song with an existing hinted `transcript.json` is left untouched by
  `verify_song`'s default (unhinted) `transcribe` callable -- fixes a real bug the review found, where
  `transcribe_vocals`'s hotwords-aware cache key made a routine re-check silently overwrite a hinted transcript
  with a fresh, worse, unhinted one.
- `tests/test_revalidate_hotwords.py`: the script's before/after scores both apply the same saved corrections;
  a song scoreable before but not after gets its own `unscorable_after` status rather than reading as "no
  regression".

## Explicitly out of scope for this spec

- Any change to the word-duration outlier cap already in `layout.py` (`_plausible_sung_intervals`,
  `max_word_duration=3.0`) -- that remains a render-time safety net for whatever the aligner produces; this spec
  aims to reduce how often the net is needed, not to remove it.
- Re-running this against every ALREADY-UPLOADED song automatically -- per the owner's standing rule
  (`feedback_keep_work_visible_doug_starts_runs`), any bulk re-Redo of already-live songs is his own call to
  start, not something to launch unprompted. The validation script (above) never writes to a real song's own
  files regardless.
