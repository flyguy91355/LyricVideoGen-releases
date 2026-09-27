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
sung during a span the aligner already placed. `corrected_heard_words()` is the single choke point every consumer of
`transcribe.load_transcript_words()` should call instead, so the correction reaches scoring (timing_gate's sync
check, pipeline._align_lyrics's candidate scoring) and the Whisper Text review popup alike with one change."""

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
    try:
        stored = json.loads((work_dir / OWNER_WHISPER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return raw

    added: list[HeardWord] = []
    for key, entry in stored.items():
        try:
            row = int(key)
            text = str(entry["text"])
            line_text = str(entry["line_text"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= row < len(lines)):
            continue
        line = lines[row]
        if line.text != line_text:
            continue  # the lyrics changed since this correction was saved -- never misapply it to the wrong line
        tokens = text.split()
        if not tokens:
            continue
        start = line.words[0].start_time or 0.0
        end = line.words[-1].end_time or start
        span = end - start
        for i, token in enumerate(tokens):
            at = start if len(tokens) == 1 else start + span * i / (len(tokens) - 1)
            added.append(HeardWord(token, at, at))
    return raw + added
