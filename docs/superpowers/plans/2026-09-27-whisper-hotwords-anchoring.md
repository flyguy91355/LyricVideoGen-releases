# Whisper Hotwords for Alignment Anchoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Whisper the song's own real lyric vocabulary as a "hotwords" hint on the one transcription call the
fetch_lyrics stage already makes -- for an owner-edited song (Part 1), the owner's own confirmed text; for every
OTHER song, on its very first, fully automatic pass (Part 2), lyrics fetched cheaply (no audio, no Whisper call)
BEFORE that transcription runs -- so a word like "Boris" (never once recognized correctly in "Boris the Spider")
gets a real anchor for the aligner instead of a guess. Fixes the alignment itself, not just the sync-check's score,
and needs no owner action first, per the owner's own direction (2026-09-27): "the lyrics it finds should be
available to Whisper at the very beginning."

**Architecture:** A new pure function (`lyric_hotwords`) turns a list of lyric lines into a deduplicated hotwords
string; `transcribe_vocals` gains an optional `hotwords` parameter threaded straight into faster-whisper's own
`hotwords` argument and into the cache signature (so a cache written before this feature, or for a different
hotwords value, is correctly invalidated). Two call sites pass it through: the owner-edited-lyrics branch (Part 1,
using the owner's own confirmed lines) and `_build_audio_check` (Part 2, using a fresh, cheap `fetch_lyric_lines()`
lookup run immediately before it -- the same plain sidecar/lrclib/syncedlyrics lookup `fetch_lyric_lines_verified`
already uses internally, just called once more, before any Whisper call, purely for its text). A read-only
validation script then re-checks every song with saved word timings -- owner-edited or not -- for a score
regression before this is considered shippable.

**Tech Stack:** Python 3.11, faster-whisper 1.2.1 (already installed), pytest.

**Spec:** `docs/superpowers/specs/2026-09-27-whisper-hotwords-anchoring-design.md`

## Global Constraints

- `hotwords` defaults to `""` everywhere -- a caller that does not pass it gets EXACTLY today's behavior (no
  hotwords, same cache keys for every already-cached song that never used one).
- A cache file written before this feature (no `"hotwords"` key at all) must be treated as `hotwords=""` on read,
  so no already-cached song is force-re-transcribed by this change alone.
- Both branches of `run_pipeline`'s fetch_lyrics stage change behavior in this plan: the `owner_lines is not None`
  branch (Part 1, Task 3) and the fetched-lyrics `else:` branch via `_build_audio_check` (Part 2, Task 4).
- `fetch_lyric_lines()` (the cheap, no-audio hint lookup Part 2 adds a call to) must be stubbed in
  `tests/test_pipeline.py`'s shared `_patch_common` fixture -- it now runs unconditionally in every pipeline test
  that reaches the fetched-lyrics branch (nearly all of them), and an unstubbed run would make a real network call
  from the test suite.
- Nothing in this plan re-runs any already-uploaded song's pipeline; the validation script (Task 5) is read-only
  except for its own scratch re-transcription, and does not touch any song's `lyrics_timed.json`.
- Full test suite must pass after every task, not just the file touched.

## Review Focus

- A song whose `lyrics_timed.json` was never touched by an owner edit (the overwhelming majority) must transcribe
  identically to before when nothing is fetchable -- covered by Task 1's default-empty-hotwords test and Task 4's
  `_patch_common`-stubbed-to-`[]` default across every pre-existing pipeline test.
- A owner-lines list containing blank/whitespace-only entries (a stray blank line in an edit) must not poison the
  hotwords string with empty tokens -- covered by Task 1's blank-line test.
- A cache file from before this feature existed (genuinely missing the `"hotwords"` key, not just set to `""`)
  must still be reused, not force a needless re-transcription of every existing song -- covered by Task 2's
  legacy-cache test.
- Two songs whose lyrics happen to share a repeated line (e.g. two different songs both containing "oh oh oh")
  must each get hotwords built from THEIR OWN lyrics only, never leaking between calls (pure function, no shared
  state) -- covered by Task 1's own function being stateless (no module-level mutable state), verified by a test
  calling it twice with different inputs in the same process.
- An existing test that overrides `transcribe_vocals` with a lambda accepting only its two positional parameters
  (no `**kwargs`) would break the instant a caller passes `hotwords=` as a keyword -- covered by Task 4's fix to
  `test_run_pipeline_gives_the_lyrics_fetch_an_audio_check_built_from_the_transcript`, the one such test in the
  file.
- The validation script must not silently "pass" a song it could not actually score (e.g. one with too few judged
  lines) as a false negative for "no regression" -- covered by Task 5's own test of that exact case.

