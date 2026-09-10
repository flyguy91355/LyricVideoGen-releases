"""Guitar chord fingering data, extracted from tombatossals/chords-db
(https://github.com/tombatossals/chords-db, MIT licensed) -- specifically
lib/guitar.json's `positions[i]` with the lowest `baseFret`, for every (root,
quality) combination lyricvideo.detect_chords() can produce (12 roots x the 5
qualities in chord_theory.QUALITY_SUFFIX). Not hand-authored -- see
docs/superpowers/specs/2026-09-09-chord-fingering-chart-design.md for the
extraction method and why this dataset was chosen over inventing the data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChordShape:
    frets: tuple[int, ...]    # 6 values, low E to high e. -1 = muted, 0 = open,
                               # >0 = fretted (counting rows from base_fret).
    fingers: tuple[int, ...]  # 6 values. 0 = no finger, 1-4 = finger number.
    base_fret: int = 1        # 1 = shape starts at the nut (frets are absolute
                               # fret numbers, a nut line is drawn). >1 = no nut
                               # line; show a "<base_fret>fr" label instead.


CHORD_SHAPES: dict[str, ChordShape] = {
    'A': ChordShape(frets=(-1, 0, 2, 2, 2, 0), fingers=(0, 0, 1, 2, 3, 0), base_fret=1),
    'A#': ChordShape(frets=(-1, 1, 3, 3, 3, 1), fingers=(0, 1, 2, 3, 4, 1), base_fret=1),
    'A#7': ChordShape(frets=(-1, 1, 3, 1, 3, 1), fingers=(0, 1, 3, 1, 4, 1), base_fret=1),
    'A#m': ChordShape(frets=(-1, 1, 3, 3, 2, 1), fingers=(0, 1, 3, 4, 2, 1), base_fret=1),
    'A#m7': ChordShape(frets=(-1, 1, 3, 1, 2, 1), fingers=(0, 1, 3, 1, 2, 1), base_fret=1),
    'A#maj7': ChordShape(frets=(-1, 1, 3, 2, 3, 1), fingers=(0, 1, 3, 2, 4, 1), base_fret=1),
    'A7': ChordShape(frets=(-1, 0, 2, 0, 2, 0), fingers=(0, 0, 2, 0, 3, 0), base_fret=1),
    'Ab': ChordShape(frets=(4, 3, 1, 1, 1, -1), fingers=(3, 2, 1, 1, 1, 0), base_fret=1),
    'Ab7': ChordShape(frets=(-1, -1, 1, 1, 1, 2), fingers=(0, 0, 1, 1, 1, 2), base_fret=1),
    'Abm': ChordShape(frets=(1, 3, 3, 1, 1, 1), fingers=(1, 3, 4, 1, 1, 1), base_fret=4),
    'Abm7': ChordShape(frets=(4, -1, 4, 4, 4, -1), fingers=(2, 0, 3, 3, 3, 0), base_fret=1),
    'Abmaj7': ChordShape(frets=(1, 3, 2, 2, 1, 1), fingers=(1, 4, 2, 3, 1, 1), base_fret=4),
    'Am': ChordShape(frets=(-1, 0, 2, 2, 1, 0), fingers=(0, 0, 2, 3, 1, 0), base_fret=1),
    'Am7': ChordShape(frets=(-1, 0, 2, 0, 1, 0), fingers=(0, 0, 2, 0, 1, 0), base_fret=1),
    'Amaj7': ChordShape(frets=(-1, 0, 2, 1, 2, 0), fingers=(0, 0, 2, 1, 3, 0), base_fret=1),
    'B': ChordShape(frets=(2, 2, 4, 4, 4, 2), fingers=(1, 1, 2, 3, 4, 1), base_fret=1),
    'B7': ChordShape(frets=(-1, 2, 1, 2, 0, 2), fingers=(0, 2, 1, 3, 0, 4), base_fret=1),
    'Bb': ChordShape(frets=(-1, 1, 3, 3, 3, 1), fingers=(0, 1, 2, 3, 4, 1), base_fret=1),
    'Bb7': ChordShape(frets=(-1, 1, 3, 1, 3, 1), fingers=(0, 1, 3, 1, 4, 1), base_fret=1),
    'Bbm': ChordShape(frets=(-1, 1, 3, 3, 2, 1), fingers=(0, 1, 3, 4, 2, 1), base_fret=1),
    'Bbm7': ChordShape(frets=(-1, 1, 3, 1, 2, 1), fingers=(0, 1, 3, 1, 2, 1), base_fret=1),
    'Bbmaj7': ChordShape(frets=(-1, 1, 3, 2, 3, 1), fingers=(0, 1, 3, 2, 4, 1), base_fret=1),
    'Bm': ChordShape(frets=(2, 2, 4, 4, 3, 2), fingers=(1, 1, 3, 4, 2, 1), base_fret=1),
    'Bm7': ChordShape(frets=(2, 2, 4, 2, 3, 2), fingers=(1, 1, 3, 1, 2, 1), base_fret=1),
    'Bmaj7': ChordShape(frets=(2, 2, 4, 3, 4, 2), fingers=(1, 1, 3, 2, 4, 1), base_fret=1),
    'C': ChordShape(frets=(-1, 3, 2, 0, 1, 0), fingers=(0, 3, 2, 0, 1, 0), base_fret=1),
    'C#': ChordShape(frets=(-1, 4, 3, 1, 2, 1), fingers=(0, 4, 3, 1, 2, 1), base_fret=1),
    'C#7': ChordShape(frets=(-1, 4, 3, 4, 2, -1), fingers=(0, 3, 2, 4, 1, 0), base_fret=1),
    'C#m': ChordShape(frets=(-1, 4, 2, 1, 2, -1), fingers=(0, 4, 2, 1, 3, 0), base_fret=1),
    'C#m7': ChordShape(frets=(-1, 1, 3, 1, 2, 1), fingers=(0, 1, 3, 1, 2, 1), base_fret=4),
    'C#maj7': ChordShape(frets=(-1, 4, 3, 1, 1, 1), fingers=(0, 4, 3, 1, 1, 1), base_fret=1),
    'C7': ChordShape(frets=(-1, 3, 2, 3, 1, 0), fingers=(0, 3, 2, 4, 1, 0), base_fret=1),
    'Cm': ChordShape(frets=(-1, 3, 1, 0, 1, 3), fingers=(0, 3, 2, 0, 1, 4), base_fret=1),
    'Cm7': ChordShape(frets=(-1, 3, 1, 3, 4, -1), fingers=(0, 2, 1, 3, 4, 0), base_fret=1),
    'Cmaj7': ChordShape(frets=(3, 3, 2, 0, 0, 0), fingers=(2, 3, 1, 0, 0, 0), base_fret=1),
    'D': ChordShape(frets=(-1, -1, 0, 2, 3, 2), fingers=(0, 0, 0, 1, 3, 2), base_fret=1),
    'D#': ChordShape(frets=(-1, -1, 1, 3, 4, 3), fingers=(0, 0, 1, 2, 4, 3), base_fret=1),
    'D#7': ChordShape(frets=(-1, -1, 1, 3, 2, 3), fingers=(0, 0, 1, 3, 2, 4), base_fret=1),
    'D#m': ChordShape(frets=(-1, -1, 1, 3, 4, 2), fingers=(0, 0, 1, 3, 4, 2), base_fret=1),
    'D#m7': ChordShape(frets=(-1, -1, 1, 3, 2, 2), fingers=(0, 0, 1, 4, 2, 3), base_fret=1),
    'D#maj7': ChordShape(frets=(-1, 1, 1, 3, 3, 3), fingers=(0, 1, 1, 3, 3, 3), base_fret=1),
    'D7': ChordShape(frets=(-1, -1, 0, 2, 1, 2), fingers=(0, 0, 0, 2, 1, 3), base_fret=1),
    'Db': ChordShape(frets=(-1, 4, 3, 1, 2, 1), fingers=(0, 4, 3, 1, 2, 1), base_fret=1),
    'Db7': ChordShape(frets=(-1, 4, 3, 4, 2, -1), fingers=(0, 3, 2, 4, 1, 0), base_fret=1),
    'Dbm': ChordShape(frets=(-1, 4, 2, 1, 2, -1), fingers=(0, 4, 2, 1, 3, 0), base_fret=1),
    'Dbm7': ChordShape(frets=(-1, 1, 3, 1, 2, 1), fingers=(0, 1, 3, 1, 2, 1), base_fret=4),
    'Dbmaj7': ChordShape(frets=(-1, 4, 3, 1, 1, 1), fingers=(0, 4, 3, 1, 1, 1), base_fret=1),
    'Dm': ChordShape(frets=(-1, -1, 0, 2, 3, 1), fingers=(0, 0, 0, 2, 3, 1), base_fret=1),
    'Dm7': ChordShape(frets=(-1, -1, 0, 2, 1, 1), fingers=(0, 0, 0, 2, 1, 1), base_fret=1),
    'Dmaj7': ChordShape(frets=(-1, -1, 0, 2, 2, 2), fingers=(0, 0, 0, 1, 1, 1), base_fret=1),
    'E': ChordShape(frets=(0, 2, 2, 1, 0, 0), fingers=(0, 2, 3, 1, 0, 0), base_fret=1),
    'E7': ChordShape(frets=(0, 2, 0, 1, 0, 0), fingers=(0, 2, 0, 1, 0, 0), base_fret=1),
    'Eb': ChordShape(frets=(-1, -1, 1, 3, 4, 3), fingers=(0, 0, 1, 2, 4, 3), base_fret=1),
    'Eb7': ChordShape(frets=(-1, -1, 1, 3, 2, 3), fingers=(0, 0, 1, 3, 2, 4), base_fret=1),
    'Ebm': ChordShape(frets=(-1, -1, 1, 3, 4, 2), fingers=(0, 0, 1, 3, 4, 2), base_fret=1),
    'Ebm7': ChordShape(frets=(-1, -1, 1, 3, 2, 2), fingers=(0, 0, 1, 4, 2, 3), base_fret=1),
    'Ebmaj7': ChordShape(frets=(-1, 1, 1, 3, 3, 3), fingers=(0, 1, 1, 3, 3, 3), base_fret=1),
    'Em': ChordShape(frets=(0, 2, 2, 0, 0, 0), fingers=(0, 2, 3, 0, 0, 0), base_fret=1),
    'Em7': ChordShape(frets=(0, -1, 0, 0, 0, -1), fingers=(2, 0, 3, 3, 3, 0), base_fret=1),
    'Emaj7': ChordShape(frets=(0, 2, 1, 1, 0, 0), fingers=(0, 3, 1, 2, 0, 0), base_fret=1),
    'F': ChordShape(frets=(1, 3, 3, 2, 1, 1), fingers=(1, 3, 4, 2, 1, 1), base_fret=1),
    'F#': ChordShape(frets=(2, 4, 4, 3, 2, 2), fingers=(1, 3, 4, 2, 1, 1), base_fret=1),
    'F#7': ChordShape(frets=(2, 4, 2, 3, 2, 2), fingers=(1, 3, 1, 2, 1, 1), base_fret=1),
    'F#m': ChordShape(frets=(2, 4, 4, 2, 2, 2), fingers=(1, 3, 4, 1, 1, 1), base_fret=1),
    'F#m7': ChordShape(frets=(2, -1, 2, 2, 2, -1), fingers=(2, 0, 3, 3, 3, 0), base_fret=1),
    'F#maj7': ChordShape(frets=(2, 4, 3, 3, 2, 2), fingers=(1, 4, 2, 3, 1, 1), base_fret=1),
    'F7': ChordShape(frets=(1, 3, 1, 2, 1, 1), fingers=(1, 3, 1, 2, 1, 1), base_fret=1),
    'Fm': ChordShape(frets=(1, 3, 3, 1, 1, 1), fingers=(1, 3, 4, 1, 1, 1), base_fret=1),
    'Fm7': ChordShape(frets=(1, -1, 1, 1, 1, -1), fingers=(2, 0, 3, 3, 3, 0), base_fret=1),
    'Fmaj7': ChordShape(frets=(-1, -1, 3, 2, 1, 0), fingers=(0, 0, 3, 2, 1, 0), base_fret=1),
    'G': ChordShape(frets=(3, 2, 0, 0, 0, 3), fingers=(2, 1, 0, 0, 0, 3), base_fret=1),
    'G#': ChordShape(frets=(4, 3, 1, 1, 1, -1), fingers=(3, 2, 1, 1, 1, 0), base_fret=1),
    'G#7': ChordShape(frets=(-1, -1, 1, 1, 1, 2), fingers=(0, 0, 1, 1, 1, 2), base_fret=1),
    'G#m': ChordShape(frets=(1, 3, 3, 1, 1, 1), fingers=(1, 3, 4, 1, 1, 1), base_fret=4),
    'G#m7': ChordShape(frets=(4, -1, 4, 4, 4, -1), fingers=(2, 0, 3, 3, 3, 0), base_fret=1),
    'G#maj7': ChordShape(frets=(1, 3, 2, 2, 1, 1), fingers=(1, 4, 2, 3, 1, 1), base_fret=4),
    'G7': ChordShape(frets=(3, 2, 0, 0, 0, 1), fingers=(3, 2, 0, 0, 0, 1), base_fret=1),
    'Gb': ChordShape(frets=(2, 4, 4, 3, 2, 2), fingers=(1, 3, 4, 2, 1, 1), base_fret=1),
    'Gb7': ChordShape(frets=(2, 4, 2, 3, 2, 2), fingers=(1, 3, 1, 2, 1, 1), base_fret=1),
    'Gbm': ChordShape(frets=(2, 4, 4, 2, 2, 2), fingers=(1, 3, 4, 1, 1, 1), base_fret=1),
    'Gbm7': ChordShape(frets=(2, -1, 2, 2, 2, -1), fingers=(2, 0, 3, 3, 3, 0), base_fret=1),
    'Gbmaj7': ChordShape(frets=(2, 4, 3, 3, 2, 2), fingers=(1, 4, 2, 3, 1, 1), base_fret=1),
    'Gm': ChordShape(frets=(3, 1, 0, 0, 3, 3), fingers=(2, 1, 0, 0, 3, 4), base_fret=1),
    'Gm7': ChordShape(frets=(3, -1, 3, 3, 3, -1), fingers=(2, 0, 3, 3, 3, 0), base_fret=1),
    'Gmaj7': ChordShape(frets=(3, 2, 0, 0, 0, 2), fingers=(3, 2, 0, 0, 0, 1), base_fret=1),
}


def get_chord_shape(label: str) -> ChordShape | None:
    """Never raises -- an unrecognized label (e.g. 'N', the no-chord sentinel,
    or anything not in the 12-roots-x-5-qualities set detect_chords() can
    produce) simply has nothing to show, not an error."""
    return CHORD_SHAPES.get(label)
