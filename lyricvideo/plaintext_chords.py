from __future__ import annotations

import re

from .models import ChordWord, InstrumentalBlock, LyricLine
from .pdf_parse import CHORD_LINE_THRESHOLD, is_chord_token

_WORD_RE = re.compile(r"\S+")


def _word_positions(line: str) -> list[tuple[str, int, int]]:
    """(word, start_col, end_col) for each whitespace-separated word, using its
    real character column position -- the plain-text equivalent of the PDF
    parser's pixel x-coordinates."""
    return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(line)]


def _classify_line(line: str) -> str:
    tokens = line.split()
    if not tokens:
        return "lyric"
    chord_count = sum(1 for t in tokens if is_chord_token(t))
    return "chord" if (chord_count / len(tokens)) >= CHORD_LINE_THRESHOLD else "lyric"


def _pair_chord_to_words(chord_line: str, lyric_line: str) -> list[ChordWord]:
    lyric_words = _word_positions(lyric_line)
    result = [ChordWord(word=w, chord=None) for w, _, _ in lyric_words]
    for chord_text, c_start, c_end in _word_positions(chord_line):
        chord_center = (c_start + c_end) / 2
        best_idx, best_dist = None, None
        for i, (_, l_start, l_end) in enumerate(lyric_words):
            lyric_center = (l_start + l_end) / 2
            dist = abs(lyric_center - chord_center)
            if best_dist is None or dist < best_dist:
                best_idx, best_dist = i, dist
        if best_idx is not None:
            result[best_idx].chord = chord_text
    return result


def parse_plaintext_chords(text: str) -> tuple[list[LyricLine], list[InstrumentalBlock]]:
    """Parse owner-typed chord-over-lyric plain text (standard tab-site format)
    -- entirely deterministic text parsing, no Claude/vision call at all, so it
    can't hit the content-filtering issue vision-based PDF reading can for some
    songs' most iconic lines."""
    lines = [l for l in text.splitlines() if l.strip()]
    classified = [(line, _classify_line(line)) for line in lines]

    result: list[LyricLine] = []
    instrumental_blocks: list[InstrumentalBlock] = []
    i = 0
    while i < len(classified):
        line, kind = classified[i]
        if kind == "chord" and i + 1 < len(classified) and classified[i + 1][1] == "lyric":
            lyric_line = classified[i + 1][0]
            words = _pair_chord_to_words(line, lyric_line)
            result.append(LyricLine(words=words))
            i += 2
        elif kind == "lyric":
            words = [ChordWord(word=w, chord=None) for w, _, _ in _word_positions(line)]
            result.append(LyricLine(words=words))
            i += 1
        elif kind == "chord":
            chords = [w for w, _, _ in _word_positions(line)]
            if instrumental_blocks and instrumental_blocks[-1].before_line_index == len(result):
                instrumental_blocks[-1].chords.extend(chords)
            else:
                instrumental_blocks.append(
                    InstrumentalBlock(chords=chords, before_line_index=len(result))
                )
            i += 1
        else:
            i += 1

    if not result:
        raise ValueError("no chord-over-lyric line pairs or plain lyric lines found in the text")

    return result, instrumental_blocks
