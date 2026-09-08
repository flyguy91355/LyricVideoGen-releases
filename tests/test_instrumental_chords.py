from pathlib import Path

import numpy as np
import soundfile as sf

from lyricvideo.instrumental_chords import detect_chord_change_times


def _write_two_tone_wav(path: Path, sr: int = 22050, tone_seconds: float = 3.0):
    """A clip that abruptly changes pitch/harmonic content partway through --
    a stand-in for a real chord change, without using any copyrighted audio."""
    t1 = np.linspace(0, tone_seconds, int(sr * tone_seconds), endpoint=False)
    tone_a = 0.3 * np.sin(2 * np.pi * 220.0 * t1)  # A3
    tone_b = 0.3 * np.sin(2 * np.pi * 330.0 * t1)  # E4
    y = np.concatenate([tone_a, tone_b]).astype(np.float32)
    sf.write(str(path), y, sr)


def test_detect_chord_change_times_returns_requested_count(tmp_path):
    wav_path = tmp_path / "instrumental.wav"
    _write_two_tone_wav(wav_path)

    spans = detect_chord_change_times(wav_path, start_time=0.0, end_time=6.0, num_chords=2)

    assert len(spans) == 2
    # spans are contiguous and cover the full requested range
    assert spans[0][0] == 0.0
    assert spans[-1][1] == 6.0
    for start, end in spans:
        assert start < end


def test_detect_chord_change_times_single_chord_spans_whole_gap(tmp_path):
    wav_path = tmp_path / "instrumental.wav"
    _write_two_tone_wav(wav_path)

    spans = detect_chord_change_times(wav_path, start_time=1.0, end_time=5.0, num_chords=1)

    assert spans == [(1.0, 5.0)]


def test_detect_chord_change_times_zero_chords_returns_empty(tmp_path):
    wav_path = tmp_path / "instrumental.wav"
    _write_two_tone_wav(wav_path)

    assert detect_chord_change_times(wav_path, 0.0, 6.0, num_chords=0) == []


def _write_four_chord_wav(path: Path, sr: int = 22050, chord_seconds: float = 3.0):
    """Four distinct two-note 'chords' of chord_seconds each, back to back with
    no fades -- each abrupt join is a stand-in for a real chord change."""
    n = int(sr * chord_seconds)
    t = np.linspace(0, chord_seconds, n, endpoint=False)
    chords = [
        (220.00, 277.18),  # A3 + C#4
        (246.94, 311.13),  # B3 + D#4
        (261.63, 329.63),  # C4  + E4
        (293.66, 369.99),  # D4  + F#4
    ]
    segments = [
        0.3 * np.sin(2 * np.pi * f1 * t) + 0.3 * np.sin(2 * np.pi * f2 * t)
        for f1, f2 in chords
    ]
    y = np.concatenate(segments).astype(np.float32)
    sf.write(str(path), y, sr)


def test_detect_chord_change_times_spreads_boundaries_across_all_real_changes(tmp_path):
    """Regression test for the real-world bug: an abrupt join between two
    sustained tones smears across several consecutive CQT frames (window
    overlap), each showing elevated novelty. Naively picking the globally
    top-K novelty frames as boundaries lets several of those frames -- all
    describing the SAME single transition -- crowd out the boundaries for
    OTHER, genuinely separate chord changes elsewhere in the clip. Confirmed
    against real generated songs (a 111-second, 20-chord outro block
    collapsed to one 101-second span plus a cluster of sub-0.1-second spans
    around a single transition). With four evenly-spaced real chord changes
    (at 3s, 6s, 9s) requesting 4 chords, every boundary must land near its
    own real change, not have two or three of them crowd around one."""
    wav_path = tmp_path / "instrumental.wav"
    _write_four_chord_wav(wav_path)

    spans = detect_chord_change_times(wav_path, start_time=0.0, end_time=12.0, num_chords=4)

    assert len(spans) == 4
    boundaries = [end for _, end in spans[:-1]]
    expected = [3.0, 6.0, 9.0]
    for actual, target in zip(boundaries, expected):
        assert abs(actual - target) < 0.5, f"boundaries {boundaries} not near {expected}"


def _write_many_chord_wav(path: Path, sr: int = 22050, chord_seconds: float = 5.55, num_chords: int = 20):
    """num_chords distinct single-tone segments back to back, at the scale of
    a real long instrumental outro (e.g. 20 chords over ~111 seconds)."""
    n = int(sr * chord_seconds)
    t = np.linspace(0, chord_seconds, n, endpoint=False)
    freqs = [200 + 20 * i for i in range(num_chords)]  # deterministic, all distinct
    segments = [0.3 * np.sin(2 * np.pi * f * t) for f in freqs]
    y = np.concatenate(segments).astype(np.float32)
    sf.write(str(path), y, sr)


def test_detect_chord_change_times_does_not_plant_a_spurious_boundary_at_the_edges(tmp_path):
    """Regression test for a second, related real-world failure mode found
    while fixing the clustering bug above, at the scale of a real 20-chord
    outro: analyzing only the hard-cropped [start_time, end_time) slice makes
    the very first/last frame's novelty (silence-padding-edge vs. real
    content) look like a genuine harmonic change, even though it's just a
    window artifact at the boundary of the slice itself -- not a real
    INTERNAL chord change. Reproduced deterministically at this scale: one
    real internal boundary got dropped in favor of a spurious one 0.03s from
    an edge, doubling one chord's displayed duration to ~11s. No returned
    span may be a sliver (a spurious edge boundary) nor roughly double the
    expected ~5.55s (the dropped-boundary side effect)."""
    wav_path = tmp_path / "instrumental.wav"
    chord_seconds = 5.55
    num_chords = 20
    _write_many_chord_wav(wav_path, chord_seconds=chord_seconds, num_chords=num_chords)

    spans = detect_chord_change_times(
        wav_path, start_time=0.0, end_time=chord_seconds * num_chords, num_chords=num_chords
    )

    assert len(spans) == num_chords
    for start, end in spans:
        length = end - start
        assert length > chord_seconds * 0.5, f"span ({start}, {end}) is a sliver -- spurious edge boundary"
        assert length < chord_seconds * 1.5, f"span ({start}, {end}) is ~double -- a real boundary was dropped"


def test_detect_chord_change_times_falls_back_to_even_spacing_on_silence(tmp_path):
    wav_path = tmp_path / "silence.wav"
    sf.write(str(wav_path), np.zeros(22050 * 4, dtype=np.float32), 22050)

    spans = detect_chord_change_times(wav_path, start_time=0.0, end_time=4.0, num_chords=4)

    assert len(spans) == 4
    # even spacing: each span should be ~1.0s
    for start, end in spans:
        assert abs((end - start) - 1.0) < 0.05