---

### Task 1: `lyric_hotwords()` -- turn lyric lines into a Whisper hint

**Files:**
- Modify: `lyricvideo/transcribe.py`
- Test: `tests/test_transcribe.py`

**Interfaces:**
- Produces: `lyric_hotwords(lines: list[str]) -> str` -- exact-text deduplicated (case-insensitive comparison,
  first occurrence's original casing kept), blank/whitespace-only lines dropped, joined with a single space,
  stripped. `lyric_hotwords([])` returns `""`.

- [ ] **Step 1: Write the failing tests**

```python
def test_lyric_hotwords_dedupes_repeated_lines_keeping_first_casing():
    from lyricvideo.transcribe import lyric_hotwords

    hotwords = lyric_hotwords(["Boris the spider", "Black and hairy", "Boris the spider", "Boris the spider"])

    assert hotwords == "Boris the spider Black and hairy"


def test_lyric_hotwords_dedupes_case_insensitively():
    from lyricvideo.transcribe import lyric_hotwords

    assert lyric_hotwords(["Boris the spider", "boris the spider"]) == "Boris the spider"


def test_lyric_hotwords_drops_blank_lines():
    from lyricvideo.transcribe import lyric_hotwords

    assert lyric_hotwords(["Boris the spider", "", "   ", "Black and hairy"]) == "Boris the spider Black and hairy"


def test_lyric_hotwords_of_nothing_is_blank():
    from lyricvideo.transcribe import lyric_hotwords

    assert lyric_hotwords([]) == ""


def test_lyric_hotwords_has_no_shared_state_between_calls():
    from lyricvideo.transcribe import lyric_hotwords

    first = lyric_hotwords(["Song one's own line"])
    second = lyric_hotwords(["A completely different song's line"])

    assert first == "Song one's own line"
    assert second == "A completely different song's line"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_transcribe.py -q -k lyric_hotwords`
Expected: FAIL -- `ImportError: cannot import name 'lyric_hotwords' from 'lyricvideo.transcribe'`

- [ ] **Step 3: Implement `lyric_hotwords`**

Add to `lyricvideo/transcribe.py`, above `transcribe_vocals`:

```python
def lyric_hotwords(lines: list[str]) -> str:
    """The given lyric lines turned into a Whisper `hotwords` hint (owner, 2026-09-27): exact-text deduplicated
    (case-insensitive, first occurrence's own casing kept -- a repeated chorus line contributes once), blank lines
    dropped, joined with a single space. Lets Whisper correctly recognize a word it would otherwise guess at
    (confirmed real case: it never once heard "Boris" in "Boris the Spider") wherever it recurs in the song --
    faster-whisper threads `hotwords` into every internal decoding window, not just the first, and caps it at half
    the model's own max length, comfortably larger than a song's lyric vocabulary (verified directly against the
    installed faster-whisper 1.2.1 source, not assumed)."""
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        key = stripped.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(stripped)
    return " ".join(kept)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_transcribe.py -q -k lyric_hotwords`
Expected: PASS (5/5)

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/transcribe.py tests/test_transcribe.py
git commit -m "transcribe.py: add lyric_hotwords(), turning lyric lines into a Whisper hint"
```

---

### Task 2: thread `hotwords` through `transcribe_vocals` and its cache

**Files:**
- Modify: `lyricvideo/transcribe.py`
- Test: `tests/test_transcribe.py`

**Interfaces:**
- Consumes: `lyric_hotwords` (Task 1) -- used only by tests here, not by this function itself.
- Produces: `transcribe_vocals(vocals_path, work_dir, model=None, hotwords: str = "") -> str` (new keyword
  parameter; every existing caller, none of which pass it yet, is unaffected). `_read_cache` gains a `hotwords`
  parameter with the same default-compatible semantics.

- [ ] **Step 1: Write the failing tests**

```python
def test_hotwords_are_passed_to_the_model():
    class _CapturingModel(_FakeModel):
        def transcribe(self, path, **kwargs):
            self.seen_hotwords = kwargs.get("hotwords")
            return super().transcribe(path, **kwargs)

    model = _CapturingModel([" hello there"])
    transcribe_vocals(_vocals(tmp_path := __import__("pathlib").Path(__file__).parent), tmp_path, model=model, hotwords="Boris the spider")

    assert model.seen_hotwords == "Boris the spider"


def test_hotwords_are_saved_in_the_cache(tmp_path):
    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_FakeModel([" hello there"]), hotwords="Boris the spider")

    cached = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert cached["hotwords"] == "Boris the spider"


