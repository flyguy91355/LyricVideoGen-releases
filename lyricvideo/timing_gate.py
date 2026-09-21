"""timing_gate: the automatic yes/no on whether a song's lyric highlighting can be trusted (owner's standard, 2026-09-20).

A line is IN SYNC when its words start within half a second of where the recognizer (Whisper on the isolated vocal
track) heard them sung; a song PASSES when at least 90% of its judged lines are. Only the search for the sung word is
wider (+-2.5 s, `SEARCH_SECONDS`) and it is strictly time-local, never "anywhere in the song": a line placed where other
words are sung is out of sync even when its words are sung in another verse or chorus. The check it replaces let a
repeated chorus excuse a misplaced line and cleared 'Like a Prayer', whose second half ran 20-85 s ahead of the singing.

A line the recognizer heard too little of to compare is left out (unjudged); a song with too few judged lines cannot be
checked and is set aside like a failing one. Callers: `pipeline._align_lyrics` (picks the alignment and sets the concern
on a fresh render), `pipeline.list_pending_uploads`/`list_flagged_songs` (hold an already-rendered song before it can
upload), and `python -m lyricvideo.timing_gate` (report / hold over a whole work folder)."""

from __future__ import annotations

import argparse
import bisect
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

from .anchors import HeardWord
from .cleared_log import record_cleared, record_removed
from .lyric_audio_match import _content, _tokens, _words_match
from .models import load_song, save_song
from .owner_verified import verification
from .transcribe import load_transcript_words

TOLERANCE_SECONDS = 0.5        # a line this close to the singing is in sync (the owner notices at about half a second)
SEARCH_SECONDS = 2.5           # how far around a word's placed time a sung match is looked for; NOT a tolerance
PASS_SHARE = 0.90              # the default bar; the owner's own is Settings.timing_pass_percent (see use_pass_share_from)
MIN_JUDGED_LINES = 8           # fewer judged lines than this and the song cannot be checked
MIN_HEARD_NEARBY = 3           # a line with no matching word is "out of sync" only if this many words are sung around it
_SHOWN_LINES = 8               # line numbers named in the reason before "..."
_EPSILON = 1e-9
_GATE_PREFIX = "SET ASIDE FOR REVIEW -- the lyric timing"
_GATE_MARKERS = ("of the lines start within half a second", "the lyric timing could not be checked")   # the older word-level
                                                                                                # check says "of the words"

_pass_share_source = None


def use_pass_share_from(source=None) -> None:
    """Registers where the pass mark comes from (the GUI hands in a function that reads the LIVE Settings, so the very
    next check sees a changed slider); None goes back to the default 90%."""
    global _pass_share_source
    _pass_share_source = source


def pass_share() -> float:
    """The bar in force right now, as a share between 0.5 and 1.0."""
    if _pass_share_source is None:
        return PASS_SHARE
    try:
        return min(max(float(_pass_share_source()), 0.5), 1.0)
    except Exception:
        return PASS_SHARE


def _percent(share: float) -> str:
    """"80%", but "89.7%" -- a score that rounds up to the bar must never read like a pass."""
    return f"{share * 100:.1f}".rstrip("0").rstrip(".") + "%"


def is_gate_concern(text: str) -> bool:
    """True for a concern THIS check wrote and alone (never one from the lyric-text checks, alone or combined with one):
    only those may be released or rewritten when the bar moves."""
    return text.startswith(_GATE_PREFIX) and any(marker in text for marker in _GATE_MARKERS)


def hidden_note(hidden: int, percent: int) -> str:
    """The line under the Upload to YouTube list ("" when nothing is hidden)."""
    if hidden <= 0:
        return ""
    return f"{hidden} video{'' if hidden == 1 else 's'} hidden: below {percent}%"


