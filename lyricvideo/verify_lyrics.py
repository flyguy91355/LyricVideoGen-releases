"""Re-checks ALREADY-MADE songs against their audio: the Whisper check built on
2026-09-19 (lyric_audio_match.py) only covers songs generated from then on, and 86
of the owner's songs were made before any check existed.

    python -m lyricvideo.verify_lyrics                 # report only, writes nothing
    python -m lyricvideo.verify_lyrics --flag          # also hold failures for review

`--flag` writes a concern onto a not-yet-uploaded song's lyrics_timed.json, exactly
what the generation-time check does, so it skips auto-upload and appears in
"Flagged for Lyrics Review". It never overwrites an existing concern and never
touches an uploaded song (that video can't be recalled). Each song's transcript is
cached in its work folder, so a Redo later reuses it. Results are appended to
`--report` as they finish and a re-run resumes where it stopped."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from .lyric_audio_match import audio_match_passes, describe_mismatch, score_lyrics_against_transcript
from .models import load_song, save_song
from .transcribe import transcribe_vocals
from .youtube_state import load_youtube_state


@dataclass
class SongVerdict:
    slug: str
    status: str          # verified | flagged | mismatch | already-flagged | mismatch-uploaded | no-stem | no-lyrics | error
    coverage: float = 0.0
    worst_run: int = 0
    worst_heard_gap: int = 0
    concern: str = ""


def verify_song(work_dir: Path, transcribe: Callable = transcribe_vocals, flag: bool = False) -> SongVerdict:
    work_dir = Path(work_dir)
    slug = work_dir.name
    timed_path = work_dir / "lyrics_timed.json"
    song = load_song(timed_path)
    lines = [" ".join(w.word for w in line.words) for line in song.lines if line.words]
    if not lines:
        return SongVerdict(slug, "no-lyrics")
    vocals = Path(song.vocal_stem_path) if song.vocal_stem_path else None
    if vocals is None or not vocals.exists():
        return SongVerdict(slug, "no-stem")

    match = score_lyrics_against_transcript(lines, transcribe(vocals, work_dir))
    numbers = dict(coverage=round(match.coverage, 3), worst_run=match.worst_run, worst_heard_gap=match.worst_heard_gap)
    if audio_match_passes(match):
        return SongVerdict(slug, "verified", **numbers)

    concern = describe_mismatch(match)
    if load_youtube_state(work_dir) is not None:
        return SongVerdict(slug, "mismatch-uploaded", concern=concern, **numbers)
    if song.lyrics_accuracy_concern:
        return SongVerdict(slug, "already-flagged", concern=song.lyrics_accuracy_concern, **numbers)
    if flag:
        save_song(replace(song, lyrics_accuracy_concern=concern), timed_path)
        return SongVerdict(slug, "flagged", concern=concern, **numbers)
    return SongVerdict(slug, "mismatch", concern=concern, **numbers)


def verify_all(
    work_root: Path, transcribe: Callable = transcribe_vocals, flag: bool = False,
    on_result: Callable[[SongVerdict], None] | None = None, skip: set[str] | None = None,
) -> list[SongVerdict]:
    verdicts: list[SongVerdict] = []
    for entry in sorted(Path(work_root).iterdir(), key=lambda e: e.name):
        if not entry.is_dir() or not (entry / "lyrics_timed.json").exists() or entry.name in (skip or set()):
            continue
        try:
            verdict = verify_song(entry, transcribe=transcribe, flag=flag)
        except Exception as e:  # one broken song must never stop a multi-hour run
            verdict = SongVerdict(entry.name, "error", concern=f"{type(e).__name__}: {e}")
        verdicts.append(verdict)
        if on_result is not None:
            on_result(verdict)
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-check finished songs' lyrics against their audio.")
    parser.add_argument("--work-root", default="work", type=Path)
    parser.add_argument("--flag", action="store_true", help="hold failures in Flagged for Lyrics Review")
    parser.add_argument("--report", default="lyrics_audio_report.jsonl", type=Path)
    args = parser.parse_args(argv)

    done: set[str] = set()
    if args.report.exists():
        for row in args.report.read_text(encoding="utf-8").splitlines():
            if row.strip():
                record = json.loads(row)
                if record.get("status") != "error":
                    done.add(record["slug"])

    def record(verdict: SongVerdict) -> None:
        with args.report.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(verdict)) + "\n")
        print(f"{verdict.slug:40s} {verdict.status:18s} coverage {verdict.coverage:.2f}", flush=True)

    verify_all(args.work_root, flag=args.flag, on_result=record, skip=done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
