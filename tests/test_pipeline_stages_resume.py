"""Resuming a held song (issue #7 review, wave 2): Render Anyway / Set Key after a Redo whose timing was held must never
make a video without chords (F006/F022); an EASY CHORD version is only ever rendered, never run through the pipeline
(EASY-GUARD); the owner's key is recorded even when it matches the saved chords; a song_info.json with no title is
identified again (NO-TITLE)."""

import json
from pathlib import Path

import pytest

from lyricvideo.chord_theory import save_easy_chord_capo_marker
from lyricvideo.key_decision import KeyDecision, load_decision, save_decision, save_owner_key
from lyricvideo.models import ChordEvent, ChordTrack, Song, load_song, save_song
from lyricvideo.pipeline import HELD_MARKER, HeldBeforeVideo, run_pipeline
from tests.test_pipeline import _failing_gate, _patch_common


def _passing_gate(monkeypatch):
    from lyricvideo.timing_gate import Settled, SyncReport

    monkeypatch.setattr(
        "lyricvideo.pipeline.settle_alignment",
        lambda candidates, line_words, heard, preferred=None, earlier_concern="", needed=None: Settled(
            preferred, candidates[preferred], SyncReport(1.0, 0, 0, 0), earlier_concern,
        ),
    )


def _capture_render(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.assemble_video",
        lambda lines, chord_track, *a, **k: seen.append({"chords": chord_track, "args": a, "kwargs": k}),
    )
    return seen


# --- F006 / F022: Render Anyway after a Redo held for its timing ---------------------------------------------------------

def test_render_anyway_after_a_held_redo_detects_the_chords_again_instead_of_rendering_none(tmp_path, monkeypatch):
    from lyricvideo.gui import _stage_to_resume

    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)                                    # the song's first, finished run
    assert load_decision(work_dir).confirmed and load_song(work_dir / "lyrics_timed.json").chord_track.events
    (work_dir / "test-song.mp4").write_bytes(b"first video")

    _failing_gate(monkeypatch)
    with pytest.raises(HeldBeforeVideo):
        run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics")    # the Redo: new timing, held
    assert load_song(work_dir / "lyrics_timed.json").chord_track.events == []   # re-timed with no chords...
    assert load_decision(work_dir).confirmed                                     # ...while the old decision stays

    assert _stage_to_resume(work_dir) == "detect_chords"

    _passing_gate(monkeypatch)
    detected = []
    real_detect = ChordTrack(events=[ChordEvent(0.0, 10.0, "C")], key="C major", bpm=100.0)
    monkeypatch.setattr("lyricvideo.pipeline.detect_chords", lambda path, **k: detected.append(1) or real_detect)
    seen = _capture_render(monkeypatch)
    run_pipeline(Path("audio.mp3"), work_dir, start_stage=_stage_to_resume(work_dir))

    assert detected == [1]
    assert seen[0]["chords"].events and seen[0]["chords"].key == "C major"
    assert not (work_dir / HELD_MARKER).exists()


