"""The owner's corrections to specific WHISPER-heard lines (owner request, 2026-09-27, confirmed against real
evidence -- Boris the Spider: the fetched/edited lyrics were exactly right while Whisper genuinely mis-transcribed
or entirely skipped several passages, scoring correctly-timed lines as "out of sync" purely because Whisper's own
guess at the words was wrong there, never because the timing itself was off).

Saved as `work/<song>/whisper_owner.json`, keyed by ROW INDEX -- the same row order `pipeline.whisper_lines_for()`
shows, one entry per non-blank lyric line -- alongside the exact lyric line text that row showed at save time, so a
correction whose line has since changed (a later lyrics edit) is detected and dropped for just that one row rather
than silently misapplied to a different line. A correction is synthesized into ordinary HeardWord entries, spread
evenly across THAT LINE's OWN already-placed word span (from the aligner, independent of Whisper) -- it never claims
Whisper heard something at a time nobody has verified singing happens, only that the owner confirms, by ear, what is
sung during a span the aligner already placed.

Two ways in, one correction store: `corrected_heard_words()` for a caller with a SAVED Song (timing_gate's read-only
judge, the Whisper Text popup); `add_corrections()` for a caller mid-alignment (pipeline._align_lyrics) that has a
winning candidate's own final per-word `times` but has not saved anything to disk yet -- real incident, 2026-09-27:
without this second path, a Redo re-ran _align_lyrics from scratch and reproduced the IDENTICAL "out of sync"
concern even after the owner saved a correction, because only the read-only re-judge and the display popup ever
consulted it, never the concern a fresh render actually computes and writes."""

from __future__ import annotations

import json
from pathlib import Path

from .anchors import HeardWord
from .models import load_song
from .transcribe import load_transcript_words

OWNER_WHISPER_FILE = "whisper_owner.json"


def current_lyric_line_texts(work_dir: Path) -> list[str]:
    """The lyric line texts in the same row order whisper_lines_for() shows (blank lines skipped) -- shared so the
    Whisper Text editor and a saved correction are both checked against exactly what a row means today."""
    song = load_song(Path(work_dir) / "lyrics_timed.json")
    return [line.text for line in song.lines if line.words]


