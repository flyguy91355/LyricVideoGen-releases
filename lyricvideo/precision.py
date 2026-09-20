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


@dataclass(frozen=True)
class AlignmentChoice:
    method: str                # "whole-song" | "anchored" | "blended"
    precision: Precision
    ok: bool
    concern: str               # "" when ok, otherwise why the song is set aside
    times: list[tuple[float, float]]


def _normal(word: str) -> str:
    return re.sub(r"[^a-z']", "", word.lower())


def match_words(words: list[str], heard: list[HeardWord]) -> dict[int, float]:
    """Index of each lyric word (flat, song order) that Whisper also heard, in order -> the time it was heard."""
    ours = [(i, _normal(w)) for i, w in enumerate(words)]
    ours = [(i, w) for i, w in ours if w]
    theirs = [(h, _normal(h.word)) for h in heard]
    theirs = [(h, w) for h, w in theirs if w]
    matcher = difflib.SequenceMatcher(None, [w for _, w in ours], [w for _, w in theirs], autojunk=False)
    return {
        ours[i + k][0]: theirs[j + k][0].start
        for i, j, size in matcher.get_matching_blocks() for k in range(size)
    }


def measure(starts: list[float], line_of_word: list[int], evidence: dict[int, float], line_count: int) -> Precision:
    errors: dict[int, list[float]] = {}
    for k, heard_at in evidence.items():
        errors.setdefault(line_of_word[k], []).append(starts[k] - heard_at)
    flat = [e for errs in errors.values() for e in errs]
    lines = len(set(line_of_word))
    off = sorted(line + 1 for line, errs in errors.items() if abs(statistics.median(errs)) > CLEARLY_OFF_SECONDS)
    share = sum(abs(e) <= WITHIN_SECONDS for e in flat) / len(flat) if flat else 0.0
    return Precision(share, len(flat), lines, len(off), lines - len(errors), tuple(off))


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
    return [candidates[chosen[line_of_word[k]]][k] for k in range(len(whole))]


def _passes(precision: Precision) -> bool:
    return precision.share >= NEEDED_SHARE and precision.off_lines <= MAX_OFF_LINE_SHARE * precision.lines


def choose_alignment(
    candidates: dict[str, list[tuple[float, float]]], line_of_word: list[int], evidence: dict[int, float], line_count: int,
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
    scored = {name: measure([t[0] for t in times], line_of_word, evidence, line_count) for name, times in options.items()}
    best = max(scored, key=lambda name: (round(scored[name].share, 3), -scored[name].off_lines))
    precision = scored[best]
    if _passes(precision):
        return AlignmentChoice(best, precision, True, "", options[best])
    shown = ", ".join(str(n) for n in precision.off_line_numbers[:8]) + (", ..." if len(precision.off_line_numbers) > 8 else "")
    which = f" (lines {shown})" if precision.off_line_numbers else ""
    return AlignmentChoice(
        best, precision, False,
        f"SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only {precision.share:.0%} of the words start "
        f"within half a second of where they are sung, and {precision.off_lines} of the {precision.lines} lines are "
        f"clearly off (by more than a second){which}. A line the singer never sings, or sings elsewhere, cannot be timed. "
        "The video is made but not uploaded; watch it and correct the lyrics or timing.",
        options[best],
    )
