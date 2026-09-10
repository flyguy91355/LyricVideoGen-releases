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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

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
    return r.json() if r.status_code == 200 and isinstance(r.json(), dict) else None


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


def _fetch_syncedlyrics_hit(title: str, artist: str) -> Hit | None:
    try:
        import syncedlyrics  # type: ignore
    except ImportError:
        return None
    term = f"{artist} {title}".strip()
    try:
        try:
            lrc = syncedlyrics.search(term, allow_plain_format=True)
        except TypeError:
            lrc = syncedlyrics.search(term)
    except Exception as exc:
        log.warning("syncedlyrics failed: %s", exc)
        return None
    if not lrc:
        return None
    return lrc, bool(LRC_TAG.search(lrc)), 0.0


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

    text, synced, _ref_duration = hit
    if synced and not text.strip():
        log.info("Track flagged instrumental by lyrics provider")
        return []
    lines = parse_lrc(text, duration) if synced else plain_to_lines(text, duration)
    return [l.text for l in lines if l.text.strip()]