def owner_whisper_corrections(work_dir: Path) -> dict[int, str]:
    """The raw saved {row_index: corrected_text}, with no staleness check -- for prefilling the editor with
    whatever was last saved, even for a row whose line has since changed."""
    try:
        data = json.loads((Path(work_dir) / OWNER_WHISPER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, entry in data.items():
        try:
            out[int(key)] = str(entry["text"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_owner_whisper_line(work_dir: Path, row_index: int, corrected_text: str, line_text: str) -> None:
    """Saves (or, with a blank corrected_text, clears) one row's correction -- merges into any existing file, never
    touching another row's own correction. `line_text` is the lyric line that row showed at save time, checked
    again on read so a later lyrics edit can never leave a correction pointed at the wrong line."""
    path = Path(work_dir) / OWNER_WHISPER_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    key = str(row_index)
    if corrected_text.strip():
        data[key] = {"text": corrected_text.strip(), "line_text": line_text}
    else:
        data.pop(key, None)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _stored_corrections(work_dir: Path) -> list[tuple[int, str, str]]:
    """Every saved (row, text, line_text) triple, straight off disk -- validated against the CALLER's own current
    row texts/spans by add_corrections/corrected_heard_words, not here (this file only knows what was saved, never
    what "today's" lines are)."""
    try:
        data = json.loads((Path(work_dir) / OWNER_WHISPER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for key, entry in data.items():
        try:
            out.append((int(key), str(entry["text"]), str(entry["line_text"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _synthetic_words(text: str, word_times: list[tuple[float, float]]) -> list[HeardWord]:
    """One HeardWord per token in `text`. When the correction has the SAME number of words as the real aligned
    line (the common case: the owner typed the true lyric line itself), each token is placed at THAT SPECIFIC
    word's own real time -- not an interpolated guess. Real incident, 2026-09-27: a line's real per-word pacing is
    often uneven (some words sung longer than others), so spreading a correction evenly across the line's overall
    span could drift an individual word's synthetic position more than the sync check's own half-second tolerance
    away from where the aligner actually placed that word, even on a fully, correctly saved correction. Only when
    the word counts differ (a correction that isn't simply the real line's own words) is the span spread evenly, as
    a reasonable approximation with no per-word times to anchor to."""
    tokens = text.split()
    if not tokens:
        return []
    if len(tokens) == len(word_times):
        return [HeardWord(token, start, start) for token, (start, _end) in zip(tokens, word_times)]
    start, end = word_times[0][0], word_times[-1][1]
    span = end - start
    positions = [start] if len(tokens) == 1 else [start + span * i / (len(tokens) - 1) for i in range(len(tokens))]
    return [HeardWord(token, at, at) for token, at in zip(tokens, positions)]


def add_corrections(
    work_dir: Path, heard: list[HeardWord], line_words: list[list[str]], times: list[tuple[float, float]],
) -> list[HeardWord]:
    """`heard` PLUS synthetic words for every still-valid saved correction, placed against that line's own real
    per-word times in `times` (flat, one (start, end) per word across ALL of `line_words`, blank lines included) --
    for a caller that already has a winning candidate's own final per-word times but has not saved a Song to disk
    yet (e.g. pipeline._align_lyrics, scoring the alignment it is about to write). Row numbers, and the line-text
    staleness check, follow the exact convention whisper_lines_for()/current_lyric_line_texts() use: rows are
    indices into the NON-BLANK lines only, in order, each identified by its own joined text -- a blank line in
    `line_words` is skipped from the row count (never assigned a row), matching how the GUI numbers rows in the
    Whisper Text popup. Safe to call on a song with no correction file yet (returns `heard` unchanged)."""
    stored = _stored_corrections(work_dir)
    if not stored:
        return heard
    word_times: list[list[tuple[float, float]]] = []
    texts: list[str] = []
    n = 0
    for words in line_words:
        count = len(words)
        if count:
            word_times.append(times[n:n + count])
            texts.append(" ".join(words))
        n += count
    added: list[HeardWord] = []
    for row, text, line_text in stored:
        if not (0 <= row < len(word_times)) or texts[row] != line_text:
            continue  # out of range, or the lyrics changed since this correction was saved
        added.extend(_synthetic_words(text, word_times[row]))
    return heard + added


def corrected_heard_words(work_dir: Path, song=None) -> list[HeardWord]:
    """The transcript's raw heard words PLUS synthetic ones for every row with a still-valid owner correction --
    the raw words are never removed, only supplemented, so an unrelated token's own matching is unaffected. Safe to
    call on a song with no lyrics_timed.json yet or no correction file (returns the raw words, or [], unchanged).

    `work_dir` is where the TRANSCRIPT and the correction file live (an EASY CHORD variant is judged against its
    ORIGINAL song's transcript -- see timing_gate.transcript_dir); `song` is whose LINES a correction is placed
    against, when the caller already has it loaded (its own lines may belong to a different folder than `work_dir`,
    e.g. the variant's own lyrics_timed.json) -- reusing it here avoids parsing the same file a second time in the
    song-list scans this feeds (issue #7 review: every list already parses each song file at most once). Only
    read from `work_dir/lyrics_timed.json` when the caller has no song of its own to hand in."""
    work_dir = Path(work_dir)
    raw = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(work_dir)]
    if song is None:
        try:
            song = load_song(work_dir / "lyrics_timed.json")
        except Exception:
            return raw
    lines = [line for line in song.lines if line.words]
    line_words = [[w.word for w in line.words] for line in lines]
    times = [(w.start_time or 0.0, w.end_time or (w.start_time or 0.0)) for line in lines for w in line.words]
    return add_corrections(work_dir, raw, line_words, times)
