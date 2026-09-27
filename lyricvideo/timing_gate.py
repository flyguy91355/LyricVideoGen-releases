"""timing_gate: the automatic yes/no on whether a song's lyric highlighting can be trusted (owner's standard, 2026-09-20).

A line is IN SYNC when its words start within half a second of where the recognizer (Whisper on the isolated vocal
track) heard them sung; a song PASSES when at least 90% of its judged lines are. Only the search for the sung word is
wider (+-2.5 s, `SEARCH_SECONDS`) and it is strictly time-local, never "anywhere in the song": a line placed where other
words are sung is out of sync even when its words are sung in another verse or chorus. The check it replaces let a
repeated chorus excuse a misplaced line and cleared 'Like a Prayer', whose second half ran 20-85 s ahead of the singing.

A line the recognizer heard too little of to compare is left out (unjudged); a song with too few judged lines cannot be
checked and is set aside like a failing one. Callers: `pipeline._align_lyrics` (picks the alignment and sets the concern
on a fresh render), `pipeline.list_pending_uploads`/`list_flagged_songs` (judge an already-rendered song READ-ONLY at the
current bar before it can upload -- timing_verdict; a listing never writes), and
`python -m lyricvideo.timing_gate` (report / `--hold` writes the hold over a whole work folder)."""

from __future__ import annotations

import argparse
import bisect
import os
import statistics
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from .anchors import HeardWord
from .cleared_log import record_cleared, record_removed
from .lyric_audio_match import _content, _tokens, _words_match
from .models import Song, display_slug, load_song, original_song_dir, save_song
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


def percent_display(share: float) -> str:
    """"80%", but "89.7%" -- a score that rounds up to the bar must never read like a pass. Public (not
    module-private) since pipeline.py's cleared-song note (owner, 2026-09-23: "i want to know how close it
    is") uses the identical formatting for the real achieved percentage."""
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
            f"SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only {percent_display(self.share)} of the lines start "
            f"within half a second of where they are sung ({percent_display(self.needed)} are needed); lines {shown}{more} are off. {tail}"
        )


def heard_text_near_line(line_words: list, heard: list[HeardWord]) -> str:
    """The words Whisper heard within +-SEARCH_SECONDS of a lyric line's own placed span -- the exact window
    check_sync() searches when scoring that line. `line_words` are Word objects (start_time/end_time set); an
    empty line, or one with nothing heard nearby, returns "". Shared by the GUI's Whisper Text review popup
    (pipeline.whisper_lines_for) and deep_review's diagnosis, so both read the identical evidence."""
    if not line_words:
        return ""
    low = line_words[0].start_time - SEARCH_SECONDS
    high = line_words[-1].end_time + SEARCH_SECONDS
    ordered = sorted(heard, key=lambda hw: hw.start)
    starts = [hw.start for hw in ordered]
    nearby = ordered[bisect.bisect_left(starts, low):bisect.bisect_right(starts, high)]
    return " ".join(hw.word.strip() for hw in nearby if hw.word.strip())


_IN, _OUT, _UNJUDGED = "in", "out", "unjudged"


def _line_verdicts(
    line_words: list[list[str]], times: list[tuple[float, float]], heard: list[HeardWord],
) -> list[str | None]:
    """One verdict per line, in line order: "in" (its words start within TOLERANCE_SECONDS of where they are sung), "out"
    (they don't, or other words are sung where it is placed), "unjudged" (the recognizer heard too little there to say), or
    None for a line with no words. check_sync counts these; pick_by_sync compares candidates line by line with them."""
    sung = sorted((hw.start, token) for hw in heard for token in _content(_tokens(hw.word)))
    starts = [start for start, _ in sung]

    def sung_between(low: float, high: float) -> list[tuple[float, str]]:
        return sung[bisect.bisect_left(starts, low):bisect.bisect_right(starts, high)]

    n = 0
    verdicts: list[str | None] = []
    for words in line_words:
        placed = times[n:n + len(words)]
        n += len(words)
        if not words:
            verdicts.append(None)
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
            verdicts.append(_UNJUDGED)                      # nothing but filler and vocalisations to compare
        elif offsets:
            verdicts.append(_IN if abs(statistics.median(offsets)) <= TOLERANCE_SECONDS + _EPSILON else _OUT)
        elif len(sung_between(placed[0][0] - SEARCH_SECONDS, placed[-1][1] + SEARCH_SECONDS)) >= MIN_HEARD_NEARBY:
            verdicts.append(_OUT)                           # other words are sung right here, none of this line's
        else:
            verdicts.append(_UNJUDGED)                      # the recognizer heard (almost) nothing here: cannot say
    return verdicts


