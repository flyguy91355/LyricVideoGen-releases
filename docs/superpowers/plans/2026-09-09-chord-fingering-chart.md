# Chord Fingering Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a small guitar fingering diagram for every unique chord in the
song, upper-left corner, in order of first appearance, with the currently-playing
chord highlighted.

**Architecture:** `chord_shapes.py` holds a verified, complete lookup table of
guitar fingerings (extracted from a real MIT-licensed database, not invented) for
every label `detect_chords()` can produce. `chord_diagram.py` draws one small
diagram per chord and lays them out as a legend. `pipeline.py` computes the
song's ordered unique-chord list once; `assemble_video()` draws the legend every
frame using that list plus the live current chord. `Settings` gains one toggle.

**Tech Stack:** Python 3.12, PIL (already a dependency), pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-chord-fingering-chart-design.md`

## Global Constraints

- `chord_shapes.py`/`chord_diagram.py` take plain arguments only (no `Settings`
  import) — same decoupling convention as `render.py`/`detect_chords.py`.
- Every new `assemble_video()` parameter defaults to a value that reproduces
  today's behavior when `Settings` isn't in play (`show_chord_legend=True` is the
  one exception already decided in the spec — it's a new, additive visual, on by
  default, exactly like `show_chord_timeline`/`show_key_bpm` were when they
  shipped).
- Every `Path.read_text()`/`.write_text()` call passes `encoding="utf-8"`
  explicitly (existing codebase convention).
- Data source: `tombatossals/chords-db` (MIT licensed, verified). Only the 5
  qualities this app detects (`major`, `minor`, `7`, `m7`, `maj7`) across 12
  roots are extracted — no other chord suffix is bundled.
- Run `cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/ -v`
  after every task; all tests must pass before committing. The pre-commit hook
  blocks any commit touching a `.py`/`.sh` file unless `CLAUDE.md` is also staged
  with a real, accurate change.

---

## Task 1: chord_shapes.py — verified fingering data

**Files:**
- Create: `lyricvideo/chord_shapes.py`
- Test: `tests/test_chord_shapes.py`

**Interfaces:**
- Produces: `ChordShape` frozen dataclass (`frets: tuple[int, ...]` len 6,
  low-E-to-high-e; `-1` = muted, `0` = open, `>0` = fretted at that position
  counting from `base_fret`; `fingers: tuple[int, ...]` len 6, `0` = no finger,
  `1`-`4` = finger number; `base_fret: int = 1` — `1` means the shape starts at
  the nut and `frets` values are absolute fret numbers; `>1` means the diagram
  has no nut line and should show a `"<base_fret>fr"` label instead), and
  `get_chord_shape(label: str) -> ChordShape | None` (`None` for `"N"` and
  anything else unrecognized — must never raise).

Every one of the 85 entries below was extracted from `chords-db`'s
`lib/guitar.json` (`positions[i]` with the lowest `baseFret` for that chord),
verified to have a maximum relative fret span of 4 (matching the dataset's own
`fretsOnChord: 4`), and spot-checked against standard open-chord shapes (E, A,
D, G, C, Am, Em all match textbook fingerings exactly).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chord_shapes.py`:

