"""Music-theory helpers: pitch classes, chord templates, key estimation, spelling.

Ported from LyricChord's chords/theory.py. Chord "qualities" are kept deliberately
small (triads plus optional sevenths) so the on-screen chords stay playable for a
strumming guitarist/pianist.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .models import ChordEvent, ChordTrack

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


# Owner, 2026-09-23, the full arc: "the keys that have the most easy chords ... c major D major a minor",
# then "dont forget B and F major and minor", then the real rule stated plainly: "i dont want any flat or
# sharp keys at all in this.. those where the hard chords are" -- and finally, walked back on reflection:
# "you didnt have them, i added them, maybe i shouldnt have" / "lets do b an f hard". F and B are natural
# tonics, but neither has a true OPEN chord shape on guitar (F is at least a mini-barre, B at least a partial
# barre), so they're excluded. Only the 5 true open major shapes and 3 true open minor shapes count -- there
# is no simple open Cm or Gm either, which is exactly why the minor set is smaller than the major one. This
# is the SAME set capo_and_shape_key() uses as its own shape candidates, by construction, so the two rules
# (is this song's own key easy already / what capo converts a hard one) can never drift apart. Independently
# verified against guitar-chord.org's own published capo transposition chart.
_CAGED_MAJOR_SHAPES = {"C": 0, "D": 2, "E": 4, "G": 7, "A": 9}
_CAGED_MINOR_SHAPES = {"Am": 9, "Dm": 2, "Em": 4}


def is_easy_key(key: str) -> bool:
    tonic, _, mode = key.partition(" ")
    if mode == "major":
        return tonic in _CAGED_MAJOR_SHAPES
    if mode == "minor":
        return f"{tonic}m" in _CAGED_MINOR_SHAPES
    return False


def capo_and_shape_key(key: str) -> tuple[int, str] | None:
    """(capo_fret, shape_key) for a hard key -- e.g. ("F# major") -> (2, "E"), meaning "capo 2, play E
    shapes." None for a key that's already easy (is_easy_key) or unrecognized: nothing to convert."""
    if is_easy_key(key):
        return None
    tonic, _, mode = key.partition(" ")
    root = NOTES_SHARP.index(tonic) if tonic in NOTES_SHARP else (NOTES_FLAT.index(tonic) if tonic in NOTES_FLAT else None)
    if root is None or mode not in ("major", "minor"):
        return None
    candidates = _CAGED_MAJOR_SHAPES if mode == "major" else _CAGED_MINOR_SHAPES
    capo, shape = min(((root - pitch) % 12, name) for name, pitch in candidates.items())
    return None if capo == 0 else (capo, shape)


def parse_chord_label(label: str) -> tuple[int, str] | None:
    """(root, quality) for a real chord label like "F#m7" -> (6, "min7") -- the inverse of spell(). None for
    "N" (no-chord) or anything that doesn't parse as a real chord. Checks 2-letter note names (sharps/flats)
    before 1-letter ones, and longer quality suffixes ("maj7"/"min7") before shorter ones ("m"/"7") so "m7"
    is never mistaken for a bare "m" plus a stray "7"."""
    for note_list in (NOTES_SHARP, NOTES_FLAT):
        for note_len in (2, 1):
            candidate = label[:note_len]
            if candidate in note_list:
                root = note_list.index(candidate)
                suffix = label[note_len:]
                for quality, sfx in sorted(QUALITY_SUFFIX.items(), key=lambda kv: -len(kv[1])):
                    if sfx == suffix:
                        return root, quality
    return None


def transpose_chord_label(label: str, capo: int) -> str:
    """A chord label re-spelled `capo` semitones DOWN -- what you'd actually finger with that capo on to
    sound the original chord. "N" and anything unparseable pass through unchanged. Always spells with sharps:
    none of the CAGED shape keys (C D E G A Am Dm Em) are in _FLAT_MAJOR_TONICS, so this matches the app's
    own existing spelling convention exactly, with no separate flats-lookup needed."""
    parsed = parse_chord_label(label)
    if parsed is None:
        return label
    root, quality = parsed
    return spell((root - capo) % 12, quality, use_flats=False)


def transpose_chord_track(chord_track: ChordTrack, capo_fret: int, shape_key: str) -> ChordTrack:
    """The whole track re-spelled for a capo at `capo_fret`, fretting `shape_key`'s open shapes
    (e.g. ("D", 1) for Eb major -- see capo_and_shape_key). Timing is untouched -- only each
    event's own label is transposed (transpose_chord_label) and the track's key is renamed to
    the shape actually fretted, e.g. "D major"/"E minor" (a minor shape_key like "Em" carries its
    trailing "m", stripped here since ChordTrack.key spells minors as "<tonic> minor", the same
    convention key_name() uses elsewhere)."""
    events = [ChordEvent(start=e.start, end=e.end, label=transpose_chord_label(e.label, capo_fret))
              for e in chord_track.events]
    if shape_key.endswith("m"):
        key = f"{shape_key[:-1]} minor"
    else:
        key = f"{shape_key} major"
    return ChordTrack(events=events, key=key, bpm=chord_track.bpm)


EASY_CHORD_MARKER_FILENAME = "easy_chord_capo.json"


def save_easy_chord_capo_marker(
    work_dir: Path, capo_fret: int, shape_key: str, original_key: str, original_title: str,
) -> None:
    """Written by pipeline.build_capo_variant() into the `-capo` work dir it just built -- the one
    structured, durable record of "this song is an EASY CHORD (capo) variant" and what it was converted
    from/to. Read back by youtube_schedule.schedule_upload() (owner, 2026-09-23: "should maybe have that
    in the upload file too"), which can run days after the render -- a folder-name or title-suffix
    convention alone would work today but isn't something upload code should have to re-parse and trust
    indefinitely. `original_title` is the CLEAN pre-suffix title (the capo work dir's own song.title
    already carries the "EasyChords" filename suffix, needed for the real rendered .mp4's name, not for
    the YouTube title)."""
    Path(work_dir).joinpath(EASY_CHORD_MARKER_FILENAME).write_text(
        json.dumps({
            "capo_fret": capo_fret, "shape_key": shape_key,
            "original_key": original_key, "original_title": original_title,
        }),
        encoding="utf-8",
    )


def load_easy_chord_capo_marker(work_dir: Path) -> dict | None:
    """None for an ordinary song (no marker file, or an unreadable/corrupt one) -- never raises, since a
    marker-reading problem must never block or corrupt an otherwise-normal upload."""
    path = Path(work_dir) / EASY_CHORD_MARKER_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ordered_unique_chords(chord_track: ChordTrack) -> list[str]:
    """Every distinct chord label in the song, in first-seen order across ALL
    events (sung or instrumental) -- backs the chord fingering legend, which
    shows one diagram per chord regardless of whether it happens during vocals.
    'N' (no-chord) is excluded -- there's nothing to finger. Moved here from
    pipeline.py (2026-09-23, still re-exported from there) so a light module
    like youtube_playlists.py can count a song's chords -- for the 3-CHORD/
    4-CHORD playlists -- without importing all of pipeline.py's heavy deps."""
    labels: list[str] = []
    for event in chord_track.events:
        if event.label != "N" and event.label not in labels:
            labels.append(event.label)
    return labels
