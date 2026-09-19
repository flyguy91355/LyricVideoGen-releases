"""Lyrics retrieval and LRC parsing, ported from LyricChord's pipeline/lyrics.py.

Provider order:
  1. Sidecar file next to the audio: "<name>.lrc" (synced) or "<name>.txt" (plain)
  2. lrclib.net - free, no API key, returns synced + plain lyrics
  3. syncedlyrics - aggregates Musixmatch / NetEase / Megalobiz / lrclib

Synced lyrics are only correct for the *edition* they were timed against, so lrclib
results are gathered for every known title variant and ranked by how closely their
reference duration matches this file. Real per-word/per-line TIMING always comes from
this program's own forced alignment (align.py) -- only line TEXT is ever kept from
whatever provider answers here.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .lyric_accuracy import check_lyric_accuracy
from .lyric_audio_match import AudioMatch, audio_match_badness, audio_match_passes, describe_mismatch
from .lyric_arbiter import Arbitration, describe_arbitration
from .lyric_reconcile import SUGGESTION_FILENAME
from .text_clean import artist_key, normalize
from .vocal_onset import vocal_onset_rise

log = logging.getLogger("playalongvideoproduction")

LRC_TAG = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
META_TAG = re.compile(r"^\[([a-zA-Z]+):([^\]]*)\]$")

MAX_LINE_HOLD = 10.0
LRCLIB_BASE = "https://lrclib.net/api"
LRCLIB_PARALLELISM = 6
DURATION_TOLERANCE = 8.0
HTTP_HEADERS = {"User-Agent": "PlayAlongVideoProduction/1.0"}

# A provider hit: (lyrics text, synced?, reference duration in seconds or 0).
# Empty text with synced=True means "the provider says this track is instrumental".
Hit = tuple[str, bool, float]


@dataclass
class _LyricLine:
    start: float
    end: float
    text: str


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _tag_seconds(m: "re.Match[str]") -> float:
    mins, secs, frac = m.group(1), m.group(2), m.group(3) or "0"
    return int(mins) * 60 + int(secs) + int(frac) / (10 ** len(frac))


def parse_lrc(text: str, duration: float = 0.0) -> list[_LyricLine]:
    """Parse LRC text into timed lines. Supports several timestamps per line and the
    standard `[offset:+/-ms]` header (positive values make the lyrics appear earlier)."""
    entries: list[tuple[float, str]] = []
    offset = 0.0
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        meta = META_TAG.match(raw)
        if meta:
            if meta.group(1).lower() == "offset":
                try:
                    offset = int(meta.group(2).strip().replace("+", "")) / 1000.0
                except ValueError:
                    pass
            continue
        tags = []
        pos = 0
        while True:
            m = LRC_TAG.match(raw, pos)
            if not m:
                break
            tags.append(m)
            pos = m.end()
        if not tags:
            continue
        body = raw[pos:].strip()
        for tag in tags:
            entries.append((_tag_seconds(tag), body))

    entries.sort(key=lambda e: e[0])
    lines: list[_LyricLine] = []
    for i, (start, text_) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else (duration if duration > start else start + MAX_LINE_HOLD)
        if text_:
            end = min(end, start + MAX_LINE_HOLD)
        lines.append(_LyricLine(start=max(0.0, start - offset), end=max(start, end) - offset, text=text_))
    return lines


def plain_to_lines(text: str, duration: float) -> list[_LyricLine]:
    """Spread untimed lyric lines evenly between an assumed intro and outro."""
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    if not rows or duration <= 0:
        return []
    start, end = duration * 0.08, duration * 0.94
    step = (end - start) / len(rows)
    return [_LyricLine(start + i * step, start + (i + 1) * step, row) for i, row in enumerate(rows)]


def has_lyrics_sidecar(path: Path) -> bool:
    return path.with_suffix(".lrc").exists() or path.with_suffix(".txt").exists()


def _sidecar(path: Path) -> Hit | None:
    lrc = path.with_suffix(".lrc")
    if lrc.exists():
        content = lrc.read_text(encoding="utf-8", errors="ignore")
        return (content, True, 0.0) if content.strip() else None
    txt = path.with_suffix(".txt")
    if txt.exists():
        content = txt.read_text(encoding="utf-8", errors="ignore")
        if content.strip():
            return content, bool(LRC_TAG.search(content)), 0.0
    return None


def score_lyrics_candidate(item: dict, file_duration: float) -> float:
    """Rank an lrclib record: synced beats plain, closer reference duration beats farther."""
    if item.get("syncedLyrics"):
        base = 2.0
    elif item.get("plainLyrics"):
        base = 1.0
    else:
        return -1.0
    ref = _num(item.get("duration"))
    if file_duration and ref:
        delta = abs(ref - file_duration)
        base -= min(0.9, delta / 120.0) if delta > 2.0 else 0.0
    return base


CLUSTER_TOLERANCE = 6.0
FOREIGN_EDITION_GAP = 20.0


@dataclass
class _Timeline:
    item: dict
    ref: float
    first: float
    last: float
    n_lines: int


def _timeline(item: dict) -> _Timeline | None:
    if not isinstance(item.get("syncedLyrics"), str) or not item["syncedLyrics"]:
        return None
    ref = _num(item.get("duration"))
    lines = [l for l in parse_lrc(item["syncedLyrics"], ref) if l.text.strip()]
    if not lines:
        return None
    return _Timeline(item, ref, lines[0].start, lines[-1].start, len(lines))


def _same_start(a: _Timeline, b: _Timeline) -> bool:
    return abs(a.first - b.first) <= CLUSTER_TOLERANCE


def _same_timeline(a: _Timeline, b: _Timeline) -> bool:
    return abs(a.first - b.first) <= CLUSTER_TOLERANCE and abs(a.last - b.last) <= 2 * CLUSTER_TOLERANCE


def _copied_from_other_edition(t: _Timeline, matching: list[_Timeline], foreign: list[_Timeline]) -> bool:
    foreign_sharers = sum(1 for f in foreign if _same_timeline(f, t))
    matching_sharers = sum(1 for m in matching if _same_timeline(m, t))
    return foreign_sharers >= max(2, matching_sharers)


OnsetScorer = Callable[[list[float]], list[float]]
ONSET_DISAGREEMENT = 0.5


def choose_lyrics_candidate(items: list[dict], file_duration: float,
                            tolerance: float = DURATION_TOLERANCE,
                            onset_scorer: OnsetScorer | None = None) -> tuple[dict | None, str]:
    """Pick the lrclib record whose timeline most plausibly belongs to this file's
    edition. See module docstring; logic ported unchanged from LyricChord."""
    timelines = [t for t in (_timeline(it) for it in items if isinstance(it, dict)) if t]
    if file_duration > 0:
        matching = [t for t in timelines if abs(t.ref - file_duration) <= tolerance]
        foreign = [t for t in timelines if abs(t.ref - file_duration) > FOREIGN_EDITION_GAP]
    else:
        matching, foreign = timelines, []

    if matching:
        copied_flags = [_copied_from_other_edition(t, matching, foreign) for t in matching]
        if all(copied_flags):
            copied_flags = [False] * len(matching)
        best, best_score, best_note = None, float("-inf"), ""
        scored: list[tuple[float, _Timeline]] = []
        for t, copied in zip(matching, copied_flags):
            cluster = [o for o in matching if _same_start(o, t)]
            median_first = sorted(o.first for o in cluster)[len(cluster) // 2]
            score = float(len(cluster))
            if copied:
                score -= 10.0
            tail = (file_duration - t.last) / file_duration if file_duration > 0 else 0.0
            score -= 3.0 * max(0.0, tail - 0.3)
            score += 0.02 * t.n_lines
            score -= 0.1 * abs(t.first - median_first)
            scored.append((score, t))
            if score > best_score:
                best, best_score = t, score
                best_note = (f"{len(cluster)} of {len(matching)} matching-length records agree on this start"
                             + ("; timing copied from another edition" if copied else ""))
        assert best is not None

        if onset_scorer is not None:
            peers = sorted(((s, t) for s, t in scored if _same_start(t, best) and s >= best_score - 1.5),
                           key=lambda st: st[1].first)
            groups: list[list[tuple[float, _Timeline]]] = []
            for s, t in peers:
                if groups and t.first - groups[-1][0][1].first <= ONSET_DISAGREEMENT:
                    groups[-1].append((s, t))
                else:
                    groups.append([(s, t)])
            if len(groups) > 1:
                rep_times = [sorted(t.first for _, t in g)[len(g) // 2] for g in groups]
                try:
                    rises = onset_scorer(rep_times)
                except Exception as exc:
                    log.debug("vocal onset check failed: %s", exc)
                    rises = []
                if len(rises) == len(groups) and all(math.isfinite(r) for r in rises):
                    own = next(i for i, g in enumerate(groups) if any(t is best for _, t in g))
                    idx = max(range(len(groups)), key=lambda i: (round(rises[i], 3), i == own))
                    chosen = max(groups[idx], key=lambda st: st[0])[1]
                    if chosen is not best:
                        best_note += f"; audio places the first line at {chosen.first:.1f}s rather than {best.first:.1f}s"
                        best = chosen
        return best.item, best_note

    ranked = sorted((it for it in items if isinstance(it, dict)),
                    key=lambda it: score_lyrics_candidate(it, file_duration), reverse=True)
    if ranked and score_lyrics_candidate(ranked[0], file_duration) >= 0:
        return ranked[0], "no record matches this file's length; using the closest edition"
    return None, "no usable records"


def _pick_lrclib(item: dict) -> Hit | None:
    ref = _num(item.get("duration"))
    if item.get("syncedLyrics"):
        return item["syncedLyrics"], True, ref
    if item.get("plainLyrics"):
        return item["plainLyrics"], False, ref
    if item.get("instrumental"):
        return "", True, ref
    return None


_TITLE_SEPARATOR = re.compile(r"\s*[/|&+,]\s*|\s+-\s+")


def title_variants(title: str, limit: int = 4) -> list[str]:
    """Spellings uploaders use for the same track."""
    out: list[str] = []

    def add(t: str) -> None:
        t = t.strip()
        if t and t.lower() not in {o.lower() for o in out}:
            out.append(t)

    add(title)
    parts = [p for p in _TITLE_SEPARATOR.split(title) if p.strip()]
    if len(parts) > 1:
        add(" ".join(parts))
        add("/".join(parts))
        add(" - ".join(parts))
    add(normalize(title))
    return out[:limit]


def artist_matches(record_artist: str, wanted: str) -> bool:
    if not wanted:
        return True
    a, b = artist_key(record_artist or ""), artist_key(wanted)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _lrclib_get(params: dict) -> dict | None:
    r = requests.get(f"{LRCLIB_BASE}/get", params=params, headers=HTTP_HEADERS, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    return data if isinstance(data, dict) else None


def _lrclib_search(params: dict) -> list[dict]:
    r = requests.get(f"{LRCLIB_BASE}/search", params=params, headers=HTTP_HEADERS, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _fetch_lrclib_hit(audio_path: Path, title: str, artist: str, duration: float,
                      alt_titles: list[str]) -> Hit | None:
    """Query lrclib.net and choose among all records by edition consensus."""
    titles = [t for t in [title, *alt_titles] if t.strip()]
    if not titles:
        return None

    jobs: list[tuple[Callable[[dict], object], dict]] = []
    if artist and duration > 0:
        for t in titles:
            jobs.append((_lrclib_get, {"artist_name": artist, "track_name": t, "duration": int(round(duration))}))
    queries: list[dict] = []
    for t in titles:
        for k, variant in enumerate(title_variants(t)):
            if k == 0:
                queries.append({"track_name": variant})
            queries.append({"q": variant})
            if artist:
                if k == 0:
                    queries.append({"track_name": variant, "artist_name": artist})
                queries.append({"q": f"{artist} {variant}"})
    seen_queries = set()
    for params in queries[:16]:
        key = tuple(sorted(params.items()))
        if key not in seen_queries:
            seen_queries.add(key)
            jobs.append((_lrclib_search, params))

    def run(job: tuple[Callable[[dict], object], dict]) -> tuple[dict, object, Exception | None]:
        fn, params = job
        try:
            return params, fn(params), None
        except (requests.RequestException, ValueError) as exc:
            return params, None, exc

    candidates: dict[object, dict] = {}
    with ThreadPoolExecutor(max_workers=LRCLIB_PARALLELISM) as pool:
        for params, result, exc in pool.map(run, jobs):
            if exc is not None:
                log.warning("lrclib request failed (%s): %s", params, exc)
                continue
            for item in (result if isinstance(result, list) else [result] if result else []):
                if isinstance(item, dict):
                    candidates.setdefault(item.get("id", id(item)), item)

    if artist:
        by_artist = {k: v for k, v in candidates.items()
                     if artist_matches(str(v.get("artistName") or ""), artist)}
        if by_artist:
            candidates = by_artist
    if not candidates:
        return None

    scorer: OnsetScorer | None = None
    if audio_path.is_file():
        scorer = lambda times: vocal_onset_rise(audio_path, times)  # noqa: E731
    chosen, note = choose_lyrics_candidate(list(candidates.values()), duration, onset_scorer=scorer)
    if chosen is not None:
        log.info("lrclib: record #%s chosen (%s)", chosen.get("id", "?"), note)
        return _pick_lrclib(chosen)
    for item in candidates.values():
        ref = _num(item.get("duration"))
        if item.get("instrumental") and (not duration or abs(ref - duration) <= DURATION_TOLERANCE):
            return "", True, ref
    return None


def _fetch_syncedlyrics_hit(title: str, artist: str, providers: list[str] | None = None) -> Hit | None:
    try:
        import syncedlyrics  # type: ignore
    except ImportError:
        return None
    term = f"{artist} {title}".strip()
    kwargs = {"providers": providers} if providers else {}
    try:
        try:
            lrc = syncedlyrics.search(term, allow_plain_format=True, **kwargs)
        except TypeError:
            lrc = syncedlyrics.search(term, **kwargs)
    except Exception as exc:
        log.warning("syncedlyrics failed: %s", exc)
        return None
    if not lrc:
        return None
    return lrc, bool(LRC_TAG.search(lrc)), 0.0


# The real, distinct lyric sources this app can try, in priority order:
# the owner's own sidecar file, lrclib.net's own edition-consensus search,
# then syncedlyrics against each of its providers individually (confirmed
# via syncedlyrics.search(..., providers=[name]) -- not one blended call,
# so a provider that got the wrong song doesn't hide one that got it
# right). See docs/superpowers/specs/2026-09-18-lyric-accuracy-check-design.md.
_ACCURACY_CHECK_SOURCES = ["sidecar", "lrclib", "Musixmatch", "NetEase", "Megalobiz", "Genius"]


# "作曲 : Bob Seger", "Composer: X", "Lyrics by：X" -- a provider's credit line, not a sung line. Needs the
# colon (ASCII or full-width) so a real lyric like "Written by the wind" or "Producer, I'm so tired" stays.
_CREDIT_LINE_RE = re.compile(
    r"^\s*(?:作曲|作词|作詞|词|曲|编曲|編曲|制作人|製作人|演唱|歌手|专辑|專輯|录音|錄音|混音|"
    r"composers?|composed\s+by|lyricists?|lyrics(?:\s+by)?|music(?:\s+by)?|words(?:\s+by)?|written\s+by|"
    r"produced\s+by|producers?|arranged\s+by|arranger|mixed\s+by|mastered\s+by|recorded\s+by|"
    r"published\s+by|publisher|vocals?|artist|album|title)\s*[:：]",
    re.IGNORECASE,
)


def _clean_timed_rows(rows: list[tuple[str, float]]) -> tuple[list[str], list[float]]:
    """Drops provider credit lines and normalises full-width punctuation ("（" -> "(", "，" -> ",") --
    NetEase's Night Moves showed '作曲 : Bob Seger' as the first lyric for the whole intro, and a stray
    full-width bracket at a line's end rendered as an empty box. A trailing unclosed "(" is dropped. Each
    row's time (0.0 when there is none) stays attached to the line it belongs to."""
    lines: list[str] = []
    times: list[float] = []
    for row, when in rows:
        if _CREDIT_LINE_RE.match(row):
            continue
        row = unicodedata.normalize("NFKC", row).strip()
        if row.endswith("(") and row.count("(") > row.count(")"):
            row = row[:-1].rstrip()
        if row:
            lines.append(row)
            times.append(when)
    return lines, times