```python
from lyricvideo.chord_shapes import CHORD_SHAPES, ChordShape, get_chord_shape
from lyricvideo.chord_theory import NOTES_FLAT, NOTES_SHARP, QUALITY_SUFFIX


def test_every_label_detect_chords_can_produce_has_a_shape():
    """detect_chords() can only ever emit one of 12 roots (sharp or flat
    spelling) x 5 qualities -- this must never come back None for any of them."""
    for root_names in (NOTES_SHARP, NOTES_FLAT):
        for root in root_names:
            for suffix in QUALITY_SUFFIX.values():
                label = root + suffix
                shape = get_chord_shape(label)
                assert shape is not None, f"missing shape for {label!r}"
                assert len(shape.frets) == 6
                assert len(shape.fingers) == 6


def test_get_chord_shape_returns_none_for_no_chord():
    assert get_chord_shape("N") is None


def test_get_chord_shape_returns_none_for_unrecognized_label():
    assert get_chord_shape("Xmaj13#11") is None


def test_open_e_major_matches_textbook_shape():
    shape = get_chord_shape("E")
    assert shape.frets == (0, 2, 2, 1, 0, 0)
    assert shape.base_fret == 1


def test_open_a_minor_matches_textbook_shape():
    shape = get_chord_shape("Am")
    assert shape.frets == (-1, 0, 2, 2, 1, 0)


def test_sharp_and_flat_spellings_of_the_same_pitch_share_a_shape():
    assert get_chord_shape("C#m") == get_chord_shape("Dbm")
    assert get_chord_shape("G#maj7") == get_chord_shape("Abmaj7")


def test_a_chord_with_no_low_position_shape_gets_a_base_fret_above_one():
    """C#m7/Dbm7 has no standard low-fret voicing in the source database --
    real songbooks show these as a barre shape higher up the neck instead."""
    shape = get_chord_shape("C#m7")
    assert shape.base_fret == 4
    assert shape == get_chord_shape("Dbm7")


def test_chord_shapes_dict_has_exactly_85_entries():
    """17 distinct root spellings (7 naturals need one spelling, 5 accidentals
    need both sharp and flat) x 5 qualities."""
    assert len(CHORD_SHAPES) == 85
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_chord_shapes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.chord_shapes'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/chord_shapes.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_chord_shapes.py -v`
Expected: 127 passed (120 from the parametrized-by-loop first test's individual
assertions collapse into 1 test function, so actually 7 passed — count is exactly
the 7 test functions defined above; the loop inside the first test just makes
that one test thorough, it doesn't multiply the collected test count).

- [ ] **Step 5: Add a minimal CLAUDE.md note and commit**

```
`lyricvideo/chord_shapes.py` (new) holds guitar fingering data for every chord
`detect_chords()` can produce, extracted from tombatossals/chords-db (MIT) --
not yet drawn anywhere (in progress; see
docs/superpowers/plans/2026-09-09-chord-fingering-chart.md).
```

```bash
git add lyricvideo/chord_shapes.py tests/test_chord_shapes.py CLAUDE.md
git commit -m "Add verified guitar chord fingering data (chord_shapes.py)"
```

---

## Task 2: chord_diagram.py — draw one diagram and the full legend

**Files:**
- Create: `lyricvideo/chord_diagram.py`
- Test: `tests/test_chord_diagram.py`

**Interfaces:**
- Consumes: `lyricvideo.chord_shapes.ChordShape`/`get_chord_shape` (Task 1).
- Produces: `draw_single_chord_diagram(shape, label, box_size, font_path, *,
  highlighted=False, accent_color=(56,189,248), text_color=(255,255,255),
  dim_text_color=(148,163,184), panel_color=(11,18,32), panel_alpha=150) ->
  Image.Image` (RGBA, size `box_size`) and `draw_chord_legend(frame,
  chord_labels: list[str], current_label: str | None, font_path, *, frame_size,
  show_chord_legend=True, accent_color=..., text_color=..., dim_text_color=...,
  panel_color=..., panel_alpha=...) -> Image.Image` (RGB, size `frame.size`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chord_diagram.py`:

```python
import numpy as np
from PIL import Image

from lyricvideo.chord_diagram import draw_chord_legend, draw_single_chord_diagram
from lyricvideo.chord_shapes import get_chord_shape


def test_draw_single_chord_diagram_returns_requested_size(test_font_path):
    shape = get_chord_shape("C")
    img = draw_single_chord_diagram(shape, "C", (120, 160), test_font_path)
    assert img.size == (120, 160)
    assert img.mode == "RGBA"


def test_draw_single_chord_diagram_is_not_blank(test_font_path):
    shape = get_chord_shape("Em")
    img = draw_single_chord_diagram(shape, "Em", (120, 160), test_font_path)
    assert img.getextrema()[3] != (0, 0)  # alpha channel has real content


def test_draw_single_chord_diagram_highlighted_differs_from_unhighlighted(test_font_path):
    shape = get_chord_shape("G")
    plain = np.array(draw_single_chord_diagram(shape, "G", (120, 160), test_font_path, highlighted=False))
    lit = np.array(draw_single_chord_diagram(shape, "G", (120, 160), test_font_path, highlighted=True))
    assert not np.array_equal(plain, lit)


def test_draw_single_chord_diagram_shows_base_fret_label_when_not_at_the_nut(test_font_path):
    shape = get_chord_shape("C#m7")  # base_fret=4, verified in Task 1
    assert shape.base_fret == 4
    img = draw_single_chord_diagram(shape, "C#m7", (120, 160), test_font_path)
    assert img.size == (120, 160)  # renders without error


def test_draw_chord_legend_renders_without_error(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080))
    assert frame.size == (1920, 1080)


def test_draw_chord_legend_changes_pixels_versus_plain_background(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = np.array(draw_chord_legend(bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080)))
    plain = np.array(bg)
    assert not np.array_equal(frame, plain)


def test_draw_chord_legend_does_not_mutate_input_frame(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    draw_chord_legend(bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080))
    assert bg.getextrema() == ((20, 20), (20, 20), (20, 20))


def test_draw_chord_legend_hidden_when_disabled(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(
        bg, ["G", "D", "Am"], "D", test_font_path, frame_size=(1920, 1080), show_chord_legend=False,
    )
    assert np.array_equal(np.array(frame), np.array(bg))


def test_draw_chord_legend_empty_chord_list_changes_nothing(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(bg, [], None, test_font_path, frame_size=(1920, 1080))
    assert np.array_equal(np.array(frame), np.array(bg))


def test_draw_chord_legend_skips_unrecognized_labels_without_raising(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    frame = draw_chord_legend(
        bg, ["G", "NotARealChord123", "Am"], "G", test_font_path, frame_size=(1920, 1080),
    )
    assert frame.size == (1920, 1080)


def test_draw_chord_legend_current_chord_highlight_changes_which_diagram_is_lit(test_font_path):
    bg = Image.new("RGB", (1920, 1080), (20, 20, 20))
    current_g = np.array(draw_chord_legend(bg, ["G", "D"], "G", test_font_path, frame_size=(1920, 1080)))
    current_d = np.array(draw_chord_legend(bg, ["G", "D"], "D", test_font_path, frame_size=(1920, 1080)))
    assert not np.array_equal(current_g, current_d)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_chord_diagram.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.chord_diagram'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/chord_diagram.py`:

```python
"""Draws guitar chord fingering diagrams (the box-with-dots chart from a
songbook) and lays them out as an upper-left legend of every chord in the song.
Takes plain arguments only, no Settings import -- same decoupling convention as
render.py/detect_chords.py."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .chord_shapes import ChordShape, get_chord_shape

ACCENT_COLOR_DEFAULT = (56, 189, 248)
TEXT_COLOR_DEFAULT = (255, 255, 255)
DIM_TEXT_COLOR_DEFAULT = (148, 163, 184)
PANEL_COLOR_DEFAULT = (11, 18, 32)
PANEL_ALPHA_DEFAULT = 150

_LABEL_HEIGHT_FRAC = 0.20       # fraction of diagram height reserved for the chord name
_MUTE_OPEN_HEIGHT_FRAC = 0.12   # fraction reserved for the X/O row above the nut
_GRID_SIDE_PAD_FRAC = 0.12      # fraction of diagram width padded on each side of the string grid

_LEGEND_MARGIN_FRAC = 0.03
_LEGEND_GAP_FRAC = 0.012
_LEGEND_MAX_ROW_WIDTH_FRAC = 0.5
_DIAGRAM_WIDTH_FRAC = 0.075
_DIAGRAM_HEIGHT_FRAC = 0.16


def draw_single_chord_diagram(
    shape: ChordShape,
    label: str,
    box_size: tuple[int, int],
    font_path: str,
    *,
    highlighted: bool = False,
    accent_color: tuple[int, int, int] = ACCENT_COLOR_DEFAULT,
    text_color: tuple[int, int, int] = TEXT_COLOR_DEFAULT,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR_DEFAULT,
    panel_color: tuple[int, int, int] = PANEL_COLOR_DEFAULT,
    panel_alpha: int = PANEL_ALPHA_DEFAULT,
) -> Image.Image:
    """One small fingering diagram: chord name above, 6 vertical string lines,
    a nut line (or a base-fret label, if the shape doesn't start at the nut) +
    4 fret lines below it, filled dots with finger numbers for fretted strings,
    'X'/'O' above the nut for muted/open strings. Transparent background outside
    the rounded panel so it composites cleanly onto a video frame."""
    bw, bh = box_size
    img = Image.new("RGBA", box_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    panel_fill = (*panel_color, panel_alpha)
    draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=6, fill=panel_fill)
    if highlighted:
        draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=6, outline=(*accent_color, 255), width=3)

    label_h = int(bh * _LABEL_HEIGHT_FRAC)
    xo_h = int(bh * _MUTE_OPEN_HEIGHT_FRAC)
    grid_y0 = label_h + xo_h
    grid_h = bh - grid_y0 - 4
    side_pad = int(bw * _GRID_SIDE_PAD_FRAC)
    grid_x0 = side_pad
    grid_w = bw - 2 * side_pad

    label_font = ImageFont.truetype(font_path, max(10, label_h - 4))
    small_font = ImageFont.truetype(font_path, max(8, xo_h - 2))
    finger_font = ImageFont.truetype(font_path, max(8, int(grid_h / 4 * 0.5)))

    label_color = accent_color if highlighted else text_color
    label_w = draw.textlength(label, font=label_font)
    draw.text((bw / 2 - label_w / 2, 2), label, font=label_font, fill=(*label_color, 255))

    string_xs = [grid_x0 + i * (grid_w / 5) for i in range(6)]
    fret_ys = [grid_y0 + r * (grid_h / 4) for r in range(5)]

    for r, fy in enumerate(fret_ys):
        line_width = 3 if (r == 0 and shape.base_fret == 1) else 1
        draw.line([(string_xs[0], fy), (string_xs[-1], fy)], fill=(*dim_text_color, 255), width=line_width)
    for sx in string_xs:
        draw.line([(sx, fret_ys[0]), (sx, fret_ys[-1])], fill=(*dim_text_color, 255), width=1)

    if shape.base_fret != 1:
        tag = f"{shape.base_fret}fr"
        draw.text((string_xs[-1] + 4, fret_ys[0] - 4), tag, font=small_font, fill=(*dim_text_color, 255))

    dot_radius = min(grid_w / 5, grid_h / 4) * 0.32
    dot_color = accent_color if highlighted else dim_text_color
    for i in range(6):
        f = shape.frets[i]
        sx = string_xs[i]
        if f == -1:
            draw.text((sx - small_font.size / 4, xo_h * 0.1), "X", font=small_font, fill=(*text_color, 255))
        elif f == 0:
            draw.text((sx - small_font.size / 4, xo_h * 0.1), "O", font=small_font, fill=(*text_color, 255))
        elif f > 0:
            row_center_y = (fret_ys[f - 1] + fret_ys[f]) / 2
            draw.ellipse(
                (sx - dot_radius, row_center_y - dot_radius, sx + dot_radius, row_center_y + dot_radius),
                fill=(*dot_color, 255),
            )
            finger = shape.fingers[i]
            if finger:
                ftext = str(finger)
                fw = draw.textlength(ftext, font=finger_font)
                draw.text(
                    (sx - fw / 2, row_center_y - finger_font.size / 2),
                    ftext, font=finger_font, fill=(*panel_color, 255),
                )

    return img


def draw_chord_legend(
    frame: Image.Image,
    chord_labels: list[str],
    current_label: str | None,
    font_path: str,
    *,
    frame_size: tuple[int, int],
    show_chord_legend: bool = True,
    accent_color: tuple[int, int, int] = ACCENT_COLOR_DEFAULT,
    text_color: tuple[int, int, int] = TEXT_COLOR_DEFAULT,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR_DEFAULT,
    panel_color: tuple[int, int, int] = PANEL_COLOR_DEFAULT,
    panel_alpha: int = PANEL_ALPHA_DEFAULT,
) -> Image.Image:
    """Composites one draw_single_chord_diagram() per chord_labels entry into
    the upper-left corner of `frame`, left to right, wrapping to further rows
    past _LEGEND_MAX_ROW_WIDTH_FRAC of the frame width. A label
    get_chord_shape() can't resolve is skipped, not an error. Returns a new
    image; `frame` is not mutated (matches draw_scene/draw_chord_bar's own
    copy-on-write style)."""
    if not show_chord_legend or not chord_labels:
        return frame

    w, h = frame_size
    box_w = int(w * _DIAGRAM_WIDTH_FRAC)
    box_h = int(h * _DIAGRAM_HEIGHT_FRAC)
    gap = int(w * _LEGEND_GAP_FRAC)
    margin_x = int(w * _LEGEND_MARGIN_FRAC)
    margin_y = int(h * _LEGEND_MARGIN_FRAC)
    max_row_width = int(w * _LEGEND_MAX_ROW_WIDTH_FRAC)

    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    row_start_x = margin_x
    x, y = margin_x, margin_y
    drew_any = False

    for label in chord_labels:
        shape = get_chord_shape(label)
        if shape is None:
            continue
        if x != row_start_x and x + box_w > row_start_x + max_row_width:
            x = row_start_x
            y += box_h + gap
        diagram = draw_single_chord_diagram(
            shape, label, (box_w, box_h), font_path,
            highlighted=(label == current_label),
            accent_color=accent_color, text_color=text_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=panel_alpha,
        )
        overlay.alpha_composite(diagram, (x, y))
        x += box_w + gap
        drew_any = True

    if not drew_any:
        return frame

    composited = Image.alpha_composite(frame.convert("RGBA"), overlay)
    return composited.convert("RGB")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_chord_diagram.py -v`
Expected: 10 passed

- [ ] **Step 5: Manually verify a rendered diagram visually**

```bash
cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -c "
from PIL import Image
from lyricvideo.chord_diagram import draw_chord_legend

bg = Image.new('RGB', (1920, 1080), (20, 30, 45))
frame = draw_chord_legend(bg, ['G', 'D', 'Am', 'C#m7'], 'D', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', frame_size=(1920, 1080))
frame.save('/tmp/chord_legend_check.png')
print('wrote /tmp/chord_legend_check.png')
"
```

Then view `/tmp/chord_legend_check.png` (Read tool supports images) and confirm:
four diagrams in the upper-left, chord names readable, dots roughly where
strings/frets cross, D's diagram visually highlighted (accent border + colored
name) since it's the current chord, and C#m7's diagram shows a "4fr" label
instead of a thick nut line.

- [ ] **Step 6: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/chord_diagram.py tests/test_chord_diagram.py CLAUDE.md
git commit -m "Add chord diagram drawing and the upper-left chord legend"
```

(Append to the same in-progress note from Task 1 first — "`chord_diagram.py` can
now draw a full legend; not yet wired into assemble_video()/pipeline.py.")

---

## Task 3: pipeline.py — ordered_unique_chords()

**Files:**
- Modify: `lyricvideo/pipeline.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `lyricvideo.models.ChordTrack` (existing).
- Produces: `ordered_unique_chords(chord_track: ChordTrack) -> list[str]`
  (first-seen order across every event, sung or instrumental; `"N"` excluded;
  same first-seen-order convention as the existing `_instrumental_chord_labels`).

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_pipeline.py` (after the existing
`_instrumental_chord_labels` tests; add `ordered_unique_chords` to the existing
`from lyricvideo.pipeline import (...)` block at the top):

```python
def test_ordered_unique_chords_preserves_first_seen_order():
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "G"), ChordEvent(2.0, 4.0, "D"),
        ChordEvent(4.0, 6.0, "Am"), ChordEvent(6.0, 8.0, "G"),
    ])

    assert ordered_unique_chords(chord_track) == ["G", "D", "Am"]


