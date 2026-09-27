"""Small text helpers, ported from LyricChord's utils/text.py, used for fuzzy
title/artist matching during song identification and lyric lookup."""

from __future__ import annotations

import re
import unicodedata

_PAREN_NOISE = re.compile(
    r"[\(\[\{]\s*(official|lyrics?|lyric video|audio|video|hd|hq|remaster(ed)?( \d{4})?|"
    r"live|explicit|clean|mono|stereo|visuali[sz]er|4k|1080p|from .*)[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
# A FILENAME's leading track number (issue #7 review): only a number with a leading zero ("01 - ", "01.", "01_",
# "01 Song") or one or two digits whose separator is followed by a space ("1. Song", "12) Song", "3 - Song"). A
# number glued to more of the title is part of it ("19-2000", "5.15", "1-800-273-8255", "2-4-6-8 Motorway"), as is
# a bare number before a space ("99 Luftballons", "21 Guns"), and a three-digit number before " - " is far more
# often a band named with a number ("747 - Some Song") than a track past 99.
_TRACK_PREFIX = re.compile(r"^\s*(?:0\d{1,2}(?:\s*[-._)\]]\s*|\s+)|\d{1,2}\s*[-._)\]]\s+)")
_ARTIST_STOP_WORDS = {"the", "and", "n", "feat", "featuring", "ft", "with"}
_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}


def strip_title_noise(text: str) -> str:
    """'(Official Video)'-style noise, extra spaces and stray separators removed; a leading number is KEPT -- for a
    title from tags or MusicBrainz, which never carry a track number ("19-2000" is the title itself)."""
    text = _PAREN_NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -_")


def clean_title(text: str) -> str:
    """A title taken from a FILENAME: the noise strip_title_noise removes, plus a real track-number prefix (see
    _TRACK_PREFIX) and separator debris."""
    text = _PAREN_NOISE.sub("", text)
    text = _TRACK_PREFIX.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -_.")


def normalize(text: str) -> str:
    """Lower-case, strip accents and punctuation. Used for fuzzy matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def artist_key(name: str) -> str:
    """Comparison key for artist names: 'The Alan Parsons Project', 'Alan Parsons
    Project', 'Simon & Garfunkel' vs 'Simon and Garfunkel' all collapse to the same
    words."""
    return " ".join(w for w in normalize(name).split() if w not in _ARTIST_STOP_WORDS)


def smart_title_case(text: str) -> str:
    """'eye in the sky' -> 'Eye in the Sky'. Leaves mixed-case input untouched."""
    if text != text.lower():
        return text
    words = text.split()
    out = []
    for i, w in enumerate(words):
        if 0 < i < len(words) - 1 and w in _SMALL_WORDS:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)