def _report_from(verdicts: list[str | None], total_lines: int, needed: float) -> SyncReport:
    in_sync = sum(1 for v in verdicts if v == _IN)
    out_of_sync = tuple(number for number, v in enumerate(verdicts, start=1) if v == _OUT)
    unjudged = sum(1 for v in verdicts if v == _UNJUDGED)
    judged = in_sync + len(out_of_sync)
    share = in_sync / judged if judged >= MIN_JUDGED_LINES else None
    return SyncReport(share, judged, unjudged, total_lines, out_of_sync, needed)


def check_sync(
    line_words: list[list[str]], times: list[tuple[float, float]], heard: list[HeardWord], needed: float | None = None,
) -> SyncReport:
    """line_words: each line's words; times: one (start, end) per word, flat in line order; heard: what was sung;
    needed: the share of lines that must be in sync (default: the bar in force, see pass_share)."""
    needed = pass_share() if needed is None else needed
    return _report_from(_line_verdicts(line_words, times, heard), len(line_words), needed)


def pick_by_sync(
    candidates: dict[str, list[tuple[float, float]]], line_words: list[list[str]], heard: list[HeardWord],
    preferred: str | None = None, needed: float | None = None,
) -> tuple[str, list[tuple[float, float]], SyncReport]:
    """The candidate alignment with the most lines in sync, counted over the SAME lines for every candidate: every line at
    least one candidate places where it can be judged. A line one candidate moves into silence (unjudged there) while another
    places it on singing counts as not in sync for the first -- ranking each candidate by its own share let one that threw a
    line into a guitar solo beat one that placed it 0.8 s late on its own singing (issue #7 review; the 'Go Your Own Way'
    failure). A tie goes to the candidate that leaves fewer such lines in silence, then to `preferred`, then to the earlier
    one. The report returned is the chosen candidate's own (check_sync), so the gate's percentage is unchanged."""
    needed = pass_share() if needed is None else needed
    verdicts = {name: _line_verdicts(line_words, times, heard) for name, times in candidates.items()}
    judgeable = {i for line_verdicts in verdicts.values() for i, v in enumerate(line_verdicts) if v in (_IN, _OUT)}
    scored = []
    for order, (name, times) in enumerate(candidates.items()):
        line_verdicts = verdicts[name]
        in_sync = sum(1 for i in judgeable if line_verdicts[i] == _IN)
        moved_into_silence = sum(1 for i in judgeable if line_verdicts[i] == _UNJUDGED)
        report = _report_from(line_verdicts, len(line_words), needed)
        scored.append((-in_sync, moved_into_silence, name != preferred, order, name, times, report))
    _, _, _, _, name, times, report = min(scored, key=lambda item: item[:4])
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
    """The final say on a fresh render: take the alignment with the most lines in sync, and set the song aside ONLY when that
    one is below the pass mark (owner, 2026-09-21). A concern the older precision/sync checks raised (`earlier_concern`, e.g. a
    line timed where nobody sings) no longer holds a song the gate passes: it once set aside a 96% song."""
    method, times, report = pick_by_sync(candidates, line_words, heard, preferred, needed)
    return Settled(method, times, report, report.concern)


_TIMED_FILE = "lyrics_timed.json"
_TRANSCRIPT_FILE = "transcript.json"

# check_saved_song's verdicts, kept while neither the song's timing file nor its transcript changes (issue #7: every song list
# re-parsed both and re-ran check_sync for every song on every open). Keyed by the song folder; the value holds the files'
# (inode, mtime_ns, size) signature and the report judged against a neutral bar -- the bar only sets SyncReport.needed, so a
# moved pass-mark slider needs no re-check. Guarded by a lock: the song lists run on the Tk thread and on background threads.
_REPORTS: dict[str, tuple[tuple, SyncReport | None]] = {}
_REPORTS_LOCK = threading.Lock()
_REPORTS_LIMIT = 5000


