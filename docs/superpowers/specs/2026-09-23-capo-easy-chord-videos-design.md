# EASY CHORD Play Along videos (capo conversion)

## Problem

Many already-passing songs are in a "hard" key (Db, Eb, F#/Gb, Ab, Bb, major
or minor) whose chords require barre shapes. A guitarist can play the exact
same recording using easy open-chord shapes by placing a capo at the right
fret and reading the transposed chord names instead. Owner (2026-09-23):
"all the songs that have passed, and are ready for up load, everyone of
them i want to create another video, same images, lyrics mp3, except the
easy chords will be displayed and the capo position such as CAPO 3,"
followed by confirming naming should match the existing EASY CHORD
playlist branding: "EASY CHORD Play Along videos."

## The capo formula

**Verified by direct computation, not assumed** -- the first draft of this
table was wrong for minors (it mirrored the major capo numbers instead of
recomputing) and right for majors only by accident. The real rule: for a
hard tonic, the capo fret is the smallest semitone distance up from a
*lower* shape-key pitch class, in the same mode. Critically, the shape-key
candidates are **not** the same 7-tonic set `is_easy_key()` uses -- that
rule (natural tonic, no sharp/flat) correctly answers "does this song's
own key need a capo at all," including F and B, but F and B have no true
*open* chord shape on guitar (both are normally at least a mini-barre), so
no real capo chart ever uses them as the shape to fret. The candidates
here are only the shapes every guitarist actually knows as fully open:
**C, D, E, G, A** for major, and **Am, Dm, Em** for minor (there is no
simple open Cm or Gm at all, which is exactly why the minor numbers below
diverge from the major ones):

