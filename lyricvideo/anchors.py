"""Where in the song does each lyric line REALLY sit, according to what Whisper heard (with word timings)?

Why: forced alignment (align.py) runs the whole song as one CTC pass. When the audio holds material the
text lacks (extra repeated choruses, ad-libs) or the text repeats lines differently, the pass drifts and every
later line lands far from the truth -- 'Girls Just Want to Have Fun' showed lines 20-50 s early, and the aligner
cannot notice. Whisper's word times give an independent, coarse position for most lines; those ANCHORS let the
aligner work one bounded window at a time so it can refine a line's timing but never wander off (see
align.align_words_anchored). Pure logic, no I/O.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

from .lyric_audio_match import _content, _matched_pairs, _tokens

_WORD_SECONDS = 0.35        # typical sung word length, for estimating where a line's unheard words sit
MIN_ANCHOR_FRACTION = 0.5   # a line is anchored when at least this share of its content words were heard...
MIN_ANCHOR_WORDS = 2        # ...and at least this many (one matching word is too easily a coincidence)


@dataclass(frozen=True)
class HeardWord:
    word: str
    start: float
    end: float


@dataclass(frozen=True)
class LineAnchor:
    line: int
    start: float        # estimated start of the whole line (allows for unheard words at its front)
    end: float          # estimated end of the whole line
    matched: int        # content words of the line that were heard, in order
    total: int          # content words in the line


@dataclass(frozen=True)
class Block:
    first: int          # first lyric line in the block
    last: int           # last lyric line in the block (inclusive)
    start: float        # audio window the block's words must fit inside
    end: float


def line_anchors(lines: list[str], heard: list[HeardWord]) -> dict[int, LineAnchor]:
    lyric_tokens: list[tuple[str, int, int]] = []       # (token, line, word index within the line)
    word_counts: list[int] = []
    for li, line in enumerate(lines):
        words = line.split()
        word_counts.append(len(words))
        for wi, word in enumerate(words):
            lyric_tokens.extend((tok, li, wi) for tok in _content(_tokens(word)))
    heard_tokens = [(tok, hw.start, hw.end) for hw in heard for tok in _content(_tokens(hw.word))]
    if not lyric_tokens or not heard_tokens:
        return {}

    hits: dict[int, list[tuple[int, float, float]]] = defaultdict(list)
    for i, j in _matched_pairs([t for t, _, _ in lyric_tokens], [t for t, _, _ in heard_tokens]):
        _, li, wi = lyric_tokens[i]
        hits[li].append((wi, heard_tokens[j][1], heard_tokens[j][2]))
    totals = Counter(li for _, li, _ in lyric_tokens)

    anchors: dict[int, LineAnchor] = {}
    previous_start = 0.0
    for li in sorted(hits):
        matched, total = len(hits[li]), totals[li]
        if total < MIN_ANCHOR_WORDS or matched < max(MIN_ANCHOR_WORDS, math.ceil(MIN_ANCHOR_FRACTION * total)):
            continue
        first_wi, first_start, _ = min(hits[li])
        last_wi, _, last_end = max(hits[li])
        start = max(previous_start, first_start - first_wi * _WORD_SECONDS)
        end = max(start + _WORD_SECONDS, last_end + (word_counts[li] - 1 - last_wi) * _WORD_SECONDS)
        anchors[li] = LineAnchor(li, start, end, matched, total)
        previous_start = start
    return anchors


def plan_windows(n_lines: int, anchors: dict[int, LineAnchor], audio_seconds: float, pad: float) -> list[Block]:
    """Split the lyric lines into blocks, each with the stretch of audio its words must fit inside: an
    anchored line gets its anchor +/- pad; the unanchored lines between two anchors share the gap between them
    (also padded); the lines before the first / after the last anchor are bounded by the song's ends."""
    def clamp(t: float) -> float:
        return min(max(t, 0.0), audio_seconds)

    def block(first: int, last: int, start: float, end: float) -> Block:
        start, end = clamp(start), clamp(end)
        if end <= start:                                    # degenerate: give the words at least a second
            start = max(0.0, end - 1.0)
            end = max(end, min(audio_seconds, start + 1.0))
        return Block(first, last, start, end)

    if n_lines <= 0:
        return []
    if not anchors:
        return [Block(0, n_lines - 1, 0.0, audio_seconds)]

    blocks: list[Block] = []
    indexes = sorted(anchors)
    if indexes[0] > 0:
        blocks.append(block(0, indexes[0] - 1, 0.0, anchors[indexes[0]].start + pad))
    for k, li in enumerate(indexes):
        a = anchors[li]
        blocks.append(block(li, li, a.start - pad, a.end + pad))
        following = indexes[k + 1] if k + 1 < len(indexes) else None
        if following is None:
            if li < n_lines - 1:
                blocks.append(block(li + 1, n_lines - 1, a.end - pad, audio_seconds))
        elif following > li + 1:
            blocks.append(block(li + 1, following - 1, a.end - pad, anchors[following].start + pad))
    return blocks