def _clean_lyric_lines(rows: list[str]) -> list[str]:
    return _clean_timed_rows([(row, 0.0) for row in rows])[0]


def _hit_to_lines_and_times(hit: Hit, duration: float) -> tuple[list[str], list[float] | None]:
    """The lyric lines and, for a SYNCED source (lrclib, NetEase...), each line's timestamp from that source --
    a human-made second opinion on where the line sits, used by anchors.combine_anchors (the timestamps never
    become the video's timing; the aligner still does that)."""
    text, synced, _ref_duration = hit
    if synced and not text.strip():
        return [], None  # provider flagged this track instrumental
    if synced:
        parsed = [l for l in parse_lrc(text, duration) if l.text.strip()]
        lines, times = _clean_timed_rows([(l.text, float(l.start)) for l in parsed])
        return lines, times
    # Plain lyrics: the text IS the result -- no timing is derived from it
    # here, so this must not go through plain_to_lines(), whose evenly-spread
    # fake timing needs a positive duration and returns NOTHING for a file
    # whose length couldn't be probed -- real lyrics were being thrown away
    # in exactly that case (found by code review, 2026-09-14).
    return _clean_lyric_lines([row.strip() for row in text.splitlines() if row.strip()]), None


def _hit_to_lines(hit: Hit, duration: float) -> list[str]:
    return _hit_to_lines_and_times(hit, duration)[0]


