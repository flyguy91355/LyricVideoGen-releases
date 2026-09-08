from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from .models import ChordWord, InstrumentalBlock, LyricLine

CHORD_RE = re.compile(
    r'^[A-G](#|b)?(maj|min|m|dim|aug|sus)?\d{0,2}(/[A-G](#|b)?)?$'
)
CHORD_LINE_THRESHOLD = 0.6
Y_TOLERANCE = 3.0


class TabPdfError(Exception):
    pass


class NoTextLayerError(TabPdfError):
    pass


class NoChordLyricPairsError(TabPdfError):
    pass


def is_chord_token(token: str) -> bool:
    return bool(CHORD_RE.match(token.strip()))


def _cluster_words_into_lines(words: list[dict], y_tolerance: float = Y_TOLERANCE) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        placed = False
        for line in lines:
            if abs(line[0]["top"] - word["top"]) <= y_tolerance:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    lines.sort(key=lambda line: line[0]["top"])
    return lines


def _classify_line(line: list[dict]) -> str:
    tokens = [w["text"] for w in line]
    if not tokens:
        return "lyric"
    chord_count = sum(1 for t in tokens if is_chord_token(t))
    return "chord" if (chord_count / len(tokens)) >= CHORD_LINE_THRESHOLD else "lyric"


def _pair_chord_to_words(chord_line: list[dict], lyric_line: list[dict]) -> list[ChordWord]:
    result = [ChordWord(word=w["text"], chord=None) for w in lyric_line]
    for chord_word in chord_line:
        chord_center = (chord_word["x0"] + chord_word["x1"]) / 2
        best_idx, best_dist = None, None
        for i, lw in enumerate(lyric_line):
            lyric_center = (lw["x0"] + lw["x1"]) / 2
            dist = abs(lyric_center - chord_center)
            if best_dist is None or dist < best_dist:
                best_idx, best_dist = i, dist
        if best_idx is not None:
            result[best_idx].chord = chord_word["text"]
    return result


def _assemble_from_words(
    all_words: list[dict], y_tolerance: float, source_desc: str
) -> tuple[list[LyricLine], list[InstrumentalBlock]]:
    """Shared line-clustering/chord-pairing assembly, driven by a generic word
    dict list ({"text", "x0", "x1", "top"}) -- used both by the real-text-layer
    path (pdfplumber word coordinates, PDF point units) and the OCR fallback
    path (pytesseract word coordinates, pixel units), which is why the caller
    supplies its own y_tolerance rather than this function assuming PDF points.
    """
    lines = _cluster_words_into_lines(all_words, y_tolerance)
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
            words = [ChordWord(word=w["text"], chord=None) for w in line]
            result.append(LyricLine(words=words))
            i += 1
        elif kind == "chord":
            # A chord line with no lyric line right after it: an instrumental-only
            # progression (intro/break/outro), not tied to any sung words.
            chords = [w["text"] for w in line]
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
        raise NoChordLyricPairsError(
            f"{source_desc} has a text layer but no chord-over-lyric line pairs "
            "or plain lyric lines were found"
        )

    return result, instrumental_blocks


def parse_tab_pdf(pdf_path: Path) -> tuple[list[LyricLine], list[InstrumentalBlock]]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        all_words: list[dict] = []
        for page in pdf.pages:
            all_words.extend(page.extract_words())

    if not all_words:
        raise NoTextLayerError(
            f"{pdf_path} has no extractable text layer (scanned/image-only PDF?)"
        )

    return _assemble_from_words(all_words, Y_TOLERANCE, str(pdf_path))