def test_a_cache_made_with_different_hotwords_is_not_reused(tmp_path):
    vocals = _vocals(tmp_path)
    transcribe_vocals(vocals, tmp_path, model=_FakeModel([" hello there"]), hotwords="Boris the spider")

    result = transcribe_vocals(vocals, tmp_path, model=_FakeModel([" a fresh transcription"]), hotwords="different words")

    assert result == "a fresh transcription"


def test_a_cache_made_with_no_hotwords_is_reused_when_none_are_requested_again(tmp_path):
    vocals = _vocals(tmp_path)
    transcribe_vocals(vocals, tmp_path, model=_FakeModel([" hello there"]))

    class _MustNotRun:
        def transcribe(self, *a, **k):
            raise AssertionError("cache should have been reused")

    assert transcribe_vocals(vocals, tmp_path, model=_MustNotRun()) == "hello there"


def test_a_legacy_cache_with_no_hotwords_key_at_all_is_reused_when_none_are_requested(tmp_path):
    """A transcript.json written before this feature existed has no "hotwords" key at all -- must be treated as
    hotwords="", not as a mismatch that forces every existing song to re-transcribe for nothing."""
    vocals = _vocals(tmp_path)
    transcribe_vocals(vocals, tmp_path, model=_FakeModel([" hello there"]))
    cache_path = tmp_path / "transcript.json"
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    del data["hotwords"]
    cache_path.write_text(json.dumps(data), encoding="utf-8")

    class _MustNotRun:
        def transcribe(self, *a, **k):
            raise AssertionError("a legacy cache with no hotwords key must still be reused")

    assert transcribe_vocals(vocals, tmp_path, model=_MustNotRun()) == "hello there"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_transcribe.py -q -k hotwords`
Expected: FAIL -- `TypeError: transcribe_vocals() got an unexpected keyword argument 'hotwords'`

- [ ] **Step 3: Implement**

In `lyricvideo/transcribe.py`, change `_read_cache` and `transcribe_vocals`:

```python
def _read_cache(cache_path: Path, vocals_size: int, language: str | None, hotwords: str) -> str | None:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            data["model"] == _model_name()
            and data["vocals_bytes"] == vocals_size
            and data["language_requested"] == (language or "auto")
            and data.get("word_timestamps") is True   # caches from before word timings existed are redone
            and data.get("hotwords", "") == hotwords   # a cache from before hotwords existed reads as hotwords=""
        ):
            return str(data["text"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def transcribe_vocals(vocals_path: Path, work_dir: Path, model=None, hotwords: str = "") -> str:
    """The words heard in `vocals_path`, as one lowercase-insensitive string. `model` is
    injectable (tests pass a fake with the same .transcribe() shape). `hotwords` (owner, 2026-09-27,
    see lyric_hotwords()) hints Whisper toward the song's own real vocabulary throughout the whole
    transcription -- "" (the default) is today's exact behavior."""
    vocals_path = Path(vocals_path)
    if not vocals_path.exists():
        raise FileNotFoundError(f"Vocal stem not found: {vocals_path}")
    vocals_size = vocals_path.stat().st_size
    cache_path = Path(work_dir) / _TRANSCRIPT_FILE

    language = _language()
    cached = _read_cache(cache_path, vocals_size, language, hotwords)
    if cached is not None:
        return cached

    model = model or _load_model()
    segments, info = model.transcribe(
        str(vocals_path), language=language, vad_filter=False, temperature=0.0,
        condition_on_previous_text=False, beam_size=1, word_timestamps=True, hotwords=hotwords or None,
    )
    segments = [s for s in segments if s.text.strip()]
    text = " ".join(s.text.strip() for s in segments)
    words = [
        {"word": w.word.strip(), "start": float(w.start), "end": float(w.end)}
        for s in segments for w in (getattr(s, "words", None) or []) if w.word.strip()
    ]
    cache_path.write_text(
        json.dumps({
            "model": _model_name(),
            "vocals_bytes": vocals_size,
            "language_requested": language or "auto",
            "language": getattr(info, "language", ""),
            "text": text,
            "word_timestamps": True,
            "hotwords": hotwords,
            "words": words,
            "segments": [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments],
        }),
        encoding="utf-8",
    )
    return text
```

(This is the existing function body with `hotwords` added to the signature, threaded into the `model.transcribe`
call, and added to the cache dict -- every other line is unchanged from the current file.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_transcribe.py -q`
Expected: PASS (every test in the file, old and new)

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/transcribe.py tests/test_transcribe.py
git commit -m "transcribe.py: thread hotwords through transcribe_vocals and its cache signature"
```

---

### Task 3: pass the owner's confirmed lyrics as hotwords in the fetch_lyrics stage

**Files:**
- Modify: `lyricvideo/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `transcribe.lyric_hotwords` (Task 1), `transcribe_vocals(..., hotwords=...)` (Task 2).

- [ ] **Step 1: Write the failing test**

In `tests/test_pipeline.py`, right after the existing
`test_the_owners_lyrics_still_get_whisper_word_timings_for_the_aligner` (search for that name -- it is in the
"the owner's own edited lyrics (2026-09-19)" section), add:

```python
def test_the_owners_own_lyrics_are_used_as_whisper_hotwords(tmp_path, monkeypatch):
    """Real incident, 2026-09-27 ("Boris the Spider"): Whisper never once heard the word "Boris" anywhere in the
    song, so the aligner had no real anchor for those lines and produced an 8+ second single-word duration filling
    the gap. Hinting Whisper with the owner's own confirmed text lets it recognize the word correctly instead."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "lyrics_owner.txt").write_text("Boris the spider\nBoris the spider\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: calls.append(k) or "words")

    run_pipeline(Path("audio.mp3"), work_dir)

    assert calls == [{"hotwords": "Boris the spider"}]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q -k whisper_hotwords`
Expected: FAIL -- `assert [] == [{'hotwords': 'Boris the spider'}]` (today's call passes no keyword arguments at
all, so `calls` collects an empty dict each time, not the hotwords one).

- [ ] **Step 3: Implement**

In `lyricvideo/pipeline.py`, change the `owner_lines is not None` branch (around line 1381):

```python
            try:
                transcribe_vocals(vocals_path, work_dir, hotwords=lyric_hotwords(owner_lines))
```

(Only this one line changes -- `transcribe_vocals(vocals_path, work_dir)` becomes
`transcribe_vocals(vocals_path, work_dir, hotwords=lyric_hotwords(owner_lines))`.)

Add `lyric_hotwords` to `pipeline.py`'s existing transcribe import line: `from .transcribe import
load_transcript_segments, load_transcript_text, load_transcript_words, transcribe_vocals` gains `lyric_hotwords` in
that same list.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q -k whisper_hotwords`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/pipeline.py tests/test_pipeline.py
git commit -m "pipeline.py: hint Whisper with the owner's own confirmed lyrics as hotwords"
```

---

### Task 4: hint Whisper with cheaply-fetched lyrics for every OTHER song, on its first automatic pass

**Files:**
- Modify: `lyricvideo/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `transcribe.lyric_hotwords` (Task 1, already imported by Task 3), `transcribe_vocals(...,
  hotwords=...)` (Task 2), `fetch_lyrics.fetch_lyric_lines(audio_path, title, artist, duration, alt_titles=None) ->
  list[str]` (already exists, unmodified by this plan).
- Produces: `_build_audio_check(vocals_path, work_dir, hotwords: str = "") -> Callable | None` (new keyword
  parameter; every existing caller -- only the fetched-lyrics branch itself -- is updated in this task).

This is the case the owner named directly (2026-09-27): "the lyrics it finds should be available to Whisper at
the very beginning" -- a song that has never been touched by hand, on its very first Generate.

- [ ] **Step 1: Write the failing test, and stub the new lookup everywhere else so no test makes a real network call**

In `tests/test_pipeline.py`'s `_patch_common` (around line 43, right after the existing
`fetch_lyric_lines_verified` stub), add:

```python
    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines", lambda *a, **k: [])
    # The new (2026-09-27) cheap hint lookup Part 2 adds before _build_audio_check -- stubbed to "nothing found"
    # here so every pipeline-wiring test above (which does not care about this) makes no real network call; the
    # hotwords-specific test below overrides this to prove the hint actually reaches transcribe_vocals.
```

Then add, right after `test_the_owners_own_lyrics_are_used_as_whisper_hotwords` (Task 3's test):

```python
def test_the_fetched_hint_lyrics_are_used_as_whisper_hotwords_for_the_audio_check(tmp_path, monkeypatch):
    """Part 2, real owner direction (2026-09-27): even a song the owner has never touched gets a hinted
    transcription on its FIRST automatic pass -- fetch_lyric_lines() (cheap: sidecar/lrclib/syncedlyrics text only,
    no audio, no Whisper call of its own) runs before _build_audio_check's own transcription and its result hints
    that transcription, exactly like the owner-edited branch (Task 3) but sourced from a fresh online lookup
    instead of the owner's own confirmed text."""
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines",
        lambda *a, **k: ["Boris the spider", "Boris the spider"],
    )
    calls = []
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: calls.append(k) or "words")

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert calls == [{"hotwords": "Boris the spider"}]
```

Also fix the ONE existing test whose `transcribe_vocals` override cannot tolerate the new `hotwords=` keyword --
`test_run_pipeline_gives_the_lyrics_fetch_an_audio_check_built_from_the_transcript` (search for that name), change:

```python
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda vocals, work_dir: "pale morning harbor lantern")
```

to:

```python
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda vocals, work_dir, **k: "pale morning harbor lantern")
```

(Every other existing override in the file already accepts `**k`/`**kwargs` and needs no change.)

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q -k fetched_hint_lyrics_are_used`
Expected: FAIL -- `AttributeError: <module 'lyricvideo.pipeline' ...> does not have the attribute
'fetch_lyric_lines'` (raised inside `_patch_common`'s own new `monkeypatch.setattr` line, since `pipeline.py`
does not import `fetch_lyric_lines` yet -- this is the correct failure: it shows the wiring is missing, not a
typo in the test).

- [ ] **Step 3: Implement**

In `lyricvideo/pipeline.py`, change the import at line 29:

```python
from .fetch_lyrics import fetch_lyric_lines, fetch_lyric_lines_verified
```

Change `_build_audio_check` (around line 706):

```python
def _build_audio_check(vocals_path: Path, work_dir: Path, hotwords: str = ""):
    """A function scoring candidate lyric lines against what Whisper hears in the
    vocal stem (lyric_audio_match.py), or None when the transcription can't run --
    then the lyrics fetch quietly falls back to the older text-only check, so a
    missing package or blocked model download never stops a song from being made.
    `hotwords` (owner, 2026-09-27) hints this transcription toward lyrics fetched cheaply moments earlier, before
    any candidate has been verified -- see lyric_hotwords()/fetch_lyric_lines()."""
    print("Listening to the vocals to check the lyrics against what is sung (about a minute)...")
    try:
        heard = transcribe_vocals(vocals_path, work_dir, hotwords=hotwords)
    except Exception as e:
        print(
            f"WARNING: could not check the lyrics against the audio ({type(e).__name__}: {e}); "
            "using the text-only check instead.", file=sys.stderr,
        )
        return None
    print(f"Heard {len(heard.split())} words in the vocals; checking the lyric sources against them...")
    return lambda lines: score_lyrics_against_transcript(lines, heard)
```

Change the fetched-lyrics `else:` branch of the fetch_lyrics stage (around line 1387):

```python
        else:
            lyrics_anthropic_client = anthropic.Anthropic()
            times_out: dict = {}
            hint_lines = fetch_lyric_lines(
                audio_path, info_data["title"], info_data["artist"], info_data["duration"],
                info_data.get("alt_titles"),
            )
            audio_check = _build_audio_check(vocals_path, work_dir, hotwords=lyric_hotwords(hint_lines))
```

(Only these two lines are new/changed inside the `else:` branch -- `times_out: dict = {}` stays where it is, and
every line from `reconcile = _build_reconcile(...)` onward is unchanged.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q`
Expected: PASS (every test in the file, old and new)

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/pipeline.py tests/test_pipeline.py
git commit -m "pipeline.py: hint Whisper with cheaply-fetched lyrics on every song's first automatic pass"
```

---

### Task 5: validation script -- prove no song regresses, owner-edited or not

**Files:**
- Create: `scripts/revalidate_hotwords.py`
- Test: `tests/test_revalidate_hotwords.py`

**Interfaces:**
- Consumes: `fetch_lyrics.fetch_lyric_lines` (Part 2's hint source for a non-owner song), `transcribe.lyric_hotwords`.
- Produces: `find_owner_lyrics_songs(work_root: Path) -> list[Path]` (every song dir under `work_root` whose
  `lyrics_timed.json` has `lyrics_source == "owner"`, Part 1); `find_fetched_lyrics_songs(work_root: Path) ->
  list[Path]` (every OTHER song dir with at least one lyric line that has word timings, Part 2);
  `revalidate_one(song_dir: Path, model=None) -> dict` -- read-only except for a scratch re-transcription:
  re-transcribes the vocal stem fresh with the hint that song's OWN real code path would build (the saved lyric
  line texts for an owner-edited song; a fresh `fetch_lyric_lines()` lookup, exactly matching Task 4's own call,
  for any other song), computes `timing_gate.check_sync` against the SONG'S OWN ALREADY-SAVED `lyrics_timed.json`
  word placements (unchanged -- this does not re-run alignment), and returns `{"song": <slug>, "kind":
  "owner"|"fetched", "before_share": <float|None>, "after_share": <float|None>, "regressed": bool}` where
  `regressed` is `True` only when both shares are real numbers and `after_share < before_share` (a song that could
  not be scored either time is reported, never silently treated as "no regression").
- `main()` (CLI): runs both finders, `revalidate_one` on each result, prints a table, and exits 1 if any song
  regressed (else 0). Never writes to any song's own files.

This checks ONE narrow thing -- whether a song's own timing sync share gets worse -- not whether a song is
flawless overall; many already have unrelated, pre-existing imperfections this never looks at (confirmed with the
owner, 2026-09-27).

- [ ] **Step 1: Write the failing tests**

```python
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo.models import ChordTrack, LyricLine, Song, Word, save_song


def _make_owner_song(work_dir, lines_words, source="owner", title="T", artist="A", duration=12.0, alt_titles=None):
    """A song directory with a real lyrics_timed.json, a real (empty) vocal-stem file at exactly the path
    load_redo_inputs()/whisper_text_for() resolve for audio_path="a.mp3" (no local audio copy exists, so
    load_redo_inputs falls back to the bare "a.mp3" -- .stem == "a"), and a real song_info.json -- read by a
    NON-owner song's own hint-building (revalidate_one's Part 2 path), matching pipeline.py's fetched-lyrics
    branch, which reads title/artist/duration/alt_titles from exactly this file."""
    work_dir.mkdir(parents=True)
    lines = [LyricLine(words=[Word(w, s, e) for w, s, e in words]) for words in lines_words]
    save_song(Song(title=title, audio_path="a.mp3", lines=lines, lyrics_source=source, chord_track=ChordTrack()),
              work_dir / "lyrics_timed.json")
    (work_dir / "song_info.json").write_text(json.dumps({
        "title": title, "artist": artist, "duration": duration, "alt_titles": alt_titles,
    }), encoding="utf-8")
    vocals_path = work_dir / "htdemucs" / "a" / "vocals.wav"
    vocals_path.parent.mkdir(parents=True)
    vocals_path.touch()


def test_find_owner_lyrics_songs_finds_only_owner_sourced_songs(tmp_path):
    from scripts.revalidate_hotwords import find_owner_lyrics_songs

    _make_owner_song(tmp_path / "work" / "owner-song", [[("hello", 0.0, 0.4)]])
    _make_owner_song(tmp_path / "work" / "fetched-song", [[("hello", 0.0, 0.4)]], source="lrclib")

    found = find_owner_lyrics_songs(tmp_path / "work")

    assert [p.name for p in found] == ["owner-song"]


def test_find_fetched_lyrics_songs_finds_only_non_owner_sourced_songs_with_word_timings(tmp_path):
    from scripts.revalidate_hotwords import find_fetched_lyrics_songs

    _make_owner_song(tmp_path / "work" / "owner-song", [[("hello", 0.0, 0.4)]])
    _make_owner_song(tmp_path / "work" / "fetched-song", [[("hello", 0.0, 0.4)]], source="lrclib")

    found = find_fetched_lyrics_songs(tmp_path / "work")

    assert [p.name for p in found] == ["fetched-song"]


class _FakeModel:
    """transcribe() returns exactly the given (word, start, end) triples as one segment's word timings."""

    def __init__(self, words):
        self.words = words

    def transcribe(self, path, **kwargs):
        words = [SimpleNamespace(word=w, start=s, end=e) for w, s, e in self.words]
        seg = SimpleNamespace(start=self.words[0][1], end=self.words[-1][2], text=" ".join(w for w, _, _ in self.words), words=words)
        return [seg], SimpleNamespace(language="en")


def _write_transcript(work_dir, words):
    """The song's EXISTING transcript.json (built without hotwords, in real use) -- revalidate_one's "before" score
    comes from this file, exactly as it would for a real already-processed song (whisper_text_for's own docstring:
    every song this script scans already has one)."""
    (work_dir / "transcript.json").write_text(json.dumps({
        "model": "medium", "vocals_bytes": 1, "language_requested": "en", "language": "en", "text": "",
        "word_timestamps": True, "hotwords": "", "segments": [],
        "words": [{"word": w, "start": s, "end": e} for w, s, e in words],
    }), encoding="utf-8")


def test_revalidate_one_reports_no_regression_when_the_score_holds_or_improves(tmp_path):
    from scripts.revalidate_hotwords import revalidate_one

    song_dir = tmp_path / "some-song"
    # 8 identical two-word lines, 8 s apart, so no line hears its neighbour -- enough judged lines to score.
    lines_words = [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)] for i in range(8)]
    _make_owner_song(song_dir, lines_words)
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)                                     # "before": already agrees perfectly
    fake = _FakeModel(flat)                                               # "after" (fresh, with hotwords): the same

    result = revalidate_one(song_dir, model=fake)

    assert result["kind"] == "owner"
    assert result["before_share"] == 1.0 and result["after_share"] == 1.0
    assert result["regressed"] is False


