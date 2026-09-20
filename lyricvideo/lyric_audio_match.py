"""Scores candidate lyric text against what a speech recognizer (Whisper, see
transcribe.py) actually heard in the isolated vocal stem. Pure logic, no I/O.

Why this exists: forced alignment (align.py) squeezes ANY text onto the audio
without complaint, and lyric_accuracy.py only asks Claude whether the text looks
like real lyrics for that title -- neither ever checks the text against the
recording, so a different edition or a mixed-up verse/chorus sailed through.

Design notes:
- Words are matched IN ORDER (a longest-common-subsequence with fuzzy word
  equality, since Whisper mishears sung words), so lyrics with the verse and
  chorus in the wrong order lose the block that is out of place.
- Whisper collapses an immediate repeat (a line or a whole chorus sung twice in
  a row is often transcribed once), so an immediately repeated line/block in the
  lyric file inherits the support of its first copy instead of counting as
  missing. A repeat separated from its first copy by other lines is NOT excused.
- A candidate passes only if overall coverage AND the longest run of consecutive
  unsupported lines are both within bounds -- an isolated misheard line is
  normal recognizer noise, a sustained run is a wrong stretch of lyrics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

_WORD_RE = re.compile(r"[a-z0-9']+")
# "(Just like a prayer)" -- a parenthesized backing vocal. Quiet echoes the
# recognizer can't hear ('Like a Prayer' had 30 such lines and looked 92% wrong),
# so they are not required to be heard (an unclosed "(" runs to the line end).
_BACKING_RE = re.compile(r"\([^)]*(?:\)|$)")
# Sung sounds with no words to recognize: "whoa oh-oh-oh", "mmm", "yeah", "la la".
_VOCALISATION_RE = re.compile(r"^(?:m+|h?m+|o+h*|w?h?o+a*h*|a+h+|ooh+|uh+|ye?a+h*|hey+|la+|na+|da+|wo+|ha+|ay+|oo+)$")

# Very common English function words (and sung filler) land in order by pure
# chance -- lyrics for an entirely different song still "matched" 35% of one
# transcript through "the/you/a" alone -- so matching and coverage are scored on
# content words only. Repeat detection still compares whole lines.
_FILLER_WORDS = frozenset(
    "a an and are as at be but by do for from had has have he her him his i if in is it its me my no "
    "not of oh on or our she so that the them then they this to up us was we were what when will with "
    "you your yeah hey la na ah ooh uh all can just like now out there".split()
)

# Strict on purpose (owner, 2026-09-19: "if it's not perfect it needs to be rejected
# for review"): a candidate that can't be confirmed is flagged, never passed.
MIN_COVERAGE = 0.70
MAX_UNSUPPORTED_RUN = 3
# The other direction: a run of consecutive SUNG content words that no lyric line
# explains means a verse/chorus is sung that the file doesn't have. Real songs
# measured 0-3 (a stray recognized phrase); a missing 6-line verse measured 13-21.
MAX_UNEXPLAINED_SUNG_WORDS = 12
_LINE_SUPPORTED_FRACTION = 0.5
_MAX_REPEAT_BLOCK_LINES = 4
_FUZZY_WORD_RATIO = 0.75


@dataclass(frozen=True)
class AudioMatch:
    coverage: float                    # fraction of the (non-repeat) lyric words heard in order
    heard_words: int                   # how many words the recognizer produced at all
    line_supported: list[bool] = field(default_factory=list)   # one flag per lyric line
    worst_run: int = 0                 # longest run of consecutive unsupported lines
    worst_heard_gap: int = 0           # longest run of sung content words no lyric line explains
    gap_text: str = ""                 # the words of that longest unexplained run (for a reviewer)
    unsupported_ranges: list[tuple[int, int]] = field(default_factory=list)  # 1-based, inclusive


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _content(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _FILLER_WORDS and not _VOCALISATION_RE.match(t)]


@lru_cache(maxsize=200_000)
def _words_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4 or abs(len(a) - len(b)) > 2 or a[0] != b[0]:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _FUZZY_WORD_RATIO


def _matched_pairs(lyric: list[str], heard: list[str]) -> list[tuple[int, int]]:
    """(lyric index, heard index) pairs on a longest in-order match, both indexes strictly increasing."""
    n, m = len(lyric), len(heard)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, below = table[i], table[i + 1]
        for j in range(m - 1, -1, -1):
            if _words_match(lyric[i], heard[j]):
                row[j] = below[j + 1] + 1
            else:
                row[j] = max(below[j], row[j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if _words_match(lyric[i], heard[j]) and table[i][j] == table[i + 1][j + 1] + 1:
            pairs.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _matched_lyric_positions(lyric: list[str], heard: list[str]) -> set[int]:
    """Indexes of `lyric` words that fall on a longest in-order match against `heard`."""
    return {i for i, _ in _matched_pairs(lyric, heard)}


def _collapse_loops(tokens: list[str], max_ngram: int = 8) -> list[str]:
    """Whisper sometimes loops on non-vocal noise ("i'm gonna be with you" x5 over an
    orchestral finale). Any run of 1-8 words repeated 3+ times in a row is trimmed to two
    copies: a loop is one bad guess, and two copies keep a genuinely doubled line intact."""
    out = list(tokens)
    for n in range(1, max_ngram + 1):
        i = 0
        while i + 3 * n <= len(out):
            if out[i:i + n] == out[i + n:i + 2 * n] == out[i + 2 * n:i + 3 * n]:
                del out[i + 2 * n:i + 3 * n]
            else:
                i += 1
    return out


def _worst_unexplained_run(heard: list[str], lyric_words: list[str]) -> tuple[int, list[str]]:
    """(length, words) of the longest run of consecutive heard words no lyric word explains."""
    explained = _matched_lyric_positions(heard, lyric_words) if heard and lyric_words else set()
    run = worst = worst_end = 0
    for i in range(len(heard)):
        run = 0 if i in explained else run + 1
        if run > worst:
            worst, worst_end = run, i + 1
    return worst, heard[worst_end - worst:worst_end]


def _repeat_owners(line_tokens: list[list[str]]) -> list[int]:
    """owner[i] = the index of the line whose support line i inherits: itself, or --
    when line i starts an IMMEDIATE repeat of the block just before it (up to
    _MAX_REPEAT_BLOCK_LINES lines) -- the earlier copy it repeats."""
    owner = list(range(len(line_tokens)))
    i = 0
    while i < len(line_tokens):
        for k in range(1, _MAX_REPEAT_BLOCK_LINES + 1):
            if i - k < 0 or i + k > len(line_tokens):
                continue
            block = line_tokens[i:i + k]
            if all(block[j] for j in range(k)) and block == line_tokens[i - k:i]:
                for j in range(k):
                    owner[i + j] = owner[i - k + j]
                i += k
                break
        else:
            i += 1
    return owner


def score_lyrics_against_transcript(lyric_lines: list[str], heard_text: str) -> AudioMatch:
    heard_all = _tokens(heard_text)
    heard = _content(heard_all)
    line_tokens = [_tokens(line) for line in lyric_lines]
    owner = _repeat_owners(line_tokens)
    content_tokens = [_content(_tokens(_BACKING_RE.sub(" ", line))) for line in lyric_lines]

    flat: list[str] = []
    line_of_word: list[int] = []
    for idx, tokens in enumerate(content_tokens):
        if owner[idx] == idx:
            flat.extend(tokens)
            line_of_word.extend([idx] * len(tokens))

    matched = _matched_lyric_positions(flat, heard) if flat and heard else set()

    words_in_line = [0] * len(line_tokens)
    matched_in_line = [0] * len(line_tokens)
    for pos, idx in enumerate(line_of_word):
        words_in_line[idx] += 1
        matched_in_line[idx] += pos in matched

    # A line with ONLY backing words ("(Every move you make)") is optional: supported when
    # those words were in fact heard (matched against every lyric word in order), otherwise
    # neutral. Real ('Every Breath You Take'): ignoring heard backing lines entirely turned
    # a 26-line section into one unsupported run.
    full_tokens = [_content(_tokens(line)) for line in lyric_lines]
    full_flat: list[str] = []
    full_line_of_word: list[int] = []
    for idx, tokens in enumerate(full_tokens):
        if owner[idx] == idx:
            full_flat.extend(tokens)
            full_line_of_word.extend([idx] * len(tokens))
    matched_full = _matched_lyric_positions(full_flat, heard) if full_flat and heard else set()
    full_words_in_line = [0] * len(line_tokens)
    full_matched_in_line = [0] * len(line_tokens)
    for pos, idx in enumerate(full_line_of_word):
        full_words_in_line[idx] += 1
        full_matched_in_line[idx] += pos in matched_full

    own_support: dict[int, bool | None] = {}
    for idx, tokens in enumerate(content_tokens):
        if owner[idx] == idx:
            if tokens:
                own_support[idx] = matched_in_line[idx] / words_in_line[idx] >= _LINE_SUPPORTED_FRACTION
            elif full_words_in_line[idx] and (
                full_matched_in_line[idx] / full_words_in_line[idx] >= _LINE_SUPPORTED_FRACTION
            ):
                own_support[idx] = True
            else:
                # None = nothing verifiable ("oh no no", a note marker, unheard backing):
                # follows its neighbors below.
                own_support[idx] = None
    # A fade-out vamp (3+ identical lines in a row) the recognizer can't hear is excused when
    # the very same line was heard and matched elsewhere in the song ('Billie Jean' repeats
    # one line ~18 times over a fade). A phrase that appears nowhere else stays unsupported.
    members_of: dict[int, int] = {}
    for idx in range(len(line_tokens)):
        members_of[owner[idx]] = members_of.get(owner[idx], 0) + 1
    for root, count in members_of.items():
        if count >= 3 and own_support.get(root) is False and any(
            line_tokens[j] == line_tokens[root] and owner[j] != root and own_support.get(owner[j])
            for j in range(len(line_tokens))
        ):
            own_support[root] = True
    resolved = [own_support[owner[idx]] for idx in range(len(line_tokens))]
    line_supported = []
    for idx, value in enumerate(resolved):
        if value is None:
            before = next((v for v in reversed(resolved[:idx]) if v is not None), None)
            after = next((v for v in resolved[idx + 1:] if v is not None), None)
            value = bool(before) or bool(after)
        line_supported.append(value)

    ranges: list[tuple[int, int]] = []
    start = None
    for idx, ok in enumerate(line_supported + [True]):
        if not ok and start is None:
            start = idx
        elif ok and start is not None:
            ranges.append((start + 1, idx))
            start = None
    worst_run = max((b - a + 1 for a, b in ranges), default=0)

    # Every lyric word -- repeats AND parenthesized backing words included: backing words need
    # not be heard, but when they are (or when Whisper transcribes both copies of a repeat)
    # they still explain what was sung.
    every_lyric_word = [w for line in lyric_lines for w in _content(_tokens(line))]
    # Loops are trimmed ONLY here: a hallucinated loop must not count as a missing verse,
    # but a real repeated line must still be there to support the lyric lines.
    gap, gap_words = _worst_unexplained_run(_collapse_loops(heard), every_lyric_word)
    return AudioMatch(
        coverage=len(matched) / len(flat) if flat else 0.0,
        heard_words=len(heard_all),
        line_supported=line_supported,
        worst_run=worst_run,
        worst_heard_gap=gap,
        gap_text=" ".join(gap_words),
        unsupported_ranges=ranges,
    )


def audio_match_passes(match: AudioMatch) -> bool:
    return (
        bool(match.line_supported)
        and match.heard_words > 0
        and match.coverage >= MIN_COVERAGE
        and match.worst_run <= MAX_UNSUPPORTED_RUN
        and match.worst_heard_gap <= MAX_UNEXPLAINED_SUNG_WORDS
    )


def describe_mismatch(match: AudioMatch) -> str:
    """One owner-facing sentence for the review queue's concern text."""
    if not match.line_supported:
        return "There is no lyric text to check against the audio."
    if match.heard_words == 0:
        return "No words were recognized in the singing, so the lyrics could not be checked against the audio."
    parts = []
    if match.coverage < MIN_COVERAGE or match.worst_run > MAX_UNSUPPORTED_RUN:
        parts.append(f"Only {match.coverage:.0%} of these lyrics match what is sung")
        if match.unsupported_ranges:
            spans = [f"{a}-{b}" if a != b else str(a) for a, b in match.unsupported_ranges[:6]]
            noun = "line" if len(spans) == 1 and "-" not in spans[0] else "lines"
            parts.append(f"{noun} {', '.join(spans)} don't match the audio")
    if match.worst_heard_gap > MAX_UNEXPLAINED_SUNG_WORDS:
        parts.append(
            f"about {match.worst_heard_gap} sung words in a row aren't in these lyrics "
            "(a verse or chorus may be missing)"
        )
    return "; ".join(parts) + "."


