import numpy as np

from lyricvideo.chord_theory import (
    KS_MAJOR,
    TRIADS,
    build_templates,
    capo_and_shape_key,
    diatonic_chords,
    estimate_key,
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


def test_estimate_key_recognizes_c_major_profile():
    # KS_MAJOR itself, rolled to tonic 0, is a perfect C major profile.
    tonic, mode, confidence = estimate_key(KS_MAJOR)
    assert tonic == 0
    assert mode == "major"
    assert confidence > 0.9


def test_estimate_key_handles_all_silence():
    tonic, mode, confidence = estimate_key(np.zeros(12))
    assert tonic == 0
    assert mode == "major"
    assert confidence == 0.0


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


def test_transpose_chord_label_always_spells_with_sharps():
    """None of the CAGED shape keys (C D E G A Am Dm Em) are in chord_theory's own
    _FLAT_MAJOR_TONICS, so a transposed chromatic/borrowed chord always reads as a
    sharp, matching the app's own existing spelling convention exactly."""
    assert transpose_chord_label("B", 1) == "A#"       # Db major -> C shapes (capo 1)
    assert transpose_chord_label("Gb", 2) == "E"        # F# major -> E shapes (capo 2)


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
