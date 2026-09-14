import subprocess

from lyricvideo.audio_decode import find_ffmpeg
from lyricvideo.detect_chords import (
    ChordEvent, ChordTrack, _parse_crema_label, _simplify_chord_label, detect_chords,
)


def _make_test_tone(tmp_path, freq=220, duration=6):
    """A simple, single-pitch tone -- not a real chord, but enough to exercise
    the full pipeline (crema analysis -> label simplification -> merge) without
    needing a real song file."""
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


def test_parse_crema_label_plain_triad():
    assert _parse_crema_label("C:maj") == (0, "maj")
    assert _parse_crema_label("A:min") == (9, "min")


def test_parse_crema_label_strips_slash_inversion():
    assert _parse_crema_label("C:maj/5") == (0, "maj")


def test_parse_crema_label_accepts_sharp_and_flat_roots():
    assert _parse_crema_label("D#:min7") == (3, "min7")
    assert _parse_crema_label("Eb:min7") == (3, "min7")


def test_parse_crema_label_no_chord_and_unknown_return_none():
    assert _parse_crema_label("N") is None
    assert _parse_crema_label("X") is None


def test_parse_crema_label_bare_root_defaults_to_major():
    # crema/mir_eval convention: a root with no ":quality" suffix is a major triad.
    assert _parse_crema_label("G") == (7, "maj")


def test_simplify_chord_label_no_chord_becomes_n():
    assert _simplify_chord_label("N", include_seventh_chords=True, use_flats=False) == "N"
    assert _simplify_chord_label("X", include_seventh_chords=True, use_flats=False) == "N"


def test_simplify_chord_label_plain_triads_unaffected_by_sevenths_flag():
    assert _simplify_chord_label("C:maj", include_seventh_chords=False, use_flats=False) == "C"
    assert _simplify_chord_label("C:min", include_seventh_chords=False, use_flats=False) == "Cm"


def test_simplify_chord_label_keeps_sevenths_when_enabled():
    assert _simplify_chord_label("G:7", include_seventh_chords=True, use_flats=False) == "G7"
    assert _simplify_chord_label("D:min7", include_seventh_chords=True, use_flats=False) == "Dm7"
    assert _simplify_chord_label("A:maj7", include_seventh_chords=True, use_flats=False) == "Amaj7"


def test_simplify_chord_label_collapses_sevenths_to_triads_when_disabled():
    assert _simplify_chord_label("G:7", include_seventh_chords=False, use_flats=False) == "G"
    assert _simplify_chord_label("D:min7", include_seventh_chords=False, use_flats=False) == "Dm"
    assert _simplify_chord_label("A:maj7", include_seventh_chords=False, use_flats=False) == "A"


def test_simplify_chord_label_half_diminished_collapses_to_min7():
    """A half-diminished 7th has a minor third and minor seventh -- closer to
    min7 than a plain minor triad."""
    assert _simplify_chord_label("F:hdim7", include_seventh_chords=True, use_flats=False) == "Fm7"


def test_simplify_chord_label_diminished_collapses_to_minor():
    assert _simplify_chord_label("A:dim", include_seventh_chords=True, use_flats=False) == "Am"


def test_simplify_chord_label_augmented_and_sus_default_to_major():
    assert _simplify_chord_label("C:aug", include_seventh_chords=True, use_flats=False) == "C"
    assert _simplify_chord_label("C:sus4", include_seventh_chords=True, use_flats=False) == "C"
    assert _simplify_chord_label("C:sus2", include_seventh_chords=True, use_flats=False) == "C"


def test_simplify_chord_label_extended_tensions_drop_to_base_seventh_quality():
    assert _simplify_chord_label("C:9", include_seventh_chords=True, use_flats=False) == "C7"
    assert _simplify_chord_label("C:maj9", include_seventh_chords=True, use_flats=False) == "Cmaj7"
    assert _simplify_chord_label("C:min9", include_seventh_chords=True, use_flats=False) == "Cm7"


def test_simplify_chord_label_respects_use_flats():
    assert _simplify_chord_label("D#:maj", include_seventh_chords=False, use_flats=True) == "Eb"
    assert _simplify_chord_label("D#:maj", include_seventh_chords=False, use_flats=False) == "D#"


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
    # This only asserts the flag is actually wired through (no crash, real
    # ChordTrack back either way) -- exact chord-guessing accuracy on a
    # synthetic single-pitch tone is not what's being tested here.
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


def test_simplify_chord_label_minor_major_seventh_keeps_its_minor_third():
    """crema's real vocabulary (pumpp's '3567s' set) includes minmaj7; it was
    missing from the quality map, so "A:minmaj7" silently became plain A
    major -- the wrong third (found by code review, 2026-09-14)."""
    assert _simplify_chord_label("A:minmaj7", include_seventh_chords=False, use_flats=False) == "Am"
    assert _simplify_chord_label("A:minmaj7", include_seventh_chords=True, use_flats=False) == "Am7"


def test_simplify_chord_label_maps_every_quality_crema_can_emit_explicitly():
    """pumpp's QUALITIES table is the authority on what crema outputs; every
    entry must be mapped explicitly rather than through the .get() default
    ('maj'), which is wrong for anything built on a minor third."""
    from lyricvideo.detect_chords import _QUALITY_TO_TRIAD_OR_SEVENTH

    crema_qualities = {
        "maj", "min", "dim", "aug", "min7", "maj7", "7", "dim7", "hdim7", "minmaj7",
        "min6", "maj6", "sus2", "sus4",
    }

    assert crema_qualities <= set(_QUALITY_TO_TRIAD_OR_SEVENTH)
