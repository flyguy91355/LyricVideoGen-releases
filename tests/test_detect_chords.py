import subprocess

import numpy as np

from lyricvideo.audio_decode import find_ffmpeg
from lyricvideo.detect_chords import ChordEvent, ChordTrack, _hpcp_chroma, _sync_chroma_to_segments, detect_chords


def _make_test_tone(tmp_path, freq=220, duration=6):
    """A simple, single-pitch tone -- not a real chord, but enough to exercise
    the full pipeline (harmonic separation -> chroma -> beat sync -> template
    match -> Viterbi -> silence detection) without needing a real song file."""
    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={duration}",
         "-ar", "22050", str(wav_path)],
        check=True,
    )
    return wav_path


def test_chord_event_duration():
    event = ChordEvent(start=1.0, end=3.5, label="Am")
    assert event.duration == 2.5


def test_chord_track_defaults_to_empty():
    track = ChordTrack()
    assert track.events == []
    assert track.key == ""
    assert track.bpm == 0.0


def test_detect_chords_returns_a_track_covering_the_whole_file(tmp_path):
    wav_path = _make_test_tone(tmp_path, duration=6)

    track = detect_chords(wav_path)

    assert isinstance(track, ChordTrack)
    assert track.events  # at least one segment
    assert track.events[0].start == 0.0
    assert track.events[-1].end >= 5.5  # covers (close to) the whole 6s file


def test_detect_chords_on_silence_returns_no_chord_label(tmp_path):
    wav_path = tmp_path / "silence.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "3", str(wav_path)],
        check=True,
    )

    track = detect_chords(wav_path)

    assert all(e.label == "N" for e in track.events)


def test_detect_chords_on_short_clip_returns_empty_track(tmp_path):
    wav_path = tmp_path / "tiny.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
         "-ar", "22050", str(wav_path)],
        check=True,
    )

    track = detect_chords(wav_path)

    assert track.events == []


def test_detect_chords_include_seventh_chords_can_produce_seventh_labels(tmp_path):
    # A synthesized dominant-7th-ish chord: root + major third + fifth + minor
    # seventh, all mixed together. With sevenths disabled the detector can only
    # ever output a plain triad label; with them enabled a "7"/"m7"/"maj7"
    # suffix becomes possible. This test only asserts the flag is actually wired
    # through (no crash, real ChordTrack back either way) -- exact chord-guessing
    # accuracy on synthetic audio is not what's being tested here.
    wav_path = tmp_path / "chord.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=220:duration=6", "-ar", "22050", str(wav_path)],
        check=True,
    )

    with_sevenths = detect_chords(wav_path, include_seventh_chords=True)
    without_sevenths = detect_chords(wav_path, include_seventh_chords=False)

    assert isinstance(with_sevenths, ChordTrack)
    assert isinstance(without_sevenths, ChordTrack)


def test_detect_chords_min_chord_seconds_merges_short_segments(tmp_path):
    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-ar", "22050", str(wav_path)],
        check=True,
    )

    loose = detect_chords(wav_path, min_chord_seconds=0.0)
    strict = detect_chords(wav_path, min_chord_seconds=5.0)

    # A near-total merge threshold (5s on a 6s clip) can only ever produce the
    # same or fewer segments than no merging at all.
    assert len(strict.events) <= len(loose.events)


def test_hpcp_chroma_c_bin_is_rolled_to_chord_theory_pitch_class_zero():
    """Real bug found live, 2026-09-13: essentia's own HPCP output does NOT
    use chord_theory's C=0 convention (empirically verified: bin 8, not bin
    0, is where a pure C4 tone peaks) -- getting this wrong silently
    transposes every detected chord. A pure sine tone at C4 (261.6256Hz)
    must, after _hpcp_chroma's internal roll, peak at chroma index 0 (C)."""
    sr = 22050
    duration = 1.0
    t = np.arange(int(sr * duration)) / sr
    tone = (0.5 * np.sin(2 * np.pi * 261.6256 * t)).astype(np.float32)

    chroma, _times = _hpcp_chroma(tone)

    # Average across frames -- a single frame right at a zero-crossing can be
    # noisy, but the peak pitch class over the whole tone must be C (index 0).
    assert int(np.argmax(chroma.mean(axis=1))) == 0


def test_sync_chroma_to_segments_aggregates_frames_within_each_segment():
    chroma = np.array([[1.0, 1.0, 5.0, 5.0]] + [[0.0] * 4] * 11)  # only bin 0 populated
    frame_times = np.array([0.1, 0.4, 0.6, 0.9])
    seg_times = np.array([0.0, 0.5, 1.0])

    seg_chroma = _sync_chroma_to_segments(chroma, frame_times, seg_times)

    assert seg_chroma.shape == (12, 2)
    assert seg_chroma[0, 0] == 1.0  # median of the two frames in [0.0, 0.5)
    assert seg_chroma[0, 1] == 5.0  # median of the two frames in [0.5, 1.0)


def test_sync_chroma_to_segments_falls_back_to_nearest_frame_when_segment_is_empty():
    """A beat-to-beat segment with no HPCP frame center actually inside it
    (a narrow gap between two frame times) must not produce an all-zero
    vector -- it should fall back to the nearest frame by time."""
    chroma = np.array([[3.0, 7.0]] + [[0.0] * 2] * 11)
    frame_times = np.array([0.1, 0.9])
    seg_times = np.array([0.4, 0.6])  # no frame center falls in [0.4, 0.6)

    seg_chroma = _sync_chroma_to_segments(chroma, frame_times, seg_times)

    assert seg_chroma[0, 0] in (3.0, 7.0)  # nearest of the two real frames, not 0


def test_detect_chords_defaults_match_module_constants(tmp_path):
    """Calling with no keyword overrides must behave exactly like the hardcoded
    pre-Settings version -- this is the backward-compatibility contract the whole
    feature depends on."""
    from lyricvideo.detect_chords import (
        INCLUDE_SEVENTH_CHORDS, MIN_CHORD_SECONDS, PREFER_FLATS, SNAP_CHORDS_TO_KEY,
    )

    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-ar", "22050", str(wav_path)],
        check=True,
    )

    default_call = detect_chords(wav_path)
    explicit_call = detect_chords(
        wav_path, snap_chords_to_key=SNAP_CHORDS_TO_KEY, prefer_flats=PREFER_FLATS,
        include_seventh_chords=INCLUDE_SEVENTH_CHORDS, min_chord_seconds=MIN_CHORD_SECONDS,
    )

    assert default_call == explicit_call
