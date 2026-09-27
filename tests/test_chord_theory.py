import numpy as np

from lyricvideo.chord_theory import (
    TRIADS,
    build_templates,
    capo_and_shape_key,
    diatonic_chords,
    is_easy_key,
    key_name,
    key_uses_flats,
    load_easy_chord_capo_marker,
    parse_chord_label,
    save_easy_chord_capo_marker,
    spell,
    transpose_chord_label,
    transpose_chord_track,
)
from lyricvideo.models import ChordEvent, ChordTrack


def test_build_templates_returns_one_row_per_root_per_quality():
    chords, templates = build_templates(TRIADS)  # ["maj", "min"]
    assert len(chords) == 24  # 12 roots x 2 qualities
    assert templates.shape == (24, 12)


def test_build_templates_rows_are_unit_norm():
    _, templates = build_templates(TRIADS)
    norms = np.linalg.norm(templates, axis=1)
    assert np.allclose(norms, 1.0)


def test_diatonic_chords_c_major_contains_expected_triads():
    dia = diatonic_chords(tonic=0, mode="major")
    assert (0, "maj") in dia   # C
    assert (7, "maj") in dia   # G
    assert (9, "min") in dia   # Am
    assert (1, "maj") not in dia  # Db, not diatonic to C major


def test_key_uses_flats_for_f_major():
    assert key_uses_flats(tonic=5, mode="major") is True  # F major


def test_key_uses_flats_false_for_g_major():
    assert key_uses_flats(tonic=7, mode="major") is False


def test_key_name_formats_readably():
    assert key_name(tonic=0, mode="major") == "C major"
    assert key_name(tonic=9, mode="minor") == "A minor"


def test_spell_major_and_minor():
    assert spell(root=0, quality="maj", use_flats=False) == "C"
    assert spell(root=9, quality="min", use_flats=False) == "Am"
    assert spell(root=10, quality="maj", use_flats=True) == "Bb"
    assert spell(root=10, quality="maj", use_flats=False) == "A#"


def test_is_easy_key_true_only_for_a_true_open_chord_shape():
    """Owner, 2026-09-23, the final word after walking it back: "you didnt have them, i added them, maybe i
    shouldnt have" -- F and B are natural tonics, but neither has a true open shape on guitar (F is at least
    a mini-barre, B at least a partial barre), so they're OUT. Only the 5 true open major shapes and 3 true
    open minor shapes count -- the exact same set capo_and_shape_key() uses as its own shape candidates, by
    construction, so the two can never drift apart."""
    for letter in "CDEGA":
        assert is_easy_key(f"{letter} major"), letter
    for letter in "ADE":
        assert is_easy_key(f"{letter} minor"), letter


def test_is_easy_key_false_for_f_and_b_which_have_no_true_open_shape():
    for key in ["F major", "F minor", "B major", "B minor"]:
        assert not is_easy_key(key), key


def test_is_easy_key_false_for_a_natural_tonic_with_no_simple_open_minor_shape():
    """There is no true open Cm or Gm shape, even though C and G are easy MAJOR keys."""
    assert not is_easy_key("C minor")
    assert not is_easy_key("G minor")


def test_is_easy_key_false_for_any_sharp_or_flat_tonic():
    for key in ["Db major", "F# major", "Bb major", "G# major", "C# major", "Eb minor", "F# minor", "Ab minor"]:
        assert not is_easy_key(key), key


def test_is_easy_key_false_for_blank_or_unknown():
    assert not is_easy_key("")
    assert not is_easy_key("unknown")


# --- capo conversion (owner, 2026-09-23: "make sure the formulas for capo postion are correct" -- verified
# against guitar-chord.org's own capo transposition chart, see the design doc) --------------------------------

def test_capo_and_shape_key_matches_the_verified_chart_for_every_hard_major_key():
    assert capo_and_shape_key("Db major") == (1, "C")
    assert capo_and_shape_key("Eb major") == (1, "D")
    assert capo_and_shape_key("F# major") == (2, "E")
    assert capo_and_shape_key("Ab major") == (1, "G")
    assert capo_and_shape_key("Bb major") == (1, "A")