| Hard key | Capo | Shape |
|---|---|---|
| Db | 1 | C |
| Dbm (C#m) | 4 | Am |
| Eb | 1 | D |
| Ebm (D#m) | 1 | Dm |
| F# (Gb) | 2 | E |
| F#m (Gbm) | 2 | Em |
| Ab | 1 | G |
| Abm (G#m) | 4 | Em |
| Bb | 1 | A |
| Bbm (A#m) | 1 | Am |

**Independently verified against a real, published capo transposition
chart** (owner, 2026-09-23: "this has to be right.... you must research
and make absolutly sure you have the correct capo position and chords"),
not just this project's own derivation -- every one of the 10 rows above,
including the two corrected minor values (Dbm and Abm both landing at
capo 4, not capo 1), matches [guitar-chord.org's capo transposition
chart](https://www.guitar-chord.org/transposition-chart-for-capo.html)
exactly, and in every case it's also the smallest capo fret the real
chart itself shows as reachable for that key.

`chord_theory.capo_and_shape_key(key: str) -> tuple[int, str] | None`
returns `None` for a key that's already easy (`is_easy_key`) -- nothing to
convert. For a hard key it returns `(capo_fret, shape_key)`, computed
against the restricted CAGED-shape candidate set above (a new, smaller
constant, distinct from `is_easy_key`'s own `_NATURAL_TONICS`), not
derived from or reusing `is_easy_key` beyond the initial "is this key hard
at all" check.

Every chord event in the song is then re-spelled down by `capo_fret`
semitones: parse the existing label back into `(root, quality)` (root from
`NOTES_SHARP`/`NOTES_FLAT`, quality from reversing `QUALITY_SUFFIX`, longest
suffix first so `"m7"` isn't mistaken for `"m"`), subtract the capo fret mod
12, and re-spell with `chord_theory.spell()` using the shape key's own
flats/sharps convention. The **audio, images, timing, and lyrics are
completely unchanged** -- only the displayed chord labels and one on-screen
badge differ.

## Design: a capo video is just another song

Rather than inventing new upload/state-tracking machinery, a capo video is
built as an ordinary second song folder, `work/<slug>-capo/`, so every
existing downstream system (pending-uploads list, the Upload button,
`organize_video()`'s playlists, `cleared_log.py`) treats it exactly like any
other finished song with zero new code:

- `song_info.json`: same artist/duration as the original; title is the
  original title with an `EasyChords` marker suffix -- see Naming below
  for exactly why and how this drives the output filename.
- `lyrics_timed.json`: identical lyric lines/timing to the original, but a
  **new `ChordTrack`** whose events are the transposed labels and whose
  `key` is the shape key. `chord_theory.transpose_chord_track(chord_track,
  capo_fret, shape_key)` builds it.
- `images/`: **reused directly** from the original work dir (same content
  hash cache), not regenerated -- zero Replicate cost, the entire point of
  the feature.
- The audio file: the same source file the original song used.

A new pipeline entry point, `pipeline.build_capo_variant(work_dir) -> Path
| None`, does this: loads the original song, returns `None` if
`is_easy_key(song.chord_track.key)` (nothing to convert), otherwise builds
the `-capo` work dir as above and calls `run_pipeline(..., start_stage=
"render")` on it (chords and lyrics are already decided; only rendering is
needed) -- the render stage already builds `chord_legend_labels` from
whatever `song.chord_track` it loads, so the legend shows the transposed
(easy) shapes automatically, no extra wiring needed. New `render.py`
element: a small "CAPO {N}" badge, same rounded-box language as
the existing Key/BPM badge, placed under the chord-fingering legend
(owner, 2026-09-23: "i want the CAPO 3 under the chords finger position
area"), not next to Key/BPM.

**Image-cache subtlety (caught in review, not yet a problem in practice):**
instrumental-gap background images are cached under a caption that embeds
the chord's own label text (`layout.instrumental_caption()`:
`f"[Instrumental — chord: {chord_label}]"`). Since the capo variant's
chord_track carries the TRANSPOSED labels, `build_image_timeline()` will
compute different caption keys than the ones the copied `images/` files
were originally cached under, so an instrumental stretch in the capo video
can miss its own specific image and fall back to the nearest real image in
the sequence instead (`assemble_video()`'s existing, already-safe
fallback -- never a flat color, never a new Replicate call, so this never
costs money or crashes). For v1 this is accepted as a minor, purely
cosmetic tradeoff during instrumental stretches specifically -- sung-line
images are keyed by lyric text and are entirely unaffected. Worth
revisiting later only if it's visually noticeable often enough to matter.

## Naming -- the two versions must never be mistaken for each other

Owner, 2026-09-23: "it must have in the file name eas[y]chords... and
when uploaded to youtube, it must have EASY CHORDS in the title and
description," and separately, "keeps the versions separate." Three
independent markers, all deterministic (never left to an AI call to
remember to include, same reasoning as `build_play_along_title()` itself):

1. **Filename.** The `-capo` work dir's own `song_info.json` stores its
   title as `f"{original_title} EasyChords"` (artist/duration copied
   unchanged from the original) -- since `run_pipeline`'s render stage
   names the output file from this same field via `slugify()`, the
   rendered file is `<slug>-easychords.mp4` with no new filename logic
   needed. The clean original title is read from the ORIGINAL work dir
   before this file is written, so it stays available, un-suffixed, for
   the YouTube title below.
2. **YouTube title.** A new deterministic builder,
   `build_easy_chord_title(song_title, artist, capo_fret)`, given the
   clean original title, mirrors `build_play_along_title()`'s convention:

   ```
   "{title} - {artist} - (EASY CHORDS Play Along - Capo {N})"
   ```
3. **YouTube description.** A fixed, deterministic sentence is prepended
   to the description before anything AI-authored, e.g. `f"EASY CHORDS
   version -- Capo {capo_fret}, play it in {shape_key} shapes (original
   key: {original_key}).\n\n"`, so the phrase's presence never depends on
   an LLM call remembering to include it.

## Triggering it

Three ways in, matching the app's existing toggle/per-action/backfill
pattern (owner, 2026-09-23: "have a easy chord setting, so if i have that
checked it will convert to easy chord?" / "any video make" / "i could redo
a song with easy chords checked.. it would redo that song with the capo
and easy chords"):

1. **A persistent `Settings.generate_easy_chord_versions` checkbox**
   (SettingsPanel, next to the other render toggles like
   `show_chord_legend`) -- when on, `run_pipeline()` calls
   `build_capo_variant()` right after a song finishes rendering, for EVERY
   future Generate/Redo/Batch run whose song lands in a hard key. No extra
   click; the render is cheap (no new images, no new AI calls) so this is
   safe to leave on by default off/on per owner preference.
2. **A per-Redo "Easy Chords" checkbox**, same row as the existing
   "Generate new images" checkbox on "Redo an Existing Song" -- checked,
   that one redo also (re)builds the capo variant regardless of the global
   setting above, so the owner can request it for one specific song
   without turning the setting on for everything.
3. **A one-time "Generate EASY CHORD Versions (existing songs)" action** --
   catches up the backlog: every already-passing, hard-key song with no
   `<slug>-capo` folder yet, run one at a time with a progress log, same
   idiom as "Batch: Process a Folder."

Either way in, each result lands in the existing rendered-songs/pending-uploads
lists exactly like a normal song, so the owner uploads it the same way
(manually, or automatically if auto-upload is on) -- no new upload UI.

## Error handling

- A song whose key is already easy: skipped, not an error.
- A `-capo` folder that already exists: skipped (idempotent re-run), unless
  the owner explicitly re-triggers it (same "Generate new images" style
  override precedent, but here there's no image work to redo -- a plain
  re-render is cheap enough to just always allow on request).
- Anything else (missing images, corrupt chord track) is caught and logged
  per-song, never aborting the rest of the batch -- same convention as
  every other batch operation in this app.

## Testing

- `chord_theory.py`: `capo_and_shape_key()` pinned against the exact
  verified table above for all 10 hard key/mode combinations (the minor
  rows especially -- Dbm/Abm at capo 4 are the two easiest to get wrong by
  assuming they mirror the majors), plus every easy key (returns `None`);
  `transpose_chord_track()` round-trips a few real chord labels (including
  7ths) through parse -> transpose -> re-spell.
- `pipeline.py`: `build_capo_variant()` returns `None` for an easy-key song
  without touching disk; builds the expected `-capo` work dir contents for
  a hard-key song (mocking the actual render call, same pattern as existing
  `run_pipeline` tests); is idempotent on a second call.
- `render.py`: the capo badge renders only when a capo value is passed in,
  positioned under the chord legend, same style-of-test as the existing
  Key/BPM badge tests.
- `settings.py`/`settings_panel.py`: `generate_easy_chord_versions` follows
  the same default-value/dirty-indicator/save-confirmation pattern as every
  other toggle (no bespoke test needed beyond what the existing
  parametrized toggle tests already cover, if any -- else one direct test).
- `pipeline.py`: `run_pipeline()` calls `build_capo_variant()` after a
  render exactly when the setting is on AND the song's key is hard; not at
  all when the setting is off, and not for an already-easy-key song.
- GUI: the one-time backfill action lists exactly the songs matching the
  criteria in "Triggering it" above (rendered, passing, hard key, no
  existing `-capo` folder) -- same test idiom as the other lazy-built
  collapsible song lists (Redo/Upload/Flagged).

## Not in scope for v1

- **Alternate tunings** (owner mentioned "sometimes the guitar is
  retuned"): unlike capo transposition, there's no clean universal formula
  -- real alternate tunings (Drop D, Open G, DADGAD, etc.) are chosen per
  song by ear/convention, not derivable from the detected key alone, and
  `crema`'s chord detection has no concept of altered tunings. Deferred
  indefinitely unless the owner wants to hand-curate specific songs.
- The Key/BPM badge brightness (owner, 2026-09-23: "thast too faint...
  needs to be just a little bit brighter") is a real but unrelated bug in
  the existing renderer -- fixed separately, not part of this feature.
