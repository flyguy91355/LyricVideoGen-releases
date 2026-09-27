"""detect_chords() now spells its labels with sharps and leaves the key blank (issue #7 review: its whole-song
HPSS + CQT key estimate cost ~12 s a song and was always replaced). These pin that the key check that follows it
(key_decision.settle_song_key) respells every label to the settled key, so the provisional sharp spelling never
reaches a video."""

from lyricvideo.key_decision import save_owner_key, settle_song_key
from lyricvideo.models import ChordEvent, ChordTrack


def _sharp_spelled_flat_key_track() -> ChordTrack:
    # I-IV-V-vi-ii in Bb major, spelled the way detect_chords() now hands them over (sharps, key "")
    labels = ["A#", "D#", "F", "Gm", "Cm", "A#", "D#", "F", "A#"]
    events = [ChordEvent(float(i * 2), float(i * 2 + 2), label) for i, label in enumerate(labels)]
    return ChordTrack(events=events, key="", bpm=100.0)


def test_a_track_still_in_review_comes_back_in_its_estimated_keys_spelling(tmp_path):
    decision, track = settle_song_key(tmp_path, _sharp_spelled_flat_key_track(), "Some Song", "Some Band", None)

    assert not decision.confirmed                       # no second opinion here, so it waits for the owner
    labels = {e.label for e in track.events}
    assert "Bb" in labels and "Eb" in labels
    assert not any("#" in label for label in labels)
    assert track.key


def test_the_owners_key_respells_the_sharp_labels(tmp_path):
    save_owner_key(tmp_path, "Bb major")

    decision, track = settle_song_key(tmp_path, _sharp_spelled_flat_key_track(), "Some Song", "Some Band", None)

    assert decision.confirmed and track.key == "Bb major"
    assert [e.label for e in track.events][:2] == ["Bb", "Eb"]
