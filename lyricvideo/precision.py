"""How PRECISELY does an alignment land on the singing -- per word, against Whisper's own word times?

Why: sync.py only asks whether each line's first word is within ~2 s (plus up to 2.5 s of allowed bias) of where it was
heard. That catches a drifted chorus but not what the owner actually sees: karaoke highlighting is noticed at half a
second, and 'Go Your Own Way' (69% of words within 0.5 s) and 'Girls Just Want to Have Fun' passed while the singer was
often a line ahead of the highlight. Measured the same way on 2026-09-19, the songs the owner called spot on scored
86-96%; the ones he called off scored 32-69%.

Neither alignment wins everywhere (the whole-song CTC pass is exact until it drifts; the anchored one repairs a drift but
squeezes lines when its anchors are noisy), so the better one is taken LINE BY LINE (`blend`) when that keeps the lines
in order. Pure logic, no I/O."""

from __future__ import annotations

import difflib
import re
import statistics
from dataclasses import dataclass

from .anchors import HeardWord

WITHIN_SECONDS = 0.5           # a word is precise when it starts this close to where Whisper heard it
CLEARLY_OFF_SECONDS = 1.0      # a line is clearly off when its words are typically further than this
MIN_MATCHED_WORDS = 20         # fewer heard-and-matching words than this cannot judge precision at all
NEEDED_SHARE = 0.70            # share of matched words that must be precise...
MAX_OFF_LINE_SHARE = 0.15      # ...and no more than this share of the lines may be clearly off


@dataclass(frozen=True)
class Precision:
    share: float               # matched words starting within WITHIN_SECONDS of where they were heard
    matched: int
    lines: int                 # lines that have words
    off_lines: int             # lines whose words are typically CLEARLY_OFF_SECONDS or more away
    no_evidence: int           # lines with no heard word to compare against
    off_line_numbers: tuple[int, ...] = ()     # those lines, 1-based (as the lyrics editor numbers them)
    silent_line_numbers: tuple[int, ...] = ()  # lines timed where the vocal track is silent, 1-based (always off)


@dataclass(frozen=True)
class AlignmentChoice:
    method: str                # "whole-song" | "anchored" | "blended"
    precision: Precision
    ok: bool
    concern: str               # "" when ok, otherwise why the song is set aside
    times: list[tuple[float, float]]


def _normal(word: str) -> str:
    return re.sub(r"[^a-z']", "", word.lower())


PAIR_WINDOW_SECONDS = 4.0      # a heard word is only a plausible partner within this of where a candidate placed the word


def match_words(
    words: list[str], heard: list[HeardWord], near: list[list[float]] | None = None, window: float = PAIR_WINDOW_SECONDS,
) -> dict[int, float]:
    """Index of each lyric word (flat, song order) that Whisper also heard, in order -> the time it was heard.

    `near` = each candidate alignment's start time per lyric word. With it, a word only pairs with a heard word within
    `window` of where SOME candidate put it. Without it, a chorus sung six times but heard twice pairs the later copies
    with the earlier ones and reports 40 s 'errors' (I Want to Hold Your Hand, Back in the Saddle); the words with no
    plausible partner simply have no evidence. Without `near` the match is purely in order."""
    ours = [(i, _normal(w)) for i, w in enumerate(words)]
    ours = [(i, w) for i, w in ours if w]
    theirs = [(h, _normal(h.word)) for h in heard]
    theirs = [(h, w) for h, w in theirs if w]
    if near is None:
        matcher = difflib.SequenceMatcher(None, [w for _, w in ours], [w for _, w in theirs], autojunk=False)
        return {
            ours[i + k][0]: theirs[j + k][0].start
            for i, j, size in matcher.get_matching_blocks() for k in range(size)
        }

    def gap(a: int, b: int) -> float | None:
        (i, wa), (h, wb) = ours[a], theirs[b]
        if wa != wb:
            return None
        nearest = min(abs(times[i] - h.start) for times in near)
        return nearest if nearest <= window else None

    # longest in-order pairing over the plausible pairs (ties: the smaller total gap)
    n, m = len(ours), len(theirs)
    best = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
    for a in range(n - 1, -1, -1):
        for b in range(m - 1, -1, -1):
            options = [best[a + 1][b], best[a][b + 1]]
            g = gap(a, b)
            if g is not None:
                count, cost = best[a + 1][b + 1]
                options.append((count + 1, cost - g))
            best[a][b] = max(options)
    evidence: dict[int, float] = {}
    a = b = 0
    while a < n and b < m:
        g = gap(a, b)
        if g is not None and best[a][b] == (best[a + 1][b + 1][0] + 1, best[a + 1][b + 1][1] - g):
            evidence[ours[a][0]] = theirs[b][0].start
            a, b = a + 1, b + 1
        elif best[a][b] == best[a + 1][b]:
            a += 1
        else:
            b += 1
    return evidence


