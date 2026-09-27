"""Diagnoses WHY a song set aside for review is actually failing, before any research or fix effort is
spent on it -- the different failure modes need completely different remedies, and only one of them
(LYRICS_WRONG) is something a lyrics-research program can help with at all:

- ALREADY_PASSES: re-checking now with the current gate/rules (at the owner's bar), the song is fine. Covers both a genuinely
  resolved song and the 2026-09-21 stale-concern bug (an old-wording concern the fix never retroactively
  cleared) -- this diagnosis always re-derives from a fresh check_saved_song(), never trusts the stored
  concern text's own wording.
- DAMAGED_AUDIO: the source audio itself is unusable (a partial/damaged file). No amount of lyrics research
  fixes a file that only has 43 seconds of a 4-minute song.
- LYRICS_WRONG: the lyric text itself looks wrong for this recording -- either the original fetch-stage
  check already said so (a non-gate concern), or a timing-gate failure's own out-of-sync lines have
  actually-different words sung nearby, not just mistimed ones.
- ALIGNMENT_ONLY: the lyrics are right -- Whisper hears the SAME words near an out-of-sync line's own
  placed time, just outside the gate's strict 0.5s tolerance -- so the real problem is alignment precision,
  a different, separate effort (see TODO.md's aligner experiment), out of scope here.
- WAITING_FOR_KEY: nothing is wrong with the lyrics or timing -- the song is only waiting for the owner's
  key (Set Key). Not this program's job, and never counted as a pass (the video may not even exist yet).
- UNKNOWN: could not be determined (no lyrics_timed.json, or a gate concern with no transcript / too few
  judged lines to re-derive anything from), or verify_lyrics' "not checked against the audio yet" hold, which
  its own free check must answer before any research is paid for.

A concern that judges the lyric TEXT (the fetch-stage audio match, the AI review, the older Claude text check --
alone or combined with a timing concern) is always LYRICS_WRONG: a passing timing re-check says nothing about
whether the words are right (wrong lines are mostly "unjudged", not out of sync), so it must never clear one.
Only a timing concern can be re-derived into ALREADY_PASSES.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from lyricvideo.anchors import HeardWord
from lyricvideo.key_decision import KEY_HOLD_PREFIX, key_needs_attention
from lyricvideo.lyric_audio_match import _content, _tokens, _words_match
from lyricvideo.models import load_song
from lyricvideo.owner_verified import verification
from lyricvideo.timing_gate import check_saved_song, heard_text_near_line, is_gate_concern
from lyricvideo.transcribe import load_transcript_words
from lyricvideo.verify_lyrics import UNCHECKED_HOLD, _is_audio_check_concern

_DAMAGED_AUDIO_MARKERS = ("damaged source audio", "partial file")
# Every timing check words its concern "SET ASIDE FOR REVIEW -- ..." (timing_gate, precision.py's older word-level
# check, sync.py); the lyric-text checks never do. "AI review" is lyric_arbiter.describe_arbitration's wording.
_TIMING_CONCERN_PREFIX = "SET ASIDE FOR REVIEW --"
_LYRIC_TEXT_MARKERS = ("AI review",)
_HELD_MARKER = "held_before_video.json"      # pipeline.HELD_MARKER (not imported: pipeline pulls in torch)
# Share of an out-of-sync line's own content words that must show up in what's actually heard nearby to
# call that line's TEXT correct (just mistimed) rather than wrong for this recording.
_LINE_MATCH_THRESHOLD = 0.5
# Share of the judged out-of-sync lines that must look textually wrong before diagnosing the whole song
# LYRICS_WRONG rather than ALIGNMENT_ONLY -- a lone stray mismatch among many merely-mistimed lines isn't
# enough to indict the lyrics file.
_MISMATCH_SHARE_FOR_LYRICS_WRONG = 1 / 3


class Category(Enum):
    ALREADY_PASSES = "already_passes"
    DAMAGED_AUDIO = "damaged_audio"
    LYRICS_WRONG = "lyrics_wrong"
    ALIGNMENT_ONLY = "alignment_only"
    WAITING_FOR_KEY = "waiting_for_key"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Diagnosis:
    category: Category
    detail: str = ""                                    # human-readable reason, for the report
    sync_share: float | None = None                      # the gate's current share, when it could be checked
    mismatched_lines: list[int] = field(default_factory=list)   # 1-based, only set for LYRICS_WRONG via the gate


def _line_looks_wrong(line_words, heard: list[HeardWord]) -> bool:
    nearby = _content(_tokens(heard_text_near_line(line_words, heard)))
    line_words_content = _content(_tokens(" ".join(w.word for w in line_words)))
    if not line_words_content:
        return False
    overlap = sum(1 for w in line_words_content if any(_words_match(w, h) for h in nearby))
    return (overlap / len(line_words_content)) < _LINE_MATCH_THRESHOLD


def is_lyric_text_concern(concern: str) -> bool:
    """True when `concern` carries a verdict on the lyric TEXT -- alone, or combined with a timing concern (the
    pipeline joins them lyric-part first). Only a pure timing concern ("SET ASIDE FOR REVIEW -- ...", any wording,
    old or new) may be cleared by a timing re-check; this never may."""
    if not concern:
        return False
    if _is_audio_check_concern(concern) or any(marker in concern for marker in _LYRIC_TEXT_MARKERS):
        return True
    return not concern.startswith(_TIMING_CONCERN_PREFIX)


def key_hold_reason(work_dir: Path) -> str | None:
    """The reason a song was stopped before its video AT THE KEY CHECK (pipeline._hold_for_key), else None -- a
    timing hold, no hold at all, or an unreadable marker."""
    try:
        reason = json.loads((Path(work_dir) / _HELD_MARKER).read_text(encoding="utf-8")).get("reason", "")
    except (OSError, ValueError, AttributeError):
        return None
    return reason if isinstance(reason, str) and reason.startswith(KEY_HOLD_PREFIX) else None


def waiting_for_key(work_dir: Path) -> bool:
    """Held at the key check, or not uploaded with its key unchecked/waiting -- the Set Key songs."""
    return key_hold_reason(work_dir) is not None or key_needs_attention(work_dir)


def diagnose_song(work_dir: Path, needed: float | None = None) -> Diagnosis:
    """The full diagnosis for one song, reading only files already on disk -- no API calls, no cost, no writes.
    `needed` is the timing bar (the owner's Settings.timing_pass_percent / 100); None = the bar in force
    (timing_gate.pass_share)."""
    work_dir = Path(work_dir)
    timed_path = work_dir / "lyrics_timed.json"
    try:
        song = load_song(timed_path)
    except Exception as e:
        return Diagnosis(Category.UNKNOWN, detail=f"could not load {timed_path.name}: {type(e).__name__}: {e}")

    concern = song.lyrics_accuracy_concern
    if concern and any(marker in concern.lower() for marker in _DAMAGED_AUDIO_MARKERS):
        return Diagnosis(Category.DAMAGED_AUDIO, detail=concern)

    if concern and concern.startswith(UNCHECKED_HOLD):
        # verify_lyrics' "not checked against the audio yet" hold: no verdict on the words exists yet, so there is
        # nothing to research -- the free local check decides first. Never cleared by a timing pass either.
        return Diagnosis(
            Category.UNKNOWN,
            detail="not yet checked against the audio -- run `python -m lyricvideo.verify_lyrics` first (free)",
        )

    if is_lyric_text_concern(concern):
        # A lyric-text-accuracy concern from the fetch-stage check (e.g. "Only 58% of these lyrics match what is
        # sung") already IS the text-correctness diagnosis. Decided BEFORE the timing re-check: a song whose wrong
        # lines were merely unjudged can pass the gate with the wrong words (HISTORY 2026-09-23).
        return Diagnosis(Category.LYRICS_WRONG, detail=concern)

    report = check_saved_song(work_dir, needed)
    if not concern:
        if report is not None and report.share is not None and not report.passes and not verification(work_dir):
            # Failing timing that was never written down (a read-only dry-run listing does not write holds): the
            # same concern hold_if_timing_fails would write.
            concern = report.concern
        elif waiting_for_key(work_dir):
            return Diagnosis(Category.WAITING_FOR_KEY, detail="the lyrics and timing are fine; waiting for the song's key (Set Key)",
                              sync_share=report.share if report is not None else None)
        else:
            return Diagnosis(Category.ALREADY_PASSES, detail="no concern recorded",
                              sync_share=report.share if report is not None else None)

    if report is not None and report.passes:
        return Diagnosis(Category.ALREADY_PASSES, detail=f"passes the current gate now ({report.share:.0%})",
                          sync_share=report.share)

    if not is_gate_concern(concern):
        # An older timing check's wording (precision.py / sync.py) that the current gate still fails: kept as
        # LYRICS_WRONG, as before -- only the current gate's own concern is re-derived line by line below.
        return Diagnosis(Category.LYRICS_WRONG, detail=concern)

    if report is None or report.share is None:
        return Diagnosis(Category.UNKNOWN,
                          detail="the timing gate could not be checked (no transcript, or too few judged lines)")

    judged = [n for n in report.out_of_sync_lines if song.lines[n - 1].words]
    if not judged:
        return Diagnosis(Category.UNKNOWN, detail="a gate concern is recorded but no out-of-sync line has words",
                          sync_share=report.share)

    heard = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(work_dir)]
    mismatched = [n for n in judged if _line_looks_wrong(song.lines[n - 1].words, heard)]

    if len(mismatched) / len(judged) >= _MISMATCH_SHARE_FOR_LYRICS_WRONG:
        return Diagnosis(Category.LYRICS_WRONG, detail=concern, sync_share=report.share, mismatched_lines=mismatched)
    return Diagnosis(Category.ALIGNMENT_ONLY, detail=concern, sync_share=report.share)