def test_a_resume_at_images_whose_saved_song_has_no_chords_detects_them_first(tmp_path, monkeypatch):
    """The same hole through any resume past the chords (e.g. the CLI's --stage images): run_pipeline itself refuses to
    render a chordless song while an earlier run's confirmed key decision sits on disk."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "Test Song", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    save_song(Song(title="Test Song", audio_path="a.mp3"), work_dir / "lyrics_timed.json")
    save_decision(work_dir, KeyDecision(status="confirmed", key="G major", source="agreed", chord_key="G major"))
    monkeypatch.setattr("lyricvideo.pipeline.stems_look_complete", lambda *a, **k: True)
    seen = _capture_render(monkeypatch)
    reported = []

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images", progress_callback=reported.append)

    assert "detect_chords" in reported and "separate" not in reported
    assert seen[0]["chords"].events and seen[0]["chords"].key == "C major"
    assert load_decision(work_dir).key == "C major"                              # settled again from the real chords


def test_missing_stems_are_separated_again_before_the_chords_are_detected_on_such_a_resume(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "Test Song", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    save_song(Song(title="Test Song", audio_path="a.mp3"), work_dir / "lyrics_timed.json")
    reported = []

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="render", progress_callback=reported.append)

    assert reported[:2] == ["separate", "detect_chords"]


def test_stage_to_resume_follows_the_saved_chords_not_a_leftover_key_decision(tmp_path):
    from lyricvideo.gui import _stage_to_resume

    song = tmp_path / "some-song"
    song.mkdir()
    assert _stage_to_resume(song) == "detect_chords"                             # nothing readable saved
    save_song(Song(title="Some Song", audio_path="a.mp3"), song / "lyrics_timed.json")
    save_decision(song, KeyDecision(status="confirmed", key="G major", source="agreed", chord_key="G major"))
    assert _stage_to_resume(song) == "detect_chords"                             # a stale decision, no chords
    save_song(
        Song(title="Some Song", audio_path="a.mp3", chord_track=ChordTrack(events=[ChordEvent(0.0, 5.0, "G")], key="G major")),
        song / "lyrics_timed.json",
    )
    assert _stage_to_resume(song) == "images"                                    # held for its key: chords are saved
    (song / "some-song.mp4").write_bytes(b"video")
    assert _stage_to_resume(song) == "render"                                    # a video exists: only re-render it


# --- the owner's key is recorded even when it is the key the chords already had ------------------------------------------

def test_set_key_with_the_saved_chords_own_key_records_a_confirmed_decision(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "Test Song", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    chords = ChordTrack(events=[ChordEvent(0.0, 10.0, "C")], key="C major", bpm=100.0)
    save_song(Song(title="Test Song", audio_path="a.mp3", chord_track=chords), work_dir / "lyrics_timed.json")
    save_decision(work_dir, KeyDecision(status="review", chord_key="C major", published_key="A minor"))
    save_owner_key(work_dir, "C major")                                          # the owner agrees with the chords
    seen = _capture_render(monkeypatch)

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")

    decision = load_decision(work_dir)
    assert decision.confirmed and decision.source == "owner" and decision.key == "C major"
    assert seen[0]["chords"].key == "C major"


# --- EASY-GUARD ---------------------------------------------------------------------------------------------------------------

def _easy_variant(tmp_path, key="Eb major"):
    easy_dir = tmp_path / "work" / "some-song" / "easychords"
    easy_dir.mkdir(parents=True)
    (easy_dir / "song_info.json").write_text(
        json.dumps({"title": "Some Song EasyChords", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    save_song(
        Song(title="Some Song EasyChords", audio_path="a.mp3",
             chord_track=ChordTrack(events=[ChordEvent(0.0, 10.0, "D")], key=key, bpm=90.0)),
        easy_dir / "lyrics_timed.json",
    )
    save_easy_chord_capo_marker(easy_dir, capo_fret=1, shape_key="D major", original_key="Eb major", original_title="Some Song")
    return easy_dir


@pytest.mark.parametrize("stage", ["identify", "separate", "fetch_lyrics", "align", "detect_chords"])
def test_an_easy_chord_folder_is_never_run_through_the_pipeline(tmp_path, monkeypatch, stage):
    _patch_common(monkeypatch, tmp_path)
    easy_dir = _easy_variant(tmp_path)

    with pytest.raises(RuntimeError, match="EASY CHORD version"):
        run_pipeline(Path("audio.mp3"), easy_dir, start_stage=stage)

    assert load_song(easy_dir / "lyrics_timed.json").chord_track.key == "Eb major"     # untouched


def test_an_easy_chord_render_takes_its_capo_from_its_marker_and_ignores_any_key_or_easy_setting(tmp_path, monkeypatch):
    from lyricvideo.settings import Settings

    _patch_common(monkeypatch, tmp_path)
    easy_dir = _easy_variant(tmp_path, key="Eb major")
    save_owner_key(easy_dir, "A minor")                   # a key saved on the variant by an old Set Key: nothing may read it
    monkeypatch.setattr("lyricvideo.pipeline.build_capo_variant", lambda *a, **k: pytest.fail("no EASY version of an EASY version"))
    monkeypatch.setattr("lyricvideo.pipeline.apply_saved_owner_key", lambda *a, **k: pytest.fail("no owner key on a variant"))
    seen = _capture_render(monkeypatch)

    run_pipeline(
        Path("audio.mp3"), easy_dir, start_stage="render",
        settings=Settings(generate_easy_chord_versions=True),
    )

    assert seen[0]["kwargs"]["capo"] == 1 and seen[0]["kwargs"]["key_label"] == "Eb major"
    assert seen[0]["chords"].key == "Eb major" and [e.label for e in seen[0]["chords"].events] == ["D"]
    assert not (easy_dir / "key_decision.json").exists()


# --- NO-TITLE -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("info", [{"artist": "a", "duration": 10.0}, {"title": "  ", "artist": "a"}, "not json"])
def test_a_song_info_without_a_title_is_identified_again_on_resume(tmp_path, monkeypatch, info):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(info if isinstance(info, str) else json.dumps(info), encoding="utf-8")
    reported = []

    out = run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics", progress_callback=reported.append)

    assert reported[0] == "identify" and out.name == "test-song.mp4"
    assert json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))["title"] == "Test Song"