def fetch_lyric_lines_verified(
    audio_path: Path, title: str, artist: str, duration: float, alt_titles: list[str] | None,
    anthropic_client, model: str = "claude-sonnet-5",
    audio_check: Callable[[list[str]], AudioMatch] | None = None,
    reconcile: Callable[[list[str], AudioMatch], tuple[list[str], list[str]] | None] | None = None,
    arbiter: Callable[[list[str], AudioMatch], Arbitration | None] | None = None,
    times_out: dict | None = None,
) -> tuple[list[str], str, str]:
    """Like fetch_lyric_lines(), but tries every real source in
    _ACCURACY_CHECK_SOURCES in order, checking each, and returns as soon as
    one passes -- rather than settling for whichever source happens to
    answer first. Returns (lines, source, concern); concern is "" only when
    some source's text passed the check cleanly.

    With `audio_check` (2026-09-19, lyric_audio_match.py), a source passes only if
    its lines match what Whisper actually heard in the vocal stem -- the Claude
    text check, which never hears the audio and passed a different edition or a
    mixed-up verse/chorus, is not used at all. If no source matches, the one
    with the BEST audio match is kept and flagged (its concern names the lines
    that don't match). With `arbiter` (lyric_arbiter.py), Claude then JUDGES that candidate's unmatched
    stretches -- lyrics wrong, or the speech recognizer merely failed? -- and if every stretch is a
    recognizer failure the candidate is accepted (source suffixed "+ai-confirmed", concern ""); otherwise
    its reasons are added to the held note. Judging works where copying did not: 8 of 8 deliberately
    corrupted songs were never confirmed, and 3 of 4 right-lyrics/hard-to-hear songs were.
    With `reconcile` too, a repair is proposed for that best candidate
    (lyric_reconcile.py) and, if it would match the audio better, is mentioned in the
    concern as a possible fix -- but NEVER applied: on real songs the "improvement" was
    Whisper's own mishearings replacing correct lyrics (text built from the recognizer's
    words matches the recognizer by construction), so it can only be a suggestion.
    Without `audio_check` (e.g. Whisper unavailable) the
    original behavior applies: check_lyric_accuracy() per source, and if none
    pass the FIRST non-empty candidate is kept with its concern. Either way
    generation is never blocked and lyrics are never fabricated.

    `times_out` (a dict) receives "line_times": the chosen source's own timestamp for each returned line when
    it was a synced source, else None -- a second opinion for the aligner (anchors.combine_anchors)."""
    def finish(lines, source, concern, times):
        if times_out is not None:
            times_out["line_times"] = times if times and len(times) == len(lines) else None
        return lines, source, concern

    best_lines: list[str] = []
    best_times: list[float] | None = None
    best_source = ""
    best_concern = "No lyrics found from any source."
    best_badness = float("inf")
    best_match: AudioMatch | None = None
    for source in _ACCURACY_CHECK_SOURCES:
        if source == "sidecar":
            hit = _sidecar(audio_path)
        elif source == "lrclib":
            hit = _fetch_lrclib_hit(audio_path, title, artist, duration, alt_titles or [])
        else:
            hit = _fetch_syncedlyrics_hit(title, artist, providers=[source])
        if hit is None:
            continue
        lines, times = _hit_to_lines_and_times(hit, duration)
        if not lines:
            continue
        if audio_check is not None:
            match = audio_check(lines)
            if audio_match_passes(match):
                return finish(lines, source, "", times)
            badness = audio_match_badness(match)
            if badness < best_badness:  # strict: on a tie the earlier source stays
                best_lines, best_source, best_times = lines, source, times
                best_concern, best_badness = describe_mismatch(match), badness
                best_match = match
            continue
        looks_accurate, concern = check_lyric_accuracy(anthropic_client, title, artist, lines, model=model)
        if looks_accurate:
            return finish(lines, source, "", times)
        if not best_lines:
            best_lines, best_source, best_concern, best_times = lines, source, concern, times
    if audio_check is not None and arbiter is not None and best_lines and best_match is not None:
        try:
            judgement = arbiter(best_lines, best_match)
        except Exception as e:  # a failed judgement must never stop the song; it just stays held
            log.warning("Lyric judgement failed: %s: %s", type(e).__name__, e)
            judgement = None
        if judgement is not None:
            if judgement.confirmed:
                return finish(best_lines, f"{best_source}+ai-confirmed", "", best_times)
            review = describe_arbitration(judgement)
            if review:
                best_concern += " " + review
    if audio_check is not None and reconcile is not None and best_lines and best_match is not None:
        try:
            repair = reconcile(best_lines, best_match)
        except Exception as e:  # a failed repair must never stop the song from being made
            log.warning("Lyric repair failed: %s: %s", type(e).__name__, e)
            repair = None
        if repair:
            new_lines, changes = repair
            new_match = audio_check(new_lines)
            if _repair_is_better(new_match, best_match):
                best_concern += (
                    f" A possible fix ({'; '.join(changes)}) was saved as {SUGGESTION_FILENAME} in the "
                    "song's folder for you to review; it was NOT applied, because a fix built from what "
                    "the recognizer heard can be wrong."
                )
    return finish(best_lines, best_source, best_concern, best_times)