@dataclass(frozen=True)
class SyncReport:
    share: float | None                 # in-sync share of judged lines; None when too few lines could be judged
    judged_lines: int
    unjudged_lines: int                 # lines the recognizer heard too little of to compare
    total_lines: int
    out_of_sync_lines: tuple[int, ...] = field(default_factory=tuple)      # 1-based, as the lyrics editor numbers them
    needed: float = PASS_SHARE                                              # the bar this report was judged against

    @property
    def passes(self) -> bool:
        return self.share is not None and self.share >= self.needed - _EPSILON

    @property
    def concern(self) -> str:
        """Why the song is set aside, in the words the review panel shows ("" when it passes)."""
        if self.passes:
            return ""
        tail = "The video is made but not uploaded; watch it and correct the lyrics or timing."
        if self.share is None:
            return (
                f"SET ASIDE FOR REVIEW -- the lyric timing could not be checked: too little of the singing was recognized "
                f"to compare the lines with (only {self.judged_lines} of {self.total_lines} lines could be judged; at least "
                f"{MIN_JUDGED_LINES} are needed). {tail}"
            )
        shown = ", ".join(str(n) for n in self.out_of_sync_lines[:_SHOWN_LINES])
        more = ", ..." if len(self.out_of_sync_lines) > _SHOWN_LINES else ""
        return (
            f"SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only {_percent(self.share)} of the lines start "
            f"within half a second of where they are sung ({_percent(self.needed)} are needed); lines {shown}{more} are off. {tail}"
        )


def check_sync(
    line_words: list[list[str]], times: list[tuple[float, float]], heard: list[HeardWord], needed: float | None = None,
) -> SyncReport:
    """line_words: each line's words; times: one (start, end) per word, flat in line order; heard: what was sung;
    needed: the share of lines that must be in sync (default: the bar in force, see pass_share)."""
    needed = pass_share() if needed is None else needed
    sung = sorted((hw.start, token) for hw in heard for token in _content(_tokens(hw.word)))
    starts = [start for start, _ in sung]

    def sung_between(low: float, high: float) -> list[tuple[float, str]]:
        return sung[bisect.bisect_left(starts, low):bisect.bisect_right(starts, high)]

    n = 0
    in_sync = unjudged = 0
    out_of_sync: list[int] = []
    for number, words in enumerate(line_words, start=1):
        placed = times[n:n + len(words)]
        n += len(words)
        if not words:
            continue
        offsets = []
        has_tokens = False
        for word, (start, _end) in zip(words, placed):
            for token in _content(_tokens(word)):
                has_tokens = True
                near = [start - heard_at for heard_at, sung_token in sung_between(start - SEARCH_SECONDS, start + SEARCH_SECONDS)
                        if _words_match(token, sung_token)]
                if near:
                    offsets.append(min(near, key=abs))
        if not has_tokens:
            unjudged += 1                                   # nothing but filler and vocalisations to compare
        elif offsets:
            if abs(statistics.median(offsets)) <= TOLERANCE_SECONDS + _EPSILON:
                in_sync += 1
            else:
                out_of_sync.append(number)
        elif len(sung_between(placed[0][0] - SEARCH_SECONDS, placed[-1][1] + SEARCH_SECONDS)) >= MIN_HEARD_NEARBY:
            out_of_sync.append(number)                      # other words are sung right here, none of this line's
        else:
            unjudged += 1                                   # the recognizer heard (almost) nothing here: cannot say
    judged = in_sync + len(out_of_sync)
    share = in_sync / judged if judged >= MIN_JUDGED_LINES else None
    return SyncReport(share, judged, unjudged, len(line_words), tuple(out_of_sync), needed)


def pick_by_sync(
    candidates: dict[str, list[tuple[float, float]]], line_words: list[list[str]], heard: list[HeardWord],
    preferred: str | None = None, needed: float | None = None,
) -> tuple[str, list[tuple[float, float]], SyncReport]:
    """The candidate alignment with the most lines in sync (a tie goes to `preferred`, then to the earlier one)."""
    scored = []
    for order, (name, times) in enumerate(candidates.items()):
        report = check_sync(line_words, times, heard, needed)
        scored.append((-(report.share if report.share is not None else -1.0), name != preferred, order, name, times, report))
    _, _, _, name, times, report = min(scored, key=lambda item: item[:3])
    return name, times, report


@dataclass(frozen=True)
class Settled:
    method: str                                  # which candidate alignment was taken
    times: list[tuple[float, float]]
    report: SyncReport
    concern: str                                 # "" when the song can be trusted


def settle_alignment(
    candidates: dict[str, list[tuple[float, float]]], line_words: list[list[str]], heard: list[HeardWord],
    preferred: str | None = None, earlier_concern: str = "", needed: float | None = None,
) -> Settled:
    """The final say on a fresh render: take the alignment with the most lines in sync, and set the song aside unless that
    one passes. A concern an earlier check raised (a line timed where nobody sings) survives a passing sync check; a failing
    one is replaced by this reason, which already says what is wrong."""
    method, times, report = pick_by_sync(candidates, line_words, heard, preferred, needed)
    return Settled(method, times, report, earlier_concern if report.passes else report.concern)


