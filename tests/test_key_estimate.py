import pytest

from lyricvideo.key_estimate import estimate_key_from_chords, parse_key, respell_chord_track
from lyricvideo.models import ChordEvent, ChordTrack


def track(*labels_and_seconds, key="C major"):
    events, t = [], 0.0
    for label, secs in labels_and_seconds:
        events.append(ChordEvent(start=t, end=t + secs, label=label))
        t += secs
    return ChordTrack(events=events, key=key, bpm=100.0)


def test_a_plain_major_progression_gives_that_major_key():
    est = estimate_key_from_chords(track(("D", 8), ("G", 4), ("A", 4), ("D", 8)))
    assert est.name == "D major"


def test_a_minor_progression_gives_the_minor_key():
    est = estimate_key_from_chords(track(("Am", 8), ("F", 4), ("G", 4), ("Am", 8)))
    assert est.name == "A minor"


def test_relative_major_and_minor_are_told_apart_by_the_home_chord():
    # the same four chords; which one the song rests on decides the key
    assert estimate_key_from_chords(track(("C", 10), ("G", 4), ("Am", 4), ("F", 4), ("C", 10))).name == "C major"
    assert estimate_key_from_chords(track(("Am", 10), ("F", 4), ("C", 4), ("G", 4), ("Am", 10))).name == "A minor"


def test_flat_keys_are_spelled_with_flats():
    est = estimate_key_from_chords(track(("Bb", 8), ("Eb", 4), ("F", 4), ("Bb", 8)))
    assert est.name == "Bb major"


def test_sevenths_count_as_their_triad():
    est = estimate_key_from_chords(track(("G7", 8), ("C7", 4), ("D7", 4), ("G7", 8)))
    assert est.name == "G major"


def test_no_chords_gives_no_estimate():
    assert estimate_key_from_chords(ChordTrack()) is None
    assert estimate_key_from_chords(track(("N", 30))) is None


def test_margin_is_small_when_two_keys_fit_almost_equally():
    clear = estimate_key_from_chords(track(("D", 8), ("G", 4), ("A", 4), ("D", 8)))
    # C, G, Am, F with no clear home chord: C major vs A minor is nearly a coin flip
    unclear = estimate_key_from_chords(track(("F", 8), ("C", 8), ("Am", 8), ("G", 8)))
    assert unclear.margin < clear.margin


@pytest.mark.parametrize("text,expected", [
    ("D major", (2, "major")), ("Bb major", (10, "major")), ("F# minor", (6, "minor")),
    ("F#m", (6, "minor")), ("Am", (9, "minor")), ("a minor", (9, "minor")), ("Eb", (3, "major")),
    ("C# Minor", (1, "minor")), ("  G  major ", (7, "major")), ("Db maj", (1, "major")), ("E min", (4, "minor")),
])
def test_parse_key_accepts_the_usual_spellings(text, expected):
    assert parse_key(text) == expected


@pytest.mark.parametrize("text", ["", "H major", "D lydian", "major", "unknown", None])
def test_parse_key_rejects_anything_else(text):
    assert parse_key(text) is None


def test_respell_uses_the_new_keys_flat_or_sharp_convention_and_sets_the_key():
    t = track(("F#m", 4), ("C#", 4), ("N", 2), ("C#m7", 4), key="Gb major")
    out = respell_chord_track(t, "Gb major")
    assert [e.label for e in out.events] == ["Gbm", "Db", "N", "Dbm7"]
    assert out.key == "Gb major"
    back = respell_chord_track(out, "F# minor")
    assert [e.label for e in back.events] == ["F#m", "C#", "N", "C#m7"]
    assert back.key == "F# minor"
    assert [(e.start, e.end) for e in back.events] == [(e.start, e.end) for e in t.events]
    assert back.bpm == t.bpm


def test_respell_leaves_the_input_untouched():
    t = track(("F#m", 4), key="Gb major")
    respell_chord_track(t, "Bb major")
    assert t.events[0].label == "F#m" and t.key == "Gb major"


def test_candidate_keys_lists_the_best_fits_best_first():
    from lyricvideo.key_estimate import candidate_keys
    cands = candidate_keys(track(("C", 10), ("G", 4), ("Am", 4), ("F", 4), ("C", 10)), count=3)
    assert len(cands) == 3 and cands[0] == "C major"
    assert "A minor" in cands            # the relative minor is always in the running for these chords


def test_candidate_keys_is_empty_without_chords():
    from lyricvideo.key_estimate import candidate_keys
    assert candidate_keys(ChordTrack()) == []


def test_chord_summary_shows_the_most_used_chords_with_their_share_of_time():
    from lyricvideo.key_estimate import chord_summary
    assert chord_summary(track(("D", 6), ("G", 3), ("A", 1), ("N", 5))) == "D 60%, G 30%, A 10%"


# --- issue #7 review, F117/F123: every real key spelling parses, nothing raises -----------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Cb major", (11, "major")), ("Fb", (4, "major")), ("E# minor", (5, "minor")), ("B#", (0, "major")),
    ("F♯ Minor", (6, "minor")), ("B♭ MAJOR", (10, "major")), ("F♯ minor", (6, "minor")), ("E♭m", (3, "minor")),
    ("f sharp minor", (6, "minor")), ("B-flat major", (10, "major")), ("gb", (6, "major")), ("C#MIN", (1, "minor")),
    ("d major.", (2, "major")), ("  e   MINOR  ", (4, "minor")), ("Bbm", (10, "minor")), ("bm", (11, "minor")),
])
def test_parse_key_accepts_every_real_spelling(text, expected):
    assert parse_key(text) == expected


@pytest.mark.parametrize("text", ["E##", "Dbb", "Fx", "CM", "F#M", "D dorian", "C mixolydian", 42, "Bb major key"])
def test_parse_key_returns_none_and_never_raises(text):
    assert parse_key(text) is None


# --- issue #7 review, F100/F101: chords are spelled by what they do in the key -------------------------------------------

def test_respell_spells_borrowed_flat_chords_in_sharp_keys_with_flats():
    d_song = track(("D", 4), ("A#", 4), ("C", 4), ("G", 4), key="")       # I, bVI, bVII, IV in D major
    assert [e.label for e in respell_chord_track(d_song, "D major").events] == ["D", "Bb", "C", "G"]
    c_song = track(("C", 4), ("A#", 4), ("D#", 4), ("G#", 4), ("C#", 4), ("F#m7", 4), key="")
    assert [e.label for e in respell_chord_track(c_song, "C major").events] == ["C", "Bb", "Eb", "Ab", "Db", "F#m7"]


def test_respell_spells_a_minor_keys_raised_seventh_with_a_sharp():
    dm = track(("Dm", 4), ("Db", 4), ("A#", 4), ("A7", 4), key="")        # i, raised vii (C#), VI, V7 in D minor
    assert [e.label for e in respell_chord_track(dm, "D minor").events] == ["Dm", "C#", "Bb", "A7"]


def test_respell_with_the_flats_box_off_uses_sharps_for_chords_and_key():
    t = track(("Bb", 4), ("Eb", 4), ("F", 4), key="Bb major")
    out = respell_chord_track(t, "Bb major", prefer_flats=False)
    assert [e.label for e in out.events] == ["A#", "D#", "F"] and out.key == "A# major"