def test_ordered_unique_chords_excludes_no_chord_label():
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "N"), ChordEvent(2.0, 4.0, "C"), ChordEvent(4.0, 6.0, "N"),
    ])

    assert ordered_unique_chords(chord_track) == ["C"]


def test_ordered_unique_chords_empty_track_returns_empty_list():
    assert ordered_unique_chords(ChordTrack()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v -k ordered_unique`
Expected: FAIL with `ImportError: cannot import name 'ordered_unique_chords'`

- [ ] **Step 3: Write the implementation**

In `lyricvideo/pipeline.py`, add the function right after `_instrumental_chord_labels`.
Find:

```python
def _default_font() -> str:
```

Wait — that function was renamed to `default_font()` in the prior Settings/GUI
plan. Find instead:

```python
def default_font() -> str:
```

Insert immediately **before** it:

```python
def ordered_unique_chords(chord_track: ChordTrack) -> list[str]:
    """Every distinct chord label in the song, in first-seen order across ALL
    events (sung or instrumental) -- backs the chord fingering legend, which
    shows one diagram per chord regardless of whether it happens during vocals.
    'N' (no-chord) is excluded -- there's nothing to finger."""
    labels: list[str] = []
    for event in chord_track.events:
        if event.label != "N" and event.label not in labels:
            labels.append(event.label)
    return labels
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v -k ordered_unique`
Expected: 3 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/pipeline.py tests/test_pipeline.py CLAUDE.md
git commit -m "Add ordered_unique_chords() for the chord fingering legend"
```

---

## Task 4: assemble.py — draw the chord legend every frame

**Files:**
- Modify: `lyricvideo/assemble.py`
- Modify: `tests/test_assemble.py`

**Interfaces:**
- Consumes: `lyricvideo.chord_diagram.draw_chord_legend(...)` (Task 2),
  `lyricvideo.models.current_chord_at` (existing, already imported by `render.py`
  but not yet by `assemble.py`).
- Produces: `assemble_video(..., *, chord_legend_labels: list[str] | None = None,
  show_chord_legend: bool = True, ...)` — new parameters appended to the
  existing keyword-only section from the prior Settings/GUI plan.

`chord_legend_labels` defaults to `None` (treated as `[]`, i.e. "nothing to
show") rather than requiring every existing caller to pass one — the CLI/tests
that don't care about the legend keep working unchanged, and `run_pipeline()`
(Task 5) is the only real caller that computes and passes a real list.

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_assemble.py` (the file already has `_fake_clips`
defined near the top — reuse it):

```python
def test_assemble_video_draws_chord_legend_with_computed_current_label(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        draw_calls.append((chord_labels, current_label))
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 1.0, "G"), ChordEvent(1.0, 2.0, "D")])
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        chord_legend_labels=["G", "D"],
    )
    calls["make_frame"](1.5)

    assert draw_calls == [(["G", "D"], "D")]


