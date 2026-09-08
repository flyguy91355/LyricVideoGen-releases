from __future__ import annotations

from pathlib import Path

DEFAULT_DICTIONARY_PATH = Path("/usr/share/dict/words")


def load_dictionary(path: Path = DEFAULT_DICTIONARY_PATH) -> set[str]:
    words = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return {w.strip().lower() for w in words if w.strip()}


def _is_dictionary_word(token: str, dictionary: set[str]) -> bool:
    if any(c.isdigit() for c in token):
        # A token with digits stripped down to a stray letter (e.g. "2S25-5555"
        # -> "s") can accidentally match a real single-letter dictionary entry --
        # never treat a token containing digits as a real word candidate.
        return False
    cleaned = "".join(c for c in token.lower() if c.isalpha())
    return bool(cleaned) and cleaned in dictionary


def filter_dictionary_lines(
    lines: list[str], dictionary: set[str], min_ratio: float = 0.6
) -> list[str]:
    """Drop lines where too few tokens are real dictionary words -- catches OCR
    gibberish (misread tab notation, chord diagrams, strumming symbols) that a
    plain alphabetic-character check lets through since the garbled tokens still
    happen to look like short alphabetic "words"."""
    result = []
    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        matched = sum(1 for t in tokens if _is_dictionary_word(t, dictionary))
        if matched / len(tokens) >= min_ratio:
            result.append(line)
    return result