def test_revalidate_one_flags_a_real_regression(tmp_path):
    from scripts.revalidate_hotwords import revalidate_one

    song_dir = tmp_path / "some-song"
    lines_words = [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)] for i in range(8)]
    _make_owner_song(song_dir, lines_words)
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)                                     # "before": already agrees perfectly
    # "after" (fresh, worse): line 0's own two words are heard as different text at the SAME times -- something IS
    # heard right there (so the line is scored OUT, not excused as unjudged, matching the real bug's own shape),
    # every other line unchanged.
    worse_words = [("wrong", 0.0, 0.4), ("words", 0.5, 0.9)] + flat[2:]
    worse = _FakeModel(worse_words)

    result = revalidate_one(song_dir, model=worse)

    assert result["before_share"] == 1.0
    assert result["regressed"] is True and result["after_share"] == pytest.approx(7 / 8)


def test_revalidate_one_uses_a_fresh_fetch_lyric_lines_lookup_for_a_non_owner_song(tmp_path, monkeypatch):
    """Part 2: a song whose lyrics came from an online source (not the owner) gets its hint from a FRESH
    fetch_lyric_lines() lookup -- matching pipeline.py's own fetched-lyrics branch (Task 4) exactly, including
    which of song_info.json's fields it passes through -- never from reading its own already-saved lyric text
    directly (the real fetch may return a different source's edition of the same lyrics; revalidate_one must
    build its hint the same way the real pipeline does, not take a shortcut)."""
    import scripts.revalidate_hotwords as revalidate

    song_dir = tmp_path / "some-song"
    lines_words = [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)] for i in range(8)]
    _make_owner_song(song_dir, lines_words, source="lrclib", title="T", artist="A", duration=12.0)
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)
    fake = _FakeModel(flat)
    seen = {}

    def fake_fetch(audio_path, title, artist, duration, alt_titles):
        seen["args"] = (title, artist, duration, alt_titles)
        return ["Boris the spider"]

    monkeypatch.setattr(revalidate, "fetch_lyric_lines", fake_fetch)

    result = revalidate.revalidate_one(song_dir, model=fake)

    assert seen["args"] == ("T", "A", 12.0, None)
    assert result["kind"] == "fetched"
    assert result["before_share"] == 1.0 and result["after_share"] == 1.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_revalidate_hotwords.py -q`
Expected: FAIL -- `ModuleNotFoundError: No module named 'scripts.revalidate_hotwords'`

- [ ] **Step 3: Implement**

Create `scripts/revalidate_hotwords.py`:

```python
"""Re-checks a song's sync score with Whisper hotwords enabled (2026-09-27), WITHOUT touching any song's own saved
files, to prove the hotwords change (transcribe.py, pipeline.py) does not regress a song that already works.
Covers BOTH parts of the feature: an owner-edited song (Part 1 -- hint = the owner's own confirmed lyric lines)
and every OTHER song that has been through the align stage at least once (Part 2 -- hint = fetch_lyric_lines() run
fresh, the same cheap lookup a real Generate now makes automatically). Read-only except for a scratch
re-transcription.

  .venv/bin/python scripts/revalidate_hotwords.py             # every song under work/ with saved word timings
  .venv/bin/python scripts/revalidate_hotwords.py --only <song>

Exits 1 if any song's score gets WORSE with hotwords than without; 0 otherwise (including "no songs found" and "a
song could not be scored either way" -- both printed, neither counted as a failure or a pass). This checks ONE
narrow thing -- the timing sync share -- not whether a song is otherwise flawless (many already have unrelated
imperfections this never looks at, confirmed with the owner, 2026-09-27)."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.anchors import HeardWord
from lyricvideo.fetch_lyrics import fetch_lyric_lines
from lyricvideo.models import load_song
from lyricvideo.pipeline import load_redo_inputs
from lyricvideo.timing_gate import check_sync
from lyricvideo.transcribe import lyric_hotwords

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def find_owner_lyrics_songs(work_root: Path) -> list[Path]:
    found = []
    for timed_path in sorted(Path(work_root).glob("*/lyrics_timed.json")):
        try:
            song = load_song(timed_path)
        except Exception:
            continue
        if song.lyrics_source == "owner":
            found.append(timed_path.parent)
    return found


def find_fetched_lyrics_songs(work_root: Path) -> list[Path]:
    """Every OTHER song dir under work_root that has been through the align stage at least once (a real
    lyrics_timed.json with at least one line that has words) -- these get their hint from a fresh
    fetch_lyric_lines() lookup, not from owner-confirmed text."""
    found = []
    for timed_path in sorted(Path(work_root).glob("*/lyrics_timed.json")):
        try:
            song = load_song(timed_path)
        except Exception:
            continue
        if song.lyrics_source != "owner" and any(line.words for line in song.lines):
            found.append(timed_path.parent)
    return found


def _score(song, heard) -> float | None:
    line_words = [[w.word for w in line.words] for line in song.lines]
    times = [(w.start_time, w.end_time) for line in song.lines for w in line.words]
    return check_sync(line_words, times, heard).share


def _hint_for(song_dir: Path, song) -> str:
    """The hotwords hint this song's OWN real code path would build -- the owner's confirmed lines for an
    owner-edited song (Part 1, pipeline.py's `owner_lines is not None` branch), or a fresh fetch_lyric_lines()
    lookup for any other song (Part 2, pipeline.py's fetched-lyrics `else:` branch), reading the same
    song_info.json fields that branch does."""
    if song.lyrics_source == "owner":
        return lyric_hotwords([line.text for line in song.lines])
    info = json.loads((song_dir / "song_info.json").read_text(encoding="utf-8"))
    hint_lines = fetch_lyric_lines(
        Path(song.audio_path), info["title"], info["artist"], info["duration"], info.get("alt_titles"),
    )
    return lyric_hotwords(hint_lines)


def revalidate_one(song_dir: Path, model=None) -> dict:
    song_dir = Path(song_dir)
    song = load_song(song_dir / "lyrics_timed.json")
    kind = "owner" if song.lyrics_source == "owner" else "fetched"
    from lyricvideo.owner_whisper import corrected_heard_words

    before_heard = corrected_heard_words(song_dir, song=song)
    before_share = _score(song, before_heard)

    audio_path, _title = load_redo_inputs(song_dir)
    vocals_path = song_dir / "htdemucs" / Path(audio_path).stem / "vocals.wav"
    if not vocals_path.exists():
        return {"song": song_dir.name, "kind": kind, "before_share": before_share, "after_share": None,
                "regressed": False}

    from lyricvideo.transcribe import _load_model  # only if model is None; kept lazy on purpose

    real_model = model or _load_model()
    segments, _info = real_model.transcribe(
        str(vocals_path), language="en", vad_filter=False, temperature=0.0,
        condition_on_previous_text=False, beam_size=1, word_timestamps=True,
        hotwords=_hint_for(song_dir, song) or None,
    )
    after_heard = [
        HeardWord(w.word.strip(), float(w.start), float(w.end))
        for s in segments for w in (getattr(s, "words", None) or []) if w.word.strip()
    ]
    after_share = _score(song, after_heard)

    regressed = before_share is not None and after_share is not None and after_share < before_share
    return {"song": song_dir.name, "kind": kind, "before_share": before_share, "after_share": after_share,
            "regressed": regressed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="just this song's work-folder name")
    args = parser.parse_args(argv)

    work_root = PROJECT_ROOT / "work"
    songs = find_owner_lyrics_songs(work_root) + find_fetched_lyrics_songs(work_root)
    if args.only:
        songs = [s for s in songs if s.name == args.only]
    if not songs:
        print("No songs with saved word timings found.")
        return 0

    any_regressed = False
    for song_dir in songs:
        result = revalidate_one(song_dir)
        before = f"{result['before_share']:.1%}" if result["before_share"] is not None else "?"
        after = f"{result['after_share']:.1%}" if result["after_share"] is not None else "?"
        flag = " REGRESSED" if result["regressed"] else ""
        print(f"  [{result['kind']:7}] {result['song']:40} {before:>6} -> {after:>6}{flag}")
        any_regressed = any_regressed or result["regressed"]
    return 1 if any_regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_revalidate_hotwords.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/revalidate_hotwords.py tests/test_revalidate_hotwords.py
git commit -m "Add scripts/revalidate_hotwords.py: prove the hotwords change regresses no song, owner-edited or not"
```

---

## Completion

After Task 5, run the full suite (`.venv/bin/python -m pytest tests/ -q`) and then actually run
`scripts/revalidate_hotwords.py` for real against the owner's own `work/` directory (this makes real Whisper
calls -- ~70s per song checked -- so it is a real cost, not a free check, and scales with how many songs have
saved word timings; use `--only <song>` to spot-check a handful first if a quick sanity read is wanted before the
full run) and report its table to the owner before calling this plan done. If it reports any regression, that
song's specific case needs understanding before shipping, not silent dismissal.