SILENT_HOP_FRACTION = 0.10     # a hop of the vocal track is voiced at this share of the song's loud level
SILENT_LINE_VOICED = 0.15      # a line fewer than this share of whose WORDS fall in singing was placed where nobody sings


def silent_lines(
    times: list[tuple[float, float]], line_of_word: list[int], loudness: list[float], hop: float,
) -> set[int]:
    """Lines (0-based) timed where the vocal track is silent. Independent of Whisper, which cannot object to a line it
    heard nothing near: 'Go Your Own Way' had two lines timed at 117 s and 174 s, inside guitar solos. Quiet singing
    is not silence -- hums and backing vocals in songs the owner called spot on are still 33-47% voiced."""
    if not loudness:
        return set()
    ordered = sorted(loudness)
    reference = ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]
    if reference <= 0:
        return set()
    voiced = [level >= SILENT_HOP_FRACTION * reference for level in loudness]
    words_of: dict[int, list[int]] = {}
    for k, line in enumerate(line_of_word):
        words_of.setdefault(line, []).append(k)

    def sung(k: int) -> bool | None:
        frames = voiced[int(times[k][0] / hop):int(times[k][1] / hop) + 1]
        return any(frames) if frames else None            # None: beyond the audio, nothing to judge

    silent = set()
    for line, ks in words_of.items():
        verdicts = [v for v in (sung(k) for k in ks) if v is not None]
        if verdicts and sum(verdicts) / len(verdicts) < SILENT_LINE_VOICED:
            silent.add(line)
    return silent


def measure(
    starts: list[float], line_of_word: list[int], evidence: dict[int, float], line_count: int,
    silent: set[int] | frozenset[int] = frozenset(),
) -> Precision:
    errors: dict[int, list[float]] = {}
    for k, heard_at in evidence.items():
        errors.setdefault(line_of_word[k], []).append(starts[k] - heard_at)
    flat = [e for errs in errors.values() for e in errs]
    lines = len(set(line_of_word))
    off = sorted({line + 1 for line, errs in errors.items() if abs(statistics.median(errs)) > CLEARLY_OFF_SECONDS}
                 | {line + 1 for line in silent})
    share = sum(abs(e) <= WITHIN_SECONDS for e in flat) / len(flat) if flat else 0.0
    return Precision(share, len(flat), lines, len(off), lines - len(errors), tuple(off), tuple(sorted(line + 1 for line in silent)))


_CAP_SECONDS = 3.0             # one wildly wrong word must not outweigh a whole line of good ones
_SWITCH_COST = 0.05            # switching candidates between lines costs a little, so a line with no evidence follows its neighbour
_ANCHORED_COST = 0.001         # ...and a tie goes to the whole-song pass


