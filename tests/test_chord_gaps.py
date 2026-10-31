import numpy as np

from lyricvideo.chord_gaps import fill_no_chord_gaps, name_for_root, power_chord_root
from lyricvideo.models import ChordEvent, ChordTrack

E, F, G, A, B = 4, 5, 7, 9, 11


def _chroma(**levels):
    c = np.full(12, 0.1)
    for name, value in levels.items():
        c[{"E": E, "F": F, "G": G, "A": A, "B": B, "Gs": 8, "Ds": 3}[name]] = value
    return c


def _loud(_a, _b):
    return 0.0


def test_a_root_with_its_fifth_second_loudest_is_a_power_chord():
    assert power_chord_root(_chroma(E=1.0, B=0.8, F=0.5)) == (E, False)


def test_a_muddy_bar_is_not_read_as_a_power_chord():
    assert power_chord_root(_chroma(E=1.0, F=0.9, B=0.8)) is None                  # the fifth is not second
    assert power_chord_root(_chroma(A=1.0, E=0.92, Gs=0.88), margin=0.05) is None  # second, but not clearly


def test_a_power_chord_is_named_the_way_the_song_uses_that_root():
    events = [ChordEvent(0, 10, "Em"), ChordEvent(10, 12, "E")]
    assert name_for_root(E, events, minor_hint=False) == "Em"
    assert name_for_root(A, events, minor_hint=False) == "A"                       # unused root: the chroma's third
    assert name_for_root(A, events, minor_hint=True) == "Am"


def test_no_chord_over_music_takes_the_full_mix_chords():
    track = ChordTrack([ChordEvent(0, 4, "Em"), ChordEvent(4, 12, "N"), ChordEvent(12, 16, "C")], "E minor", 120.0)
    mix = [ChordEvent(0, 8, "G"), ChordEvent(8, 16, "D")]

    result, filled = fill_no_chord_gaps(track, level_db=_loud, full_mix_events=lambda: mix,
                                        bar_chroma=lambda a, b: np.zeros(12))

    assert [(e.start, e.end, e.label) for e in result.events] == [(0, 4, "Em"), (4, 8, "G"), (8, 12, "D"), (12, 16, "C")]
    assert filled == 8
    assert result.key == "E minor" and result.bpm == 120.0


def test_what_the_full_mix_cannot_read_is_filled_with_power_chords_and_muddy_bars_take_the_stretch():
    track = ChordTrack([ChordEvent(0, 8, "N"), ChordEvent(8, 12, "Em")], "", 120.0)       # a bar is 2 s at 120 BPM

    def chroma(a, b):
        return _chroma(E=1.0, B=0.8) if (a, b) in ((0, 8), (6, 8)) else _chroma(E=1.0, F=0.95, G=0.9)

    result, _ = fill_no_chord_gaps(track, level_db=_loud, full_mix_events=lambda: [], bar_chroma=chroma)

    assert [(e.start, e.end, e.label) for e in result.events] == [(0, 12, "Em")]


def test_silence_and_short_gaps_stay_no_chord_and_never_run_the_full_mix():
    track = ChordTrack([ChordEvent(0, 4, "Em"), ChordEvent(4, 8, "N"), ChordEvent(8, 8.5, "N")], "", 120.0)

    def no_mix():
        raise AssertionError("the full mix must not be analysed")

    result, filled = fill_no_chord_gaps(track, level_db=lambda a, b: -60.0, full_mix_events=no_mix,
                                        bar_chroma=lambda a, b: _chroma(E=1.0, B=0.8))

    assert result is track and filled == 0