def file_signature(path: Path) -> tuple[int, int, int] | None:
    """(inode, mtime_ns, size) of a file, None when it is missing: what the song-list caches remember a parse by. Every
    save_song is an atomic replace, which gives the file a new inode, so a rewrite is noticed even within one clock tick."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def transcript_dir(song_dir: Path) -> Path:
    """Where a song's Whisper transcript lives: its own folder, or -- for an EASY CHORD variant, which build_capo_variant never
    gives one -- its original song's (the same audio, sung the same way; issue #7 review: without this no EASY video could
    ever be judged, so none was ever offered in Upload to YouTube and none was held when its song failed the bar)."""
    song_dir = Path(song_dir)
    if (song_dir / _TRANSCRIPT_FILE).exists():
        return song_dir
    return original_song_dir(song_dir)


def _judge_saved(song_dir: Path, song: Song | None, source: Path) -> SyncReport | None:
    if song is None:
        try:
            song = load_song(song_dir / _TIMED_FILE)
        except Exception:
            return None
    heard = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(source)]
    if not heard:
        return None
    line_words = [[w.word for w in line.words] for line in song.lines]
    times = [(w.start_time, w.end_time) for line in song.lines for w in line.words]
    return check_sync(line_words, times, heard, PASS_SHARE)


def check_saved_song(
    song_dir: Path, needed: float | None = None, song: Song | None = None,
    timed_signature: tuple[int, int, int] | None = None,
) -> SyncReport | None:
    """The verdict on a rendered song's own saved timing, or None when it cannot be read or has no transcript. An EASY CHORD
    variant is scored (its own lines) against its original song's transcript (transcript_dir). Remembered until the timing
    file or the transcript changes. `song` is the already-loaded lyrics_timed.json, when the caller has it, and
    `timed_signature` the file's signature (file_signature) taken BEFORE that load: a verdict on a song the file no longer
    holds is returned but never remembered."""
    song_dir = Path(song_dir)
    needed = pass_share() if needed is None else needed
    source = transcript_dir(song_dir)
    current = file_signature(song_dir / _TIMED_FILE)
    signature = (current, str(source), file_signature(source / _TRANSCRIPT_FILE))
    key = os.path.abspath(song_dir)
    if current is not None:
        with _REPORTS_LOCK:
            cached = _REPORTS.get(key)
        if cached is not None and cached[0] == signature:
            return None if cached[1] is None else replace(cached[1], needed=needed)
    report = _judge_saved(song_dir, song, source)
    loaded_this_version = song is None or timed_signature == current
    if current is not None and loaded_this_version:
        with _REPORTS_LOCK:
            if len(_REPORTS) >= _REPORTS_LIMIT:
                _REPORTS.clear()
            _REPORTS[key] = (signature, report)
    return None if report is None else replace(report, needed=needed)


def timing_verdict(existing_concern: str, report: SyncReport | None) -> str:
    """The timing concern a song carries at the bar `report` was judged against, given the concern stored in its file (which
    must be "" or this check's own -- see is_gate_concern): the report's reason when it fails, "" when it passes, and the
    stored concern unchanged when the song cannot be judged here (an older song with no transcript: only a fresh render
    holds on "could not be checked")."""
    if report is None or report.share is None:
        return existing_concern
    return "" if report.passes else report.concern


def hold_if_timing_fails(song_dir: Path, needed: float | None = None) -> str:
    """Keeps a rendered song's STORED hold in step with the bar, and returns the timing concern it now carries ("" when none).
    A song that FAILS gets the reason written into its lyrics_timed.json; a song this check held that now PASSES (the bar was
    lowered, or a redo fixed it) is released; a passing one stays clean. A concern from any other check is never touched, and
    a song that cannot be judged here (an older song with no transcript) keeps whatever it had: only a fresh render holds on
    "could not be checked". Used by `python -m lyricvideo.timing_gate --hold`; the song lists never write (they judge the
    same way, read-only, with timing_verdict).
    The write is atomic (models.save_song), and a failure to update the cleared-songs record is only a warning."""
    song_dir = Path(song_dir)
    timed_path = song_dir / _TIMED_FILE
    signature = file_signature(timed_path)
    try:
        song = load_song(timed_path)
    except Exception:
        return ""
    existing = song.lyrics_accuracy_concern
    if existing and not is_gate_concern(existing):
        return ""
    if verification(song_dir, song=song):
        return ""                                   # the owner approved this version: never re-held behind their back
    report = check_saved_song(song_dir, needed, song=song, timed_signature=signature)
    concern = timing_verdict(existing, report)
    if concern == existing:
        return concern
    save_song(replace(song, lyrics_accuracy_concern=concern), timed_path)
    slug = display_slug(song_dir)                   # "<song>/easychords", never a bare "easychords" shared by every variant
    try:
        if concern:
            record_removed(slug, concern[:300])     # so the cleared-for-upload record stops counting it
        else:
            record_cleared(slug, f"passes the {report.needed:.0%} timing check at {percent_display(report.share)}")
    except Exception as e:
        print(f"WARNING: could not update the cleared-songs record for {slug}: {type(e).__name__}: {e}", file=sys.stderr)
    return concern


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