def blend(
    whole: list[tuple[float, float]], anchored: list[tuple[float, float]], line_of_word: list[int],
    evidence: dict[int, float], line_count: int,
) -> list[tuple[float, float]] | None:
    """Each line from whichever alignment sits closer to what was heard, choosing the combination with the least total
    error that keeps the lines in order (a dynamic-programming pass over the lines). None only when the whole-song
    alignment itself is out of order."""
    candidates = (whole, anchored)
    if any(whole[k][0] > whole[k + 1][0] for k in range(len(whole) - 1)):
        return None
    words_of: dict[int, list[int]] = {}
    for k, line in enumerate(line_of_word):
        words_of.setdefault(line, []).append(k)
    lines = sorted(words_of)
    if not lines:
        return list(whole)

    def cost(line: int, which: int) -> float:
        errors = [min(abs(candidates[which][k][0] - evidence[k]), _CAP_SECONDS) for k in words_of[line] if k in evidence]
        return sum(errors) + (_ANCHORED_COST if which else 0.0)

    def in_order(previous: int, which_previous: int, line: int, which: int) -> bool:
        return candidates[which_previous][words_of[previous][-1]][0] <= candidates[which][words_of[line][0]][0]

    best = [{w: (cost(lines[0], w), None) for w in (0, 1)}]
    for i in range(1, len(lines)):
        row = {}
        for w in (0, 1):
            options = [
                (best[i - 1][pw][0] + (_SWITCH_COST if pw != w else 0.0), pw)
                for pw in best[i - 1] if in_order(lines[i - 1], pw, lines[i], w)
            ]
            if options:
                score, came_from = min(options)
                row[w] = (score + cost(lines[i], w), came_from)
        best.append(row)
    if not best[-1]:
        return list(whole)
    which = min(best[-1], key=lambda w: best[-1][w][0])
    chosen: dict[int, int] = {}
    for i in range(len(lines) - 1, -1, -1):
        chosen[lines[i]] = which
        which = best[i][which][1] if best[i][which][1] is not None else which
    mixed = [candidates[chosen[line_of_word[k]]][k] for k in range(len(whole))]
    # Two alignments can each be valid yet overlap where they are joined (a word ending after the next one starts), which
    # the renderer's sanity check refuses (Respect): cut the earlier word where the next begins.
    return [(s, min(e, mixed[k + 1][0]) if k + 1 < len(mixed) else e) for k, (s, e) in enumerate(mixed)]


def _passes(precision: Precision) -> bool:
    # a line highlighted while nobody sings is definitely wrong; the 15% allowance is for shaky evidence, not for that
    return (precision.share >= NEEDED_SHARE and precision.off_lines <= MAX_OFF_LINE_SHARE * precision.lines
            and not precision.silent_line_numbers)


def choose_alignment(
    candidates: dict[str, list[tuple[float, float]]], line_of_word: list[int], evidence: dict[int, float], line_count: int,
    loudness: list[float] | None = None, hop: float = 0.5,
) -> AlignmentChoice | None:
    """The most precise candidate (a per-line blend of 'whole-song' and 'anchored' is tried too; ties go to the
    whole-song pass), and whether it is precise enough to trust. None when too few words were heard to judge."""
    if len(evidence) < MIN_MATCHED_WORDS:
        return None
    in_order = {n: times for n, times in candidates.items() if all(times[k][0] <= times[k + 1][0] for k in range(len(times) - 1))}
    candidates = in_order or candidates             # a timeline that runs backwards cannot be rendered
    options = dict(candidates)
    if "whole-song" in candidates and "anchored" in candidates:
        mixed = blend(candidates["whole-song"], candidates["anchored"], line_of_word, evidence, line_count)
        if mixed is not None and mixed not in (candidates["whole-song"], candidates["anchored"]):
            options = {"whole-song": candidates["whole-song"], "blended": mixed, "anchored": candidates["anchored"]}
    scored = {
        name: measure([t[0] for t in times], line_of_word, evidence, line_count,
                      silent_lines(times, line_of_word, loudness or [], hop))
        for name, times in options.items()
    }
    best = max(scored, key=lambda name: (round(scored[name].share, 3), -scored[name].off_lines))
    precision = scored[best]
    if _passes(precision):
        return AlignmentChoice(best, precision, True, "", options[best])
    shown = ", ".join(str(n) for n in precision.off_line_numbers[:8]) + (", ..." if len(precision.off_line_numbers) > 8 else "")
    which = f" (lines {shown})" if precision.off_line_numbers else ""
    silent = (" Lines " + ", ".join(str(n) for n in precision.silent_line_numbers[:8])
              + " sit where nobody is singing (a guitar solo or a gap), so the file lists lines that are not sung there.") if precision.silent_line_numbers else ""
    return AlignmentChoice(
        best, precision, False,
        f"SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only {precision.share:.0%} of the words start "
        f"within half a second of where they are sung, and {precision.off_lines} of the {precision.lines} lines are "
        f"clearly off (by more than a second){which}. A line the singer never sings, or sings elsewhere, cannot be timed."
        f"{silent} The video is made but not uploaded; watch it and correct the lyrics or timing.",
        options[best],
    )
