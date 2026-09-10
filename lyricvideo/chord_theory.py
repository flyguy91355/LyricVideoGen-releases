"""Music-theory helpers: pitch classes, chord templates, key estimation, spelling.

Ported from LyricChord's chords/theory.py. Chord "qualities" are kept deliberately
small (triads plus optional sevenths) so the on-screen chords stay playable for a
strumming guitarist/pianist.
"""

from __future__ import annotations

import numpy as np

NOTES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# quality -> semitone intervals from the root
QUALITY_INTERVALS: dict[str, tuple[int, ...]] = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "min7": (0, 3, 7, 10),
    "maj7": (0, 4, 7, 11),
}
QUALITY_SUFFIX: dict[str, str] = {"maj": "", "min": "m", "7": "7", "min7": "m7", "maj7": "maj7"}

TRIADS = ["maj", "min"]
SEVENTHS = ["7", "min7", "maj7"]

# Krumhansl-Schmuckler key profiles.
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Keys conventionally written with flats (major tonics; minors via relative major).
_FLAT_MAJOR_TONICS = {5, 10, 3, 8, 1, 6}  # F Bb Eb Ab Db Gb

Chord = tuple[int, str]  # (root pitch class, quality)


def build_templates(qualities: list[str]) -> tuple[list[Chord], np.ndarray]:
    """Return chord list and a (n_chords, 12) matrix of unit-norm binary templates."""
    chords: list[Chord] = []
    rows = []
    for q in qualities:
        for root in range(12):
            vec = np.zeros(12)
            for iv in QUALITY_INTERVALS[q]:
                vec[(root + iv) % 12] = 1.0
            vec[root] = 1.15  # slight emphasis on the root
            rows.append(vec / np.linalg.norm(vec))
            chords.append((root, q))
    return chords, np.vstack(rows)


def estimate_key(chroma_mean: np.ndarray) -> tuple[int, str, float]:
    """Krumhansl-Schmuckler key finding. Returns (tonic_pc, 'major'|'minor', confidence)."""
    c = np.asarray(chroma_mean, dtype=float)
    if c.sum() <= 0:
        return 0, "major", 0.0
    best = (0, "major", -2.0)
    for tonic in range(12):
        for mode, profile in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            score = float(np.corrcoef(c, np.roll(profile, tonic))[0, 1])
            if score > best[2]:
                best = (tonic, mode, score)
    return best


def diatonic_chords(tonic: int, mode: str, include_sevenths: bool = False) -> set[Chord]:
    """Chords built on the scale degrees of the key (harmonic-minor V included)."""
    if mode == "major":
        degrees = [(0, "maj"), (2, "min"), (4, "min"), (5, "maj"), (7, "maj"), (9, "min")]
        sevenths = [(0, "maj7"), (2, "min7"), (4, "min7"), (5, "maj7"), (7, "7"), (9, "min7")]
    else:
        degrees = [(0, "min"), (3, "maj"), (5, "min"), (7, "min"), (7, "maj"), (8, "maj"), (10, "maj")]
        sevenths = [(0, "min7"), (3, "maj7"), (5, "min7"), (7, "7"), (8, "maj7"), (10, "7")]
    out = {((tonic + d) % 12, q) for d, q in degrees}
    if include_sevenths:
        out |= {((tonic + d) % 12, q) for d, q in sevenths}
    return out


def key_uses_flats(tonic: int, mode: str) -> bool:
    rel_major = tonic if mode == "major" else (tonic + 3) % 12
    return rel_major in _FLAT_MAJOR_TONICS


def key_name(tonic: int, mode: str, prefer_flats: bool = True) -> str:
    names = NOTES_FLAT if (prefer_flats and key_uses_flats(tonic, mode)) else NOTES_SHARP
    return f"{names[tonic]} {mode}"


def spell(root: int, quality: str, use_flats: bool) -> str:
    names = NOTES_FLAT if use_flats else NOTES_SHARP
    return names[root % 12] + QUALITY_SUFFIX.get(quality, quality)
