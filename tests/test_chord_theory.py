import numpy as np

from lyricvideo.chord_theory import (
    KS_MAJOR,
    TRIADS,
    build_templates,
    diatonic_chords,
    estimate_key,
    key_name,
    key_uses_flats,
    spell,
)


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
