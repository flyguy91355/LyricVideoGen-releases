"""Re-checks ALREADY-MADE songs against their audio: the Whisper check built on
2026-09-19 (lyric_audio_match.py) only covers songs generated from then on, and 86
of the owner's songs were made before any check existed.

    python -m lyricvideo.verify_lyrics                     # report only, writes nothing
    python -m lyricvideo.verify_lyrics --flag              # also hold failures for review
    python -m lyricvideo.verify_lyrics --hold-pending      # block every unchecked, waiting song NOW
    python -m lyricvideo.verify_lyrics --flag --recheck-flagged   # re-judge earlier audio-only flags

A song that fails the audio check is then JUDGED by Claude (lyric_arbiter.py): were the lyrics wrong,
or did the speech recognizer just fail on a loud/produced recording? Only a stretch judged a
recognizer failure is accepted (status `verified-ai`); everything else stays flagged, with the
judge's reasons. `--no-ai` skips the judge.

`--flag` writes a concern onto the song's lyrics_timed.json, exactly what the generation-time check
does, so a not-yet-uploaded song skips auto-upload and appears in "Flagged for Lyrics Review". An
already-UPLOADED song is flagged too (status `flagged-uploaded`) so the older videos that may need
replacing on YouTube are recorded (lyricvideo/replace_report.py lists them). It never overwrites a
concern it did not write: a timing-gate concern is kept, with a lyric failure put in front of it (the gate
releases its own concern by itself once the song passes the bar). Lyrics the owner typed in (source
"owner") are never checked or held (status `owner-lyrics`). Each song's transcript is cached in its work
folder, so a Redo later reuses it. Results are appended to `--report` as they finish and a re-run resumes
where it stopped; a `--flag` run still releases holds a report-only run verified."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from .lyric_arbiter import describe_arbitration
from .lyric_audio_match import audio_match_passes, describe_mismatch, score_lyrics_against_transcript
from .models import load_song, save_song
from .timing_gate import is_gate_concern
from .transcribe import load_transcript_segments, load_transcript_text, transcribe_vocals
from .youtube_state import load_youtube_state

# Marks a song that is waiting to upload but has not been checked yet (owner, 2026-09-19: "stop the
# uploads until the videos are analyzed"). Any non-empty concern already keeps a song out of both
# auto-upload paths, and the running app re-reads it from disk each time, so this takes effect
# immediately with no restart. verify_song releases it (verified) or replaces it with the real reason.
UNCHECKED_HOLD = "On hold: these lyrics have not been checked against the audio yet."

# The phrases lyric_audio_match.describe_mismatch() writes -- how --recheck-flagged recognizes a concern
# the AUDIO check produced (as opposed to one from the older Claude text check, which is never touched).
_AUDIO_CHECK_PHRASES = (
    "of these lyrics match what is sung", "sung words in a row aren't in these lyrics",
    "No words were recognized in the singing",
)


def _is_audio_check_concern(text: str) -> bool:
    return any(phrase in text for phrase in _AUDIO_CHECK_PHRASES)


# How the timing gate's own concern starts (timing_gate.is_gate_concern decides whether a whole concern is one).
_GATE_START = "SET ASIDE FOR REVIEW -- the lyric timing"


def _timing_part(concern: str) -> str:
    """The timing gate's concern at the end of a combined "<lyric concern> <timing concern>" ("" when there is none):
    what stays when the lyric part is cleared, so clearing lyrics never drops a timing hold."""
    at = concern.find(_GATE_START)
    return concern[at:] if at > 0 and is_gate_concern(concern[at:]) else ""


def _owner_lyrics(song) -> bool:
    """The video carries lyrics the owner typed in (Edit Lyrics, then Redo): their word is final, so no audio or AI
    check may flag or hold them (pipeline.py's fetch_lyrics stage; issue #7 review)."""
    return song.lyrics_source == "owner"


def hold_unchecked(work_root: Path, already_verified: set[str] | None = None) -> list[str]:
    """Puts every song that is waiting to upload -- rendered, never uploaded, no concern yet, not
    already verified, lyrics not the owner's own -- on hold. Returns the songs held."""
    from .pipeline import list_pending_uploads  # heavy import (torch, anthropic): only when holding

    held: list[str] = []
    for slug in list_pending_uploads(Path(work_root)):
        if slug in (already_verified or set()):
            continue
        timed_path = Path(work_root) / slug / "lyrics_timed.json"
        song = load_song(timed_path)
        if song.lyrics_accuracy_concern or _owner_lyrics(song):
            continue
        save_song(replace(song, lyrics_accuracy_concern=UNCHECKED_HOLD), timed_path)
        held.append(slug)
    return held


@dataclass
class SongVerdict:
    slug: str
    # verified | verified-ai | flagged | flagged-uploaded | mismatch | mismatch-uploaded | already-flagged |
    # owner-lyrics | no-stem | no-lyrics | error
    status: str
    coverage: float = 0.0
    worst_run: int = 0
    worst_heard_gap: int = 0
    concern: str = ""


def verify_song(
    work_dir: Path, transcribe: Callable = transcribe_vocals, flag: bool = False,
    arbiter: Callable | None = None, load_segments: Callable = load_transcript_segments, recheck: bool = False,
) -> SongVerdict:
    work_dir = Path(work_dir)
    slug = work_dir.name
    timed_path = work_dir / "lyrics_timed.json"
    song = load_song(timed_path)
    existing = song.lyrics_accuracy_concern

    def write(concern: str) -> None:
        save_song(replace(song, lyrics_accuracy_concern=concern), timed_path)

    if _owner_lyrics(song):
        # The owner's own lyrics are final: never checked, flagged or held here. A hold an older run put on them
        # is released.
        if existing == UNCHECKED_HOLD and flag:
            write("")
        return SongVerdict(slug, "owner-lyrics", concern="" if existing == UNCHECKED_HOLD and flag else existing)
    lines = [" ".join(w.word for w in line.words) for line in song.lines if line.words]
    if not lines:
        return SongVerdict(slug, "no-lyrics")
    vocals = Path(song.vocal_stem_path) if song.vocal_stem_path else None
    if vocals is None or not vocals.exists():
        return SongVerdict(slug, "no-stem")

    # Reuse whatever transcript is already cached (hinted or not -- Part 2 of the Whisper-hotwords feature can
    # leave a hinted one here), the same way whisper_text_for() already does; only transcribe fresh via the
    # injected `transcribe` when there is no cache at all. Calling `transcribe` (transcribe_vocals by default)
    # unconditionally used to silently overwrite a hinted transcript with a fresh, unhinted, worse one on every
    # re-check -- a real bug found in review, 2026-09-27.
    cached_text = load_transcript_text(work_dir)
    heard_text = cached_text if cached_text is not None else transcribe(vocals, work_dir)
    match = score_lyrics_against_transcript(lines, heard_text)
    numbers = dict(coverage=round(match.coverage, 3), worst_run=match.worst_run, worst_heard_gap=match.worst_heard_gap)
    # A hold is "no verdict yet", and (under --recheck-flagged) so is a flag the audio check alone wrote -- clearing
    # one keeps a timing concern that rode along with it.
    reevaluable = existing == UNCHECKED_HOLD or (recheck and _is_audio_check_concern(existing))
    cleared = _timing_part(existing) if reevaluable else existing
    # A concern the timing gate wrote ALONE says nothing about the words, and the gate releases it by itself once the
    # song passes the bar -- so a lyric failure must still be recorded in front of it (issue #7 review), or the song
    # would become uploadable with lyrics that failed this check.
    timing_only = bool(existing) and is_gate_concern(existing)

    if audio_match_passes(match):
        if reevaluable and flag:
            write(cleared)                                    # checked out: uploads may resume
        return SongVerdict(slug, "verified", **numbers)

    concern = describe_mismatch(match)
    if existing and not reevaluable and not timing_only:
        return SongVerdict(slug, "already-flagged", concern=existing, **numbers)

    if arbiter is not None:
        try:
            judgement = arbiter(lines, match, load_segments(work_dir))
        except Exception:  # a failed judgement just leaves the song held on the audio check alone
            judgement = None
        if judgement is not None:
            if judgement.confirmed:
                if reevaluable and flag:
                    write(cleared)
                return SongVerdict(slug, "verified-ai", **numbers)
            review = describe_arbitration(judgement)
            if review:
                concern += " " + review

    if timing_only:
        concern = f"{concern} {existing}"                     # lyric reason first: the gate can no longer release it
    elif reevaluable and cleared:
        concern = f"{concern} {cleared}"
    uploaded = load_youtube_state(work_dir) is not None
    if flag:
        write(concern)
        return SongVerdict(slug, "flagged-uploaded" if uploaded else "flagged", concern=concern, **numbers)
    return SongVerdict(slug, "mismatch-uploaded" if uploaded else "mismatch", concern=concern, **numbers)


def verify_all(
    work_root: Path, transcribe: Callable = transcribe_vocals, flag: bool = False,
    on_result: Callable[[SongVerdict], None] | None = None, skip: set[str] | None = None,
    arbiter: Callable | None = None, recheck: bool = False,
) -> list[SongVerdict]:
    def blocking_uploads(entry: Path) -> bool:
        try:
            return load_song(entry / "lyrics_timed.json").lyrics_accuracy_concern == UNCHECKED_HOLD
        except Exception:
            return False

    verdicts: list[SongVerdict] = []
    candidates = [
        e for e in Path(work_root).iterdir()
        if e.is_dir() and (e / "lyrics_timed.json").exists() and e.name not in (skip or set())
    ]
    # Songs on hold are blocking uploads, so they go first.
    for entry in sorted(candidates, key=lambda e: (not blocking_uploads(e), e.name)):
        try:
            verdict = verify_song(entry, transcribe=transcribe, flag=flag, arbiter=arbiter, recheck=recheck)
        except Exception as e:  # one broken song must never stop a multi-hour run
            verdict = SongVerdict(entry.name, "error", concern=f"{type(e).__name__}: {e}")
        verdicts.append(verdict)
        if on_result is not None:
            on_result(verdict)
    return verdicts


def _unfinished_under_flag(work_root: Path, rows: list[dict]) -> set[str]:
    """Songs an earlier report counts as done that a --flag run must still look at (issue #7 review):
    - verified (or found to carry the owner's own lyrics) by a REPORT-ONLY run, but still carrying the "not checked
      yet" hold (report-only never writes, so only a --flag run can release it);
    - reported already-flagged while their concern was the timing gate's alone (older versions recorded no lyric
      verdict for those, and the gate releases its own concern once the song passes the bar)."""
    last_status = {r["slug"]: r.get("status") for r in rows if isinstance(r, dict) and r.get("slug")}
    unfinished: set[str] = set()
    for slug, status in last_status.items():
        if status not in ("verified", "verified-ai", "owner-lyrics", "already-flagged"):
            continue
        try:
            concern = load_song(Path(work_root) / slug / "lyrics_timed.json").lyrics_accuracy_concern
        except Exception:
            continue
        if concern == UNCHECKED_HOLD or (status == "already-flagged" and is_gate_concern(concern)):
            unfinished.add(slug)
    return unfinished


def _make_arbiter() -> Callable:
    """The real AI judge: Claude via the API key in the repo's .env."""
    import anthropic
    from dotenv import load_dotenv

    from .lyric_arbiter import arbitrate

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    client = anthropic.Anthropic()
    return lambda lines, match, segments: arbitrate(client, lines, match, segments)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-check finished songs' lyrics against their audio.")
    parser.add_argument("--work-root", default="work", type=Path)
    parser.add_argument("--flag", action="store_true", help="hold failures in Flagged for Lyrics Review")
    parser.add_argument("--report", default="lyrics_audio_report.jsonl", type=Path)
    parser.add_argument("--hold-pending", action="store_true",
                        help="put every waiting-to-upload song that has not been verified on hold, then exit")
    parser.add_argument("--no-ai", action="store_true", help="skip the Claude judge of unmatched stretches")
    parser.add_argument("--recheck-flagged", action="store_true",
                        help="judge again the songs an earlier audio-only run flagged (may clear false flags)")
    args = parser.parse_args(argv)

    def report_rows() -> list[dict]:
        if not args.report.exists():
            return []
        return [json.loads(row) for row in args.report.read_text(encoding="utf-8").splitlines() if row.strip()]

    if args.hold_pending:
        verified = {r["slug"] for r in report_rows() if r.get("status") in ("verified", "verified-ai")}
        held = hold_unchecked(args.work_root, already_verified=verified)
        print(f"Put {len(held)} songs on hold until they are checked: {', '.join(held)}")
        return 0

    # A crashed song is always retried; under --flag a song an earlier run only REPORTED is processed again
    # so it actually gets flagged; under --recheck-flagged a flagged song is judged again.
    not_done = {"error"}
    if args.flag:
        not_done |= {"mismatch", "mismatch-uploaded"}
    if args.recheck_flagged:
        not_done |= {"flagged", "flagged-uploaded", "already-flagged"}
    rows = report_rows()
    done = {r["slug"] for r in rows if r.get("status") not in not_done}
    if args.flag:
        done -= _unfinished_under_flag(args.work_root, rows)

    def record(verdict: SongVerdict) -> None:
        with args.report.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(verdict)) + "\n")
        print(f"{verdict.slug:40s} {verdict.status:18s} coverage {verdict.coverage:.2f}", flush=True)

    verify_all(
        args.work_root, flag=args.flag, on_result=record, skip=done,
        arbiter=None if args.no_ai else _make_arbiter(), recheck=args.recheck_flagged,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