def audio_match_badness(match: AudioMatch) -> float:
    """0 for a candidate that passes; otherwise how far past each limit it is, as a fraction of that limit,
    summed. Used to keep the LEAST bad candidate when no source passes. Coverage alone chose a shorter
    version that omitted a verse (0.82) over the complete lyrics that failed one stretch narrowly (0.78) --
    'Night Moves', 2026-09-19."""
    if not match.line_supported:
        return float("inf")
    return (
        max(0.0, MIN_COVERAGE - match.coverage) / MIN_COVERAGE
        + max(0, match.worst_run - MAX_UNSUPPORTED_RUN) / MAX_UNSUPPORTED_RUN
        + max(0, match.worst_heard_gap - MAX_UNEXPLAINED_SUNG_WORDS) / MAX_UNEXPLAINED_SUNG_WORDS
    )


CREDIT_STAMP_SECONDS = 3.0      # a provider stamps credits within the first seconds of the file...
CREDIT_SILENT_LEAD = 3.0        # ...and a real first line is never stamped this much before the first singing
MAX_LEADING_CREDITS = 4
_LOUD_HOP_FRACTION = 0.10       # a hop of the vocal track counts as singing at this share of the song's loud level


def drop_unsung_leading_lines(lines, times, heard, loudness, hop):
    """Leading lines that are not part of the audio: (lines, times, dropped). A lyrics provider's credits (composer,
    contributor, producer...) arrive as fake lyric lines stamped near 0:00, well before the first singing, with nothing
    heard near them. Any wording qualifies -- owner, 2026-09-20: "if its not part of the audio, its a credit". Needs the
    source's own line times and audio evidence; only a leading run is considered, at most MAX_LEADING_CREDITS, and one
    real line always remains."""
    if not times or len(times) != len(lines) or not (heard or loudness):
        return lines, times, []
    onsets = [h.start for h in heard]
    if loudness:
        ordered = sorted(loudness)
        reference = ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]
        onsets += [i * hop for i, level in enumerate(loudness) if reference > 0 and level >= _LOUD_HOP_FRACTION * reference][:1]
    if not onsets:
        return lines, times, []
    first_sung = min(onsets)
    dropped = 0
    while (dropped < min(MAX_LEADING_CREDITS, len(lines) - 1) and times[dropped] <= CREDIT_STAMP_SECONDS
           and times[dropped] < first_sung - CREDIT_SILENT_LEAD):
        dropped += 1
    return lines[dropped:], times[dropped:], lines[:dropped]