def _repair_is_better(new: AudioMatch, old: AudioMatch) -> bool:
    """A repair must never be worse on any measure, and better on at least one."""
    if new.coverage < old.coverage or new.worst_run > old.worst_run or new.worst_heard_gap > old.worst_heard_gap:
        return False
    return new.coverage > old.coverage or new.worst_run < old.worst_run or new.worst_heard_gap < old.worst_heard_gap


def fetch_lyric_lines(audio_path: Path, title: str, artist: str, duration: float,
                      alt_titles: list[str] | None = None) -> list[str]:
    """Best-effort plain lyric-line text for a song: sidecar .lrc/.txt -> lrclib ->
    syncedlyrics. Returns [] if nothing usable was found anywhere -- never fabricates
    lyrics. Only line TEXT is returned; timing always comes from this program's own
    forced alignment, never from whatever timestamps a provider's LRC carries."""
    hit = _sidecar(audio_path)
    if hit is None:
        hit = _fetch_lrclib_hit(audio_path, title, artist, duration, alt_titles or [])
    if hit is None:
        hit = _fetch_syncedlyrics_hit(title, artist)
    if hit is None:
        log.warning("No lyrics found for '%s' - '%s'", artist, title)
        return []
    return _hit_to_lines(hit, duration)
    return [row.strip() for row in text.splitlines() if row.strip()]