def test_capo_and_shape_key_matches_the_verified_chart_for_every_hard_minor_key():
    """The two that would be wrong if minors just mirrored the majors' capo numbers."""
    assert capo_and_shape_key("Db minor") == (4, "Am")
    assert capo_and_shape_key("Eb minor") == (1, "Dm")
    assert capo_and_shape_key("F# minor") == (2, "Em")
    assert capo_and_shape_key("Ab minor") == (4, "Em")
    assert capo_and_shape_key("Bb minor") == (1, "Am")


def test_capo_and_shape_key_is_none_for_every_true_open_shape_key():
    for letter in "CDEGA":
        assert capo_and_shape_key(f"{letter} major") is None
    for letter in "ADE":
        assert capo_and_shape_key(f"{letter} minor") is None


def test_capo_and_shape_key_offers_a_conversion_for_f_and_b_too():
    """Owner, 2026-09-23: reversed the earlier call to treat F/B as already-easy -- neither has a true open
    shape, so they get a real capo suggestion like any other hard key, verified the same way (guitar-chord.org)."""
    assert capo_and_shape_key("F major") == (1, "E")
    assert capo_and_shape_key("F minor") == (1, "Em")
    assert capo_and_shape_key("B major") == (2, "A")
    assert capo_and_shape_key("B minor") == (2, "Am")


def test_capo_and_shape_key_is_none_for_blank_or_unknown():
    assert capo_and_shape_key("") is None
    assert capo_and_shape_key("unknown") is None


def test_parse_chord_label_reads_root_and_quality():
    assert parse_chord_label("C") == (0, "maj")
    assert parse_chord_label("Am") == (9, "min")
    assert parse_chord_label("F#m7") == (6, "min7")
    assert parse_chord_label("Bbmaj7") == (10, "maj7")
    assert parse_chord_label("G7") == (7, "7")


def test_parse_chord_label_returns_none_for_no_chord_or_garbage():
    assert parse_chord_label("N") is None
    assert parse_chord_label("") is None


def test_transpose_chord_label_shifts_down_by_the_capo_amount():
    # Bridge Over Troubled Water, real chords, Eb major -> capo 1, D shapes.
    assert transpose_chord_label("Eb", 1) == "D"
    assert transpose_chord_label("Cm7", 1) == "Bm7"
    assert transpose_chord_label("Abmaj7", 1) == "Gmaj7"
    assert transpose_chord_label("Bb7", 1) == "A7"


def test_transpose_chord_label_without_a_shape_key_spells_with_sharps():
    """With no shape key to spell in, a transposed chord reads as a plain sharp."""
    assert transpose_chord_label("B", 1) == "A#"
    assert transpose_chord_label("Gb", 2) == "E"        # F# major -> E shapes (capo 2)


def test_transpose_chord_label_spells_in_the_shape_key():
    """Issue #7 review, F100/F101: Dm shapes are a flat key (their VI is Bb), and a borrowed bVII in C shapes is Bb."""
    assert transpose_chord_label("B", 1, "Dm") == "Bb"          # Eb minor's VI (Cb/B) -> D-minor shapes
    assert transpose_chord_label("B", 1, "C") == "Bb"           # Db major's bVII (Cb/B) -> C shapes
    assert transpose_chord_label("B", 1, "C", prefer_flats=False) == "A#"
    assert transpose_chord_label("Gb", 2, "E") == "E"


def test_transpose_chord_label_leaves_no_chord_unchanged():
    assert transpose_chord_label("N", 1) == "N"