def test_assemble_video_chord_legend_defaults_to_no_labels(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        captured["chord_labels"] = chord_labels
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](0.5)

    assert captured["chord_labels"] == []


def test_assemble_video_respects_show_chord_legend_toggle(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        captured["show_chord_legend"] = kwargs.get("show_chord_legend")
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        show_chord_legend=False,
    )
    calls["make_frame"](0.5)

    assert captured["show_chord_legend"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_assemble.py -v -k chord_legend`
Expected: FAIL — `TypeError: assemble_video() got an unexpected keyword argument
'chord_legend_labels'`

- [ ] **Step 3: Update the implementation**

In `lyricvideo/assemble.py`, update the imports. Find:

```python
from .render import (
    ACCENT_COLOR, DIM_TEXT_COLOR, FRAME_SIZE, apply_ken_burns, draw_chord_bar, draw_scene,
    ken_burns_preset_for_key,
)
```

Replace with:

```python
from .chord_diagram import draw_chord_legend
from .models import current_chord_at
from .render import (
    ACCENT_COLOR, DIM_TEXT_COLOR, FRAME_SIZE, apply_ken_burns, draw_chord_bar, draw_scene,
    ken_burns_preset_for_key,
)
```

`ChordTrack` is already imported from `.models` on the line above this block —
add `current_chord_at` to that existing import instead of a new line. Find:

```python
from .models import ChordTrack, LyricLine
```

Replace with:

```python
from .models import ChordTrack, LyricLine, current_chord_at
```

(Remove the separate `from .models import current_chord_at` line just added
above if you added both — there should be exactly one `.models` import line.)

Update the function signature. Find:

```python
    show_chord_timeline: bool = True,
    show_key_bpm: bool = True,
    timeline_window_sec: float = 12.0,
) -> None:
```

Replace with:

```python
    show_chord_timeline: bool = True,
    show_key_bpm: bool = True,
    timeline_window_sec: float = 12.0,
    chord_legend_labels: list[str] | None = None,
    show_chord_legend: bool = True,
) -> None:
```

Wire it into `make_frame`. Find:

```python
        frame = draw_chord_bar(
            frame, chord_track, t, font_path,
            frame_size=frame_size, accent_color=accent_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=panel_alpha, chord_now_size=chord_now_size,
            chord_next_size=chord_next_size, show_chord_timeline=show_chord_timeline,
            show_key_bpm=show_key_bpm, timeline_window_sec=timeline_window_sec,
        )
        return np.array(frame)