CREDIT_TRAILING_GAP = 3.0       # a line stamped this long after the last singing ended is metadata, not a lyric
MAX_TRAILING_CREDITS = 12


def drop_unsung_trailing_lines(lines, times, heard, loudness, hop):
    """The mirror of drop_unsung_leading_lines: credits at the END ('Lead Vocals : Don Henley', 'Strings : London
    Philharmonic Orchestra' -- Desperado, 2026-09-20, eight of them stamped 205-212 s after the singing ended at 194 s).
    A trailing line stamped more than CREDIT_TRAILING_GAP after the last heard or sung moment is dropped; only a trailing
    run, at most MAX_TRAILING_CREDITS, and one real line always remains. Returns (lines, times, dropped)."""
    if not times or len(times) != len(lines) or not (heard or loudness):
        return lines, times, []
    last_sung = [h.end for h in heard]
    if loudness:
        reference = sorted(loudness)[min(len(loudness) - 1, int(0.99 * len(loudness)))]
        voiced = [i for i, level in enumerate(loudness) if reference > 0 and level >= _LOUD_HOP_FRACTION * reference]
        if voiced:
            last_sung.append((voiced[-1] + 1) * hop)
    if not last_sung:
        return lines, times, []
    limit = max(last_sung) + CREDIT_TRAILING_GAP
    keep = len(lines)
    while keep > 1 and len(lines) - keep < MAX_TRAILING_CREDITS and times[keep - 1] > limit:
        keep -= 1
    return lines[:keep], times[:keep], lines[keep:]