def test_transpose_chord_track_shifts_every_event_and_relabels_the_key_to_the_shape():
    # Bridge Over Troubled Water, real chords, Eb major -> capo 1, D shapes.
    track = ChordTrack(
        events=[
            ChordEvent(start=0.0, end=2.0, label="Eb"),
            ChordEvent(start=2.0, end=4.0, label="Cm7"),
            ChordEvent(start=4.0, end=6.0, label="N"),
        ],
        key="Eb major",
        bpm=83.4,
    )
    transposed = transpose_chord_track(track, 1, "D")
    assert [e.label for e in transposed.events] == ["D", "Bm7", "N"]
    assert [(e.start, e.end) for e in transposed.events] == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    assert transposed.key == "D major"
    assert transposed.bpm == 83.4


def test_transpose_chord_track_relabels_the_key_to_a_minor_shape():
    track = ChordTrack(events=[ChordEvent(start=0.0, end=1.0, label="F#m")], key="F# minor", bpm=120.0)
    transposed = transpose_chord_track(track, 2, "Em")
    assert transposed.key == "E minor"


def test_easy_chord_capo_marker_round_trips(tmp_path):
    # Owner, 2026-09-23: "should maybe have that in the upload file too" -- the upload code (which can run
    # days after the render) needs a durable, structured way to know a song is an EASY CHORD (capo) variant,
    # not just a folder-name/title-suffix convention it would have to re-parse and trust.
    save_easy_chord_capo_marker(
        tmp_path, capo_fret=1, shape_key="D", original_key="Eb major", original_title="Bridge Over Troubled Water",
    )
    assert load_easy_chord_capo_marker(tmp_path) == {
        "capo_fret": 1, "shape_key": "D", "original_key": "Eb major",
        "original_title": "Bridge Over Troubled Water",
    }


def test_easy_chord_capo_marker_is_none_for_an_ordinary_song(tmp_path):
    assert load_easy_chord_capo_marker(tmp_path) is None


def test_easy_chord_capo_marker_is_none_for_a_corrupt_file(tmp_path):
    (tmp_path / "easy_chord_capo.json").write_text("not json", encoding="utf-8")
    assert load_easy_chord_capo_marker(tmp_path) is None


def test_capo_track_matches_accepts_the_real_transposition_and_rejects_anything_else():
    from lyricvideo.chord_theory import capo_track_matches
    original = ChordTrack(events=[ChordEvent(0.0, 2.0, "Eb"), ChordEvent(2.0, 4.0, "Cm7"), ChordEvent(4.0, 5.0, "N")], key="Eb major", bpm=90.0)
    good = transpose_chord_track(original, 1, "D")
    assert capo_track_matches(original, good, 1)
    assert not capo_track_matches(original, good, 2)                       # wrong capo for these shapes
    wrong_chord = ChordTrack(events=[ChordEvent(0.0, 2.0, "D"), ChordEvent(2.0, 4.0, "Bm"), ChordEvent(4.0, 5.0, "N")], key="D major", bpm=90.0)
    assert not capo_track_matches(original, wrong_chord, 1)                # Cm7 became Bm (quality lost)
    shifted_time = ChordTrack(events=[ChordEvent(0.1, 2.0, "D"), ChordEvent(2.0, 4.0, "Bm7"), ChordEvent(4.0, 5.0, "N")], key="D major", bpm=90.0)
    assert not capo_track_matches(original, shifted_time, 1)               # timing must be untouched
    assert not capo_track_matches(original, ChordTrack(events=good.events[:2], key="D major", bpm=90.0), 1)   # an event went missing


# --- issue #7 review, F100/F101: spelling by what the chord does in the key ---------------------------------------------

def test_spell_in_key_keeps_the_keys_own_letters():
    from lyricvideo.chord_theory import spell_in_key
    assert spell_in_key(10, "maj", 2, "major") == "Bb"        # bVI of D major
    assert spell_in_key(10, "maj", 0, "major") == "Bb"        # bVII of C major
    assert spell_in_key(3, "maj", 0, "major") == "Eb"         # bIII of C major
    assert spell_in_key(6, "min", 0, "major") == "F#m"        # #iv of C major
    assert spell_in_key(8, "maj", 9, "minor") == "G#"         # raised 7th of A minor
    assert spell_in_key(1, "maj", 2, "minor") == "C#"         # raised 7th of D minor
    assert spell_in_key(5, "maj", 6, "major") == "F"          # E# in F# major has no chord diagram: F
    assert spell_in_key(11, "maj", 6, "major") == "B"         # IV of F# major
    assert spell_in_key(11, "maj", 6, "major", prefer_flats=True) == "B"
    assert spell_in_key(11, "maj", 1, "major") == "B"         # bVII of Db major is Cb: written B
    assert spell_in_key(10, "maj", 2, "major", prefer_flats=False) == "A#"