```

Replace with:

```python
        frame = draw_chord_bar(
            frame, chord_track, t, font_path,
            frame_size=frame_size, accent_color=accent_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=panel_alpha, chord_now_size=chord_now_size,
            chord_next_size=chord_next_size, show_chord_timeline=show_chord_timeline,
            show_key_bpm=show_key_bpm, timeline_window_sec=timeline_window_sec,
        )
        current = current_chord_at(chord_track, t)
        frame = draw_chord_legend(
            frame, chord_legend_labels or [], current.label if current is not None else None, font_path,
            frame_size=frame_size, show_chord_legend=show_chord_legend, accent_color=accent_color,
            text_color=text_color, dim_text_color=dim_text_color, panel_color=panel_color,
            panel_alpha=panel_alpha,
        )
        return np.array(frame)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_assemble.py -v`
Expected: 8 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/assemble.py tests/test_assemble.py CLAUDE.md
git commit -m "assemble_video: draw the chord fingering legend every frame"
```

---

## Task 5: pipeline.py — thread ordered_unique_chords into run_pipeline

**Files:**
- Modify: `lyricvideo/pipeline.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `ordered_unique_chords()` (Task 3), `assemble_video(...,
  chord_legend_labels=..., show_chord_legend=...)` (Task 4).

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_pipeline.py`:

```python
def test_run_pipeline_passes_ordered_unique_chords_to_assemble_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_assemble_video(*args, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", spying_assemble_video)
    monkeypatch.setattr(
        "lyricvideo.pipeline.detect_chords",
        lambda path, **kwargs: ChordTrack(events=[
            ChordEvent(0.0, 1.0, "G"), ChordEvent(1.0, 2.0, "D"), ChordEvent(2.0, 3.0, "G"),
        ]),
    )

    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)

    assert captured["kwargs"]["chord_legend_labels"] == ["G", "D"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v -k passes_ordered_unique_chords`
Expected: FAIL — `KeyError: 'chord_legend_labels'`

- [ ] **Step 3: Update the implementation**

In `lyricvideo/pipeline.py`, find the render call:

```python
    if start_idx <= STAGES.index("render"):
        report("render")
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            font_path or default_font(),
            **assemble_kwargs,
        )
```

Replace with:

```python
    if start_idx <= STAGES.index("render"):
        report("render")
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            font_path or default_font(),
            chord_legend_labels=ordered_unique_chords(song.chord_track),
            **assemble_kwargs,
        )
```

`show_chord_legend` itself already flows through `**assemble_kwargs` once Task 6
adds it to `Settings.render_kwargs()` — no separate wiring needed here.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: 26 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/pipeline.py tests/test_pipeline.py CLAUDE.md
git commit -m "run_pipeline: pass the song's ordered unique chords to assemble_video"
```

---

## Task 6: Settings — show_chord_legend toggle

**Files:**
- Modify: `lyricvideo/settings.py`
- Modify: `tests/test_settings.py`

**Interfaces:**
- Produces: `Settings.show_chord_legend: bool = True`, included in
  `Settings.render_kwargs()`'s returned dict under the same key name.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_settings.py`, inside
`test_settings_defaults_match_current_hardcoded_render_behavior` (find the last
assertion and add one after it):

```python
    assert s.min_chord_seconds == 0.5
```

Replace with:

```python
    assert s.min_chord_seconds == 0.5
    assert s.show_chord_legend is True
```

Add a new test after `test_render_kwargs_matches_defaults_when_settings_are_default`:

```python
def test_render_kwargs_includes_show_chord_legend():
    assert Settings().render_kwargs()["show_chord_legend"] is True
    assert Settings(show_chord_legend=False).render_kwargs()["show_chord_legend"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_settings.py -v -k "defaults_match or render_kwargs_includes_show_chord_legend"`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'show_chord_legend'`

- [ ] **Step 3: Update the implementation**

In `lyricvideo/settings.py`, find:

```python
    # --- Chord bar (render.py) -------------------------------------------
    show_chord_timeline: bool = True
    show_key_bpm: bool = True
    timeline_window_sec: float = 12.0