_SILENCE_FRACTION = 0.03    # a stretch quieter than this share of the song's own loud level is silence


def drop_words_in_silence(heard: list[HeardWord], loudness: list[float], hop: float) -> list[HeardWord]:
    """Whisper 'hears' words in silence (real: 'just wanna, just wanna' at 222-228 s of 'Girls Just Want to Have
    Fun', where the vocal track was silent after ~220 s) -- a hallucination that would anchor a line in the wrong
    place. `loudness` is the vocal track's level per `hop` seconds; a word is kept if any part of it is voiced.
    Judged against the song's own loud level, so a quiet recording is not mistaken for silence."""
    if not loudness:
        return heard
    ordered = sorted(loudness)
    reference = ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]
    if reference <= 0:
        return heard
    threshold = _SILENCE_FRACTION * reference

    def voiced(word: HeardWord) -> bool:
        first, last = int(word.start / hop), int(word.end / hop)
        window = loudness[first:last + 1]
        return bool(window) and max(window) >= threshold

    return [w for w in heard if voiced(w)]


_RUN_TOLERANCE = 3.0        # anchors whose offset from lrclib agrees within this many seconds belong to one run
_MIN_TRUSTED_RUN = 3        # a run must be at least this long to be believed over lrclib...
_SHIFT_STEP = 10.0          # ...plus one more anchor for every this many seconds it sits from the dominant offset


def combine_anchors(
    whisper: dict[int, LineAnchor], line_times: list[float] | None, word_counts: list[int],
) -> dict[int, LineAnchor]:
    """Whisper's anchors, checked against lrclib's line timestamps (a human-made second opinion).

    Text matching cannot tell which copy of a repeated chorus Whisper heard when the text repeats it more often
    than the audio does ('Girls Just Want to Have Fun': heard occurrences went to the wrong copies, 20-30 s off).
    lrclib's edition may run at a different offset (a longer intro), so the check is on CONSISTENCY: anchors whose
    offset from lrclib agrees form runs; a run of at least three is believed (a real section shift in the
    recording forms such a run too and is kept). Every other anchor -- an isolated one that contradicts lrclib --
    is replaced by lrclib's time plus the offset of the nearest believed anchors, and lines Whisper did not hear
    get the same. With no usable lrclib times, or no believable run, the Whisper anchors are returned unchanged."""
    n = len(word_counts)
    if not whisper or not line_times or len(line_times) != n:
        return whisper
    indexes = sorted(whisper)
    offset = {i: whisper[i].start - line_times[i] for i in indexes}

    runs: list[list[int]] = []
    current: list[int] = []
    for i in indexes:
        if current and abs(offset[i] - statistics.median(offset[j] for j in current)) <= _RUN_TOLERANCE:
            current.append(i)
        else:
            if current:
                runs.append(current)
            current = [i]
    if current:
        runs.append(current)
    # The longest run sets the baseline offset; any other run must be longer the further it sits from that
    # baseline (a 33 s jump backed by three anchors is far likelier three wrong chorus copies than a real shift).
    longest = max(runs, key=len)
    if len(longest) < _MIN_TRUSTED_RUN:
        return whisper
    baseline = statistics.median(offset[j] for j in longest)
    trusted = sorted(
        i for run in runs
        if len(run) >= _MIN_TRUSTED_RUN + int(abs(statistics.median(offset[j] for j in run) - baseline) // _SHIFT_STEP)
        for i in run
    )

    combined: dict[int, LineAnchor] = {}
    previous_start = 0.0
    for i in range(n):
        if i in whisper and i in trusted:
            anchor = whisper[i]
        else:
            near = [j for j in trusted if j < i][-2:] + [j for j in trusted if j > i][:2]
            start = line_times[i] + statistics.median(offset[j] for j in near)
            gap = line_times[i + 1] - line_times[i] if i + 1 < n else 4.0
            end = start + max(0.5, min(gap, word_counts[i] * 0.5 + 0.5))
            anchor = LineAnchor(i, start, end, 0, word_counts[i])
        start = max(previous_start, anchor.start)
        anchor = LineAnchor(anchor.line, start, max(anchor.end, start + 0.35), anchor.matched, anchor.total)
        combined[i] = anchor
        previous_start = start
    return combined