def test_spell_in_key_always_gives_the_same_chord_and_a_name_the_app_knows():
    from lyricvideo.chord_shapes import CHORD_SHAPES
    from lyricvideo.chord_theory import QUALITY_SUFFIX, spell_in_key
    for tonic in range(12):
        for mode in ("major", "minor"):
            for root in range(12):
                for quality in QUALITY_SUFFIX:
                    for prefer_flats in (True, False):
                        name = spell_in_key(root, quality, tonic, mode, prefer_flats)
                        assert parse_chord_label(name) == (root, quality), (tonic, mode, root, quality, name)
                        assert name in CHORD_SHAPES


def test_spell_in_key_matches_the_keys_own_flat_or_sharp_convention_for_its_own_chords():
    from lyricvideo.chord_theory import NOTES_FLAT, NOTES_SHARP, spell_in_key
    major_steps, minor_steps = (0, 2, 4, 5, 7, 9, 11), (0, 2, 3, 5, 7, 8, 10)
    for tonic in range(12):
        for mode, steps in (("major", major_steps), ("minor", minor_steps)):
            names = NOTES_FLAT if key_uses_flats(tonic, mode) else NOTES_SHARP
            for step in steps:
                root = (tonic + step) % 12
                assert spell_in_key(root, "maj", tonic, mode) == names[root], (tonic, mode, root)


def test_an_eb_minor_songs_easy_version_spells_d_minor_shapes_with_flats():
    from lyricvideo.chord_theory import capo_track_matches
    original = ChordTrack(events=[ChordEvent(0.0, 2.0, "Ebm"), ChordEvent(2.0, 4.0, "B"), ChordEvent(4.0, 6.0, "Gb"),
                                  ChordEvent(6.0, 8.0, "Db"), ChordEvent(8.0, 9.0, "N")], key="Eb minor", bpm=90.0)
    assert capo_and_shape_key("Eb minor") == (1, "Dm")
    easy = transpose_chord_track(original, 1, "Dm")
    assert [e.label for e in easy.events] == ["Dm", "Bb", "F", "C", "N"] and easy.key == "D minor"
    assert capo_track_matches(original, easy, 1)
    sharps = transpose_chord_track(original, 1, "Dm", prefer_flats=False)
    assert [e.label for e in sharps.events][:2] == ["Dm", "A#"] and capo_track_matches(original, sharps, 1)


def test_an_f_sharp_major_songs_easy_version_keeps_e_shape_sharps():
    original = ChordTrack(events=[ChordEvent(0.0, 2.0, "F#"), ChordEvent(2.0, 4.0, "B"), ChordEvent(4.0, 6.0, "C#"),
                                  ChordEvent(6.0, 8.0, "D#m")], key="F# major", bpm=90.0)
    easy = transpose_chord_track(original, 2, "E")
    assert [e.label for e in easy.events] == ["E", "A", "B", "C#m"] and easy.key == "E major"


def test_a_c_shape_easy_version_spells_a_borrowed_bvii_as_bb():
    original = ChordTrack(events=[ChordEvent(0.0, 2.0, "Db"), ChordEvent(2.0, 4.0, "B"), ChordEvent(4.0, 6.0, "Ab")],
                          key="Db major", bpm=90.0)
    assert capo_and_shape_key("Db major") == (1, "C")
    assert [e.label for e in transpose_chord_track(original, 1, "C").events] == ["C", "Bb", "G"]
