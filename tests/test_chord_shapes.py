from lyricvideo.chord_shapes import CHORD_SHAPES, get_chord_shape
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


def test_em7_is_the_standard_open_voicing():
    """chords-db's first E m7 entry is the movable 2x222x barre slid to the nut: 0x000x, open strings with two of
    them muted and no fretting hand to mute them -- unplayable for the beginners this app is for (issue #7 review)."""
    shape = get_chord_shape("Em7")
    assert shape.frets == (0, 2, 2, 0, 3, 0)
    # the same two fingers as Em (022000), plus the pinky
    assert shape.fingers[:3] == get_chord_shape("Em").fingers[:3] == (0, 2, 3)
    assert shape.fingers == (0, 2, 3, 0, 4, 0)
    assert shape.base_fret == 1


def test_no_shape_puts_a_finger_on_an_open_or_muted_string():
    for label, shape in CHORD_SHAPES.items():
        for fret, finger in zip(shape.frets, shape.fingers):
            if fret <= 0:
                assert finger == 0, f"{label}: finger {finger} on a string that isn't fretted"


def test_no_nut_shape_mutes_a_string_between_two_open_ones():
    """Nothing can mute a string that sits between two ringing open strings at the nut."""
    for label, shape in CHORD_SHAPES.items():
        if shape.base_fret != 1:
            continue
        for i in range(1, 5):
            if shape.frets[i] == -1:
                left_open = 0 in shape.frets[:i]
                right_open = 0 in shape.frets[i + 1:]
                assert not (left_open and right_open), f"{label}: muted string {i} between open strings"
