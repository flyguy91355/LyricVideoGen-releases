# Chord Fingering Chart — Design

**Date:** 2026-09-09
**Status:** Approved by owner, ready for implementation planning

## Purpose

Owner wants every chord used in a song shown as a small guitar fingering diagram
(the box-with-dots chart from a songbook, not just the chord name) baked into the
rendered video, so a player can see exactly how to form each chord without leaving
the video. Placed in the upper-left corner, one diagram per unique chord in the
song, ordered by first appearance in the progression, with the chord currently
playing visually highlighted.

## Data source

Guitar chord fingerings are not derivable from a chord name by music theory alone
(there are multiple valid ways to play any chord) — they need real reference data.
Rather than hand-authoring this, the owner asked to source it. Verified fit:
**`tombatossals/chords-db`** (MIT licensed — confirmed via its `LICENSE` file),
a comprehensive guitar/ukulele/mandolin/banjo chord database. Its `lib/guitar.json`
stores, per root × suffix, a list of `positions`, each with `frets` (per-string
fret number, -1 = muted, 0 = open, else absolute fret when `baseFret == 1`),
`fingers` (0 = none, 1-4 = finger number), and `baseFret`.

`detect_chords()` can only ever emit one of a closed set of labels: 12 roots
(sharp or flat spelling, from `chord_theory.py`'s `NOTES_SHARP`/`NOTES_FLAT`) ×
5 qualities (`maj`/`min`/`7`/`min7`/`maj7`, from `TRIADS`/`SEVENTHS`). The DB's
`major`/`minor`/`7`/`m7`/`maj7` suffixes map exactly onto those 5 qualities, and
its 12 root keys (`C`, `Csharp`, `D`, `Eb`, `E`, `F`, `Fsharp`, `G`, `Ab`, `A`,
`Bb`, `B`) map 1:1 onto the same 12 pitch-class indices `chord_theory.py` already
uses. So every label the pipeline can produce has a verified real shape — no
algorithmic fallback needed.

**Extraction rule (validated by spot-checking 8 chords across natural and
accidental roots, including F#m/C#m/Ab-major/Bb7/Ab-m7):** for each of the 12
roots × 5 qualities, take `positions[0]` — the dataset's own first/simplest entry.
Every one checked has `baseFret == 1` (meaning `frets` values are plain absolute
fret numbers, 0 = open, -1 = muted) and a max fret of 4, so v1 needs no
relative-to-baseFret math and no "starts at fret N" label — a straightforward
5-row (nut + 4 frets) diagram covers every shape. A one-time extraction script
(not shipped) downloads `guitar.json`, asserts this `baseFret == 1` / max-fret-4
property holds for all 60 selected entries (flagging any exception for manual
review — expected to hold universally based on the sample), and writes the result
into this project's own `lyricvideo/chord_shapes.py` as a plain Python dict keyed
by this project's own label spelling (both sharp and flat spellings map to the
same shape, since they're enharmonically identical) — roughly 85 distinct dict
entries (7 natural roots need one spelling, 5 accidental roots need both) ×
5 qualities. The generated file carries a comment crediting chords-db (MIT).

**Out of scope for v1:** barre-bar visual rendering (a single thick bar spanning
multiple strings) — the `fingers` array already shows "same finger" via a repeated
finger number on each dot, so individual per-string dots are used instead, which is
simpler and still fully accurate. The dataset's `capo` and `barres` fields are
therefore unused. Any suffix beyond the 5 this app detects (9ths, 11ths, sus,
dim, aug, slash chords, etc.) is not extracted at all — smaller bundled data,
nothing to maintain that can never be reached.

## Scope

**In scope:**
1. `lyricvideo/chord_shapes.py` — the extracted `ChordShape` data
   (`frets: tuple[int, ...]` len 6, `fingers: tuple[int, ...]` len 6, both
   low-E-to-high-e) plus `get_chord_shape(label: str) -> ChordShape | None`
   (`None` for `"N"` and anything else unrecognized — must never raise).
2. `lyricvideo/chord_diagram.py` — `draw_single_chord_diagram(shape, label,
   box_size, font_path, *, highlighted, accent_color, text_color, dim_text_color,
   panel_color, panel_alpha) -> Image.Image` (one small diagram: chord name above,
   6 vertical string lines, a thick nut line + 4 fret lines below it, filled dots
   with finger numbers for fretted strings, "X"/"O" above the nut for muted/open
   strings) and `draw_chord_legend(frame, chord_labels, current_label, font_path,
   *, frame_size, show_chord_legend=True, accent_color=..., ...) -> Image.Image`
   (lays out one diagram per label left-to-right in the upper-left, wrapping to
   further rows as needed; skips any label `get_chord_shape` can't resolve rather
   than erroring; highlights whichever diagram matches `current_label`).
3. `pipeline.py` gains `ordered_unique_chords(chord_track: ChordTrack) -> list[str]`
   (first-seen order across every event, sung or instrumental, deduped, `"N"`
   excluded) — same first-seen-order convention as the existing
   `_instrumental_chord_labels`.
4. `assemble_video()` computes the song's `ordered_unique_chords()` once (outside
   the per-frame closure — it's static for the whole song) and calls
   `draw_chord_legend()` every frame with the live current-chord label from its
   own `current_chord_at()` call (a second, independent call from the one
   `draw_chord_bar()` already makes internally — cheap enough not to bother
   sharing, and avoids changing `draw_chord_bar()`'s existing signature).
5. `Settings` gains `show_chord_legend: bool = True`, included in
   `render_kwargs()`; `SettingsPanel` gains a checkbox for it in the existing
   "Chord bar" section; the live preview picks it up automatically (its synthetic
   fixture already has 3 distinct chords: G, D, Am).
6. Tests: `chord_shapes.py` (every label the pipeline can produce resolves to a
   real 6-long shape; `"N"` and garbage resolve to `None`), `chord_diagram.py`
   (renders without error, non-blank; the current chord's diagram visibly differs
   from a non-current one; `show_chord_legend=False` produces no visible change
   from a plain background; an unresolvable label is skipped, not a crash),
   `pipeline.py`'s `ordered_unique_chords` (dedup/order/exclusion, mirroring the
   existing `_instrumental_chord_labels` tests), and extending `test_assemble.py`
   the same way Task 3/5 of the prior plan extended it for the chord bar.

**Out of scope:** barre-bar visuals and capo indicators (see above); any chord
quality beyond the 5 already detected; a user-configurable diagram size (fixed,
proportional to `frame_size`, same convention as the existing chord-bar geometry);
ukulele/other instruments (guitar only, matching this whole app's assumption).

## Architecture

```
pipeline.py
  └─ ordered_unique_chords(chord_track) -> ["G", "D", "Am"]  (first-seen order)

assemble_video(..., show_chord_legend=True)
  ├─ chord_legend_labels = ordered_unique_chords(chord_track)   # once, not per-frame
  └─ make_frame(t):
        ├─ (existing) draw_scene(...), draw_chord_bar(...)
        └─ current = current_chord_at(chord_track, t)     # cheap O(events) lookup, independent of
           draw_chord_legend(                              # draw_chord_bar's own identical internal
               frame, chord_legend_labels,                 # call -- not shared, just called twice
               current_label=(current.label if current is not None else None),
               font_path, frame_size=..., show_chord_legend=show_chord_legend,
               accent_color=..., text_color=..., dim_text_color=...,
               panel_color=..., panel_alpha=...,
           )
               └─ per label: get_chord_shape(label) -> ChordShape | None (skip if None)
                              draw_single_chord_diagram(shape, label, box_size, ...)
```

`chord_shapes.py` (pure data + lookup) and `chord_diagram.py` (pure PIL drawing,
same plain-keyword-argument convention as `render.py` — no `Settings` import) stay
decoupled and independently testable, matching this codebase's established
design-for-isolation convention from the prior Settings/GUI feature.

## Testing

Same pixel-diff style already used throughout `test_render.py`: a non-default
value (a different `current_label`, `show_chord_legend=False`, an unresolvable
label) must visibly/measurably change the rendered output, while the no-argument/
default-argument call path stays identical to before this feature. Widget-level
GUI wiring (the new checkbox) stays manually/visually verified, matching this
project's existing testing-constraint precedent for GUI code.