```

Replace with:

```python
    # --- Chord bar (render.py) -------------------------------------------
    show_chord_timeline: bool = True
    show_key_bpm: bool = True
    timeline_window_sec: float = 12.0
    show_chord_legend: bool = True
```

Find:

```python
            "show_chord_timeline": self.show_chord_timeline,
            "show_key_bpm": self.show_key_bpm,
            "timeline_window_sec": self.timeline_window_sec,
        }
```

Replace with:

```python
            "show_chord_timeline": self.show_chord_timeline,
            "show_key_bpm": self.show_key_bpm,
            "timeline_window_sec": self.timeline_window_sec,
            "show_chord_legend": self.show_chord_legend,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_settings.py -v`
Expected: 15 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/settings.py tests/test_settings.py CLAUDE.md
git commit -m "Settings: add show_chord_legend toggle"
```

---

## Task 7: SettingsPanel — checkbox for the chord legend

**Files:**
- Modify: `lyricvideo/settings_panel.py`

**Interfaces:** none new (widget wiring only — this project's existing
testing-constraint precedent for GUI code; verified manually in Step 2).

- [ ] **Step 1: Add the checkbox**

In `lyricvideo/settings_panel.py`, find:

```python
        self._section("Chord bar")
        self._check("show_chord_timeline", "Show scrolling chord timeline")
        self._check("show_key_bpm", "Show key and BPM")
        self._slider("timeline_window_sec", "Timeline look-ahead", 4, 30, 26, lambda v: f"{v:.0f}s")
```

Replace with:

```python
        self._section("Chord bar")
        self._check("show_chord_timeline", "Show scrolling chord timeline")
        self._check("show_key_bpm", "Show key and BPM")
        self._slider("timeline_window_sec", "Timeline look-ahead", 4, 30, 26, lambda v: f"{v:.0f}s")
        self._check("show_chord_legend", "Show chord fingering chart (upper-left)")
```

- [ ] **Step 2: Manually verify the checkbox appears and round-trips**

```bash
cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/test_settings_panel.py -v
```

Expected: 5 passed (unchanged — `values_to_settings` already tolerates any valid
`Settings` field via `Settings.from_dict`, no new pure-function test needed for
a plain checkbox).

Then:

```bash
DISPLAY="${DISPLAY:-:0}" .venv/bin/python -c "
import customtkinter as ctk
from lyricvideo.settings import Settings
from lyricvideo.settings_panel import SettingsPanel

root = ctk.CTk()
root.geometry('500x700')
panel = SettingsPanel(root, Settings())
panel.pack(fill='both', expand=True)
root.after(2000, root.destroy)
root.mainloop()
print('checkbox smoke test ok')
"
```

Confirm the window shows "Show chord fingering chart (upper-left)" checked by
default in the Chord bar section, no traceback.

- [ ] **Step 3: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/settings_panel.py CLAUDE.md
git commit -m "SettingsPanel: add checkbox for the chord fingering chart"
```

---

## Task 8: Live preview shows the chord legend too

**Files:**
- Modify: `lyricvideo/settings_preview.py`
- Modify: `tests/test_settings_preview.py`

**Interfaces:**
- Consumes: `lyricvideo.pipeline.ordered_unique_chords` (Task 3),
  `assemble_video`-style `chord_legend_labels`/`show_chord_legend` now accepted
  wherever `render_kwargs()` values are used (Task 6 already put
  `show_chord_legend` in `render_kwargs()`, so `render_preview_frame` just needs
  to also call `draw_chord_legend`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_settings_preview.py`:

```python
def test_render_preview_frame_shows_the_chord_legend():
    shown = np.array(render_preview_frame(Settings(show_chord_legend=True)))
    hidden = np.array(render_preview_frame(Settings(show_chord_legend=False)))

    assert not np.array_equal(shown, hidden)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settings_preview.py -v -k shows_the_chord_legend`
Expected: FAIL — `AssertionError` (both frames identical, since the legend isn't
drawn yet)

- [ ] **Step 3: Update the implementation**

In `lyricvideo/settings_preview.py`, find:

```python
from .layout import Scene, SceneLine, SceneWord
from .models import ChordEvent, ChordTrack
from .pipeline import default_font
from .render import draw_chord_bar, draw_scene
from .settings import Settings
```

Replace with:

```python
from .chord_diagram import draw_chord_legend
from .layout import Scene, SceneLine, SceneWord
from .models import ChordEvent, ChordTrack, current_chord_at
from .pipeline import default_font, ordered_unique_chords
from .render import draw_chord_bar, draw_scene
from .settings import Settings
```

Find:

```python
    frame = draw_chord_bar(
        frame, _PREVIEW_CHORD_TRACK, _PREVIEW_TIME, font_path,
        frame_size=frame_size, accent_color=render_kwargs["accent_color"],
        dim_text_color=render_kwargs["dim_text_color"], panel_color=render_kwargs["panel_color"],
        panel_alpha=render_kwargs["panel_alpha"], chord_now_size=render_kwargs["chord_now_size"],
        chord_next_size=render_kwargs["chord_next_size"], show_chord_timeline=render_kwargs["show_chord_timeline"],
        show_key_bpm=render_kwargs["show_key_bpm"], timeline_window_sec=render_kwargs["timeline_window_sec"],
    )
    if frame.size != preview_size:
        frame = frame.resize(preview_size, Image.LANCZOS)
    return frame
```

Replace with:

```python
    frame = draw_chord_bar(
        frame, _PREVIEW_CHORD_TRACK, _PREVIEW_TIME, font_path,
        frame_size=frame_size, accent_color=render_kwargs["accent_color"],
        dim_text_color=render_kwargs["dim_text_color"], panel_color=render_kwargs["panel_color"],
        panel_alpha=render_kwargs["panel_alpha"], chord_now_size=render_kwargs["chord_now_size"],
        chord_next_size=render_kwargs["chord_next_size"], show_chord_timeline=render_kwargs["show_chord_timeline"],
        show_key_bpm=render_kwargs["show_key_bpm"], timeline_window_sec=render_kwargs["timeline_window_sec"],
    )
    current = current_chord_at(_PREVIEW_CHORD_TRACK, _PREVIEW_TIME)
    frame = draw_chord_legend(
        frame, ordered_unique_chords(_PREVIEW_CHORD_TRACK), current.label if current is not None else None,
        font_path, frame_size=frame_size, show_chord_legend=render_kwargs["show_chord_legend"],
        accent_color=render_kwargs["accent_color"], text_color=render_kwargs["text_color"],
        dim_text_color=render_kwargs["dim_text_color"], panel_color=render_kwargs["panel_color"],
        panel_alpha=render_kwargs["panel_alpha"],
    )
    if frame.size != preview_size:
        frame = frame.resize(preview_size, Image.LANCZOS)
    return frame
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_settings_preview.py -v`
Expected: 9 passed

- [ ] **Step 5: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/settings_preview.py tests/test_settings_preview.py CLAUDE.md
git commit -m "Settings preview: show the chord fingering legend too"
```

---

## Task 9: Final CLAUDE.md rewrite and full-suite/GUI verification

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Rewrite the pipeline-stages "render" bullet in CLAUDE.md**

Find:

```
7. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling lyrics
   (karaoke word-highlight sweep, Ken Burns pans) plus a NOW/NEXT/segmented-
   timeline chord bar and a Key/BPM badge over the audio into the final 1080p mp4
   (`work_dir/<slugified-title>.mp4`). During an instrumental gap (past a line's
   own `end_time`, before the next line's `start_time`, or outside any line at
   all) the background image follows the active chord instead of freezing on the
   last-sung line — `layout.py`'s `_in_a_line()` decides which applies. No song
   title or artist text is drawn into the frame anywhere (owner decision,
   2026-09-09) — only the chord bar and the Key/BPM badge were added to the frame.
```

Replace with:

```
7. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling lyrics
   (karaoke word-highlight sweep, Ken Burns pans), a NOW/NEXT/segmented-timeline
   chord bar, a Key/BPM badge, and a chord fingering legend
   (`lyricvideo/chord_shapes.py` + `chord_diagram.py`) over the audio into the
   final mp4 (`work_dir/<slugified-title>.mp4`). The legend shows one small
   guitar diagram per unique chord in the song (`pipeline.ordered_unique_chords()`,
   first-appearance order), upper-left, with the currently-playing chord's
   diagram highlighted; fingering data is extracted from `tombatossals/chords-db`
   (MIT licensed), not hand-authored — every one of the 12 roots x 5 qualities
   `detect_chords()` can produce resolves to a real shape. During an instrumental
   gap (past a line's own `end_time`, before the next line's `start_time`, or
   outside any line at all) the background image follows the active chord
   instead of freezing on the last-sung line — `layout.py`'s `_in_a_line()`
   decides which applies. No song title or artist text is drawn into the frame
   anywhere (owner decision, 2026-09-09) — only the chord bar, Key/BPM badge, and
   chord legend were added to the frame.
```

- [ ] **Step 2: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: every test passes.

- [ ] **Step 3: Manual smoke test — mandatory, not optional**

```bash
cd /home/doug/PlayAlongVideoProduction && ./run_playalongvideoproduction.sh
```

Confirm, by actually looking at the window and the Settings panel:
- The "Show chord fingering chart (upper-left)" checkbox is present and checked
  by default in the Chord bar section.
- The live preview pane shows small chord diagrams in its upper-left corner.
- Toggling the checkbox off removes them from the preview; toggling it back on
  restores them.
- Run a real Generate or Redo on a song and confirm the finished video actually
  shows the fingering diagrams in the upper-left, changing which one is
  highlighted as the chords change.

If anything above doesn't hold, fix it and re-run this whole step before moving
on.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the finished chord fingering chart feature"
```

---