def check_saved_song(song_dir: Path, needed: float | None = None) -> SyncReport | None:
    """The verdict on a rendered song's own saved timing, or None when it cannot be read or has no transcript."""
    song_dir = Path(song_dir)
    try:
        song = load_song(song_dir / "lyrics_timed.json")
    except Exception:
        return None
    heard = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(song_dir)]
    if not heard:
        return None
    line_words = [[w.word for w in line.words] for line in song.lines]
    times = [(w.start_time, w.end_time) for line in song.lines for w in line.words]
    return check_sync(line_words, times, heard, needed)


def hold_if_timing_fails(song_dir: Path, needed: float | None = None) -> str:
    """Keeps a rendered song's hold in step with the bar, and returns the timing concern it now carries ("" when none).
    A song that FAILS gets the reason written into its lyrics_timed.json (so every upload path skips it and it shows in
    Flagged for Lyrics Review); a song this check held that now PASSES (the bar was lowered, or a redo fixed it) is
    released; a passing one stays clean. A concern from any other check is never touched, and a song that cannot be
    judged here (an older song with no transcript) keeps whatever it had: only a fresh render holds on "could not be
    checked"."""
    song_dir = Path(song_dir)
    timed_path = song_dir / "lyrics_timed.json"
    try:
        song = load_song(timed_path)
    except Exception:
        return ""
    existing = song.lyrics_accuracy_concern
    if existing and not is_gate_concern(existing):
        return ""
    if verification(song_dir):
        return ""                                   # the owner approved this version: never re-held behind their back
    report = check_saved_song(song_dir, needed)
    if report is None or report.share is None:
        return existing
    if report.passes:
        if existing:
            save_song(replace(song, lyrics_accuracy_concern=""), timed_path)
            record_cleared(song_dir.name, f"passes the {report.needed:.0%} timing check")
        return ""
    if report.concern != existing:
        save_song(replace(song, lyrics_accuracy_concern=report.concern), timed_path)
        record_removed(song_dir.name, report.concern[:300])          # so the cleared-for-upload record stops counting it
    return report.concern


def scan_songs(work_root: Path, hold: bool = False, needed: float | None = None) -> list[dict]:
    """One row per rendered song that has a saved timing and a transcript: slug, share, judged_lines, passes,
    out_of_sync_lines, on_youtube. With hold=True every FAILING song not yet on YouTube also gets its reason written in
    (held from upload and listed for review); a live video is only ever reported, never touched."""
    from .youtube_state import STATE_FILENAME       # local: keeps this module light for the render-time import

    rows = []
    for entry in sorted(Path(work_root).iterdir(), key=lambda e: e.name) if Path(work_root).exists() else []:
        if not entry.is_dir() or not (entry / "lyrics_timed.json").exists():
            continue
        report = check_saved_song(entry, needed)
        if report is None:
            continue
        on_youtube = (entry / STATE_FILENAME).exists()
        if hold and not on_youtube:
            hold_if_timing_fails(entry, needed)
        rows.append(dict(slug=entry.name, share=report.share, judged_lines=report.judged_lines, passes=report.passes,
                         out_of_sync_lines=report.out_of_sync_lines, on_youtube=on_youtube))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lyricvideo.timing_gate",
        description="Check every rendered song's lyric timing (90% of lines within half a second of the singing).")
    parser.add_argument("--work", default="work", help="the work folder (default: work)")
    parser.add_argument("--percent", type=float, help="the pass mark in percent (default: the one saved in Settings)")
    parser.add_argument("--hold", action="store_true",
                        help="also hold every failing song that is not on YouTube yet (a live video is only reported)")
    args = parser.parse_args(argv)
    if args.percent is None:
        from .settings import Settings
        args.percent = Settings.load().timing_pass_percent
    rows = scan_songs(Path(args.work), hold=args.hold, needed=args.percent / 100)
    for row in sorted(rows, key=lambda r: (r["passes"], r["share"] if r["share"] is not None else -1.0)):
        share = "  n/a" if row["share"] is None else f"{row['share']:4.0%}"
        print(f"{'PASS' if row['passes'] else 'FAIL'}  {share}  {'on YouTube' if row['on_youtube'] else 'not uploaded':12s} {row['slug']}")
    failing = [r for r in rows if not r["passes"]]
    print(f"\n{len(rows) - len(failing)} of {len(rows)} songs pass; {len(failing)} fail" + ("; failing songs not on YouTube were held." if args.hold else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
