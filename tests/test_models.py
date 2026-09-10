import json
from pathlib import Path

from lyricvideo.models import (
    ChordEvent,
    ChordTrack,
    LyricLine,
    Song,
    Word,
    current_chord_at,
    line_hash,
    load_song,
    next_chord_after,
    save_song,
)


def test_lyric_line_text_joins_words():
    line = LyricLine(words=[Word(word="hello"), Word(word="there")])
    assert line.text == "hello there"


def test_save_and_load_song_round_trip(tmp_path):
    song = Song(
        title="Test Song",
        audio_path="audio.mp3",
        vocal_stem_path="vocals.wav",
        lines=[
            LyricLine(
                words=[Word(word="hi", start_time=0.0, end_time=0.5)],
                start_time=0.0,
                end_time=0.5,
            )
        ],
        instrumental_stem_path="no_vocals.wav",
        chord_track=ChordTrack(events=[ChordEvent(start=0.0, end=2.5, label="Em7")], key="E minor", bpm=90.0),
        image_cache={"abc123": "images/abc123.png"},
    )
    path = tmp_path / "song.json"

    save_song(song, path)
    restored = load_song(path)

    assert restored == song


def test_save_and_load_song_with_no_chord_track_defaults_empty(tmp_path):
    song = Song(title="No Chords Yet", audio_path="audio.mp3")
    path = tmp_path / "song.json"

    save_song(song, path)
    restored = load_song(path)

    assert restored.chord_track == ChordTrack()


def test_load_song_tolerates_legacy_chord_key_on_words(tmp_path):
    """Real bug found live 2026-09-09: songs saved by the pre-merge pipeline have a
    "chord" key on every word dict (from the old ChordWord model) and an
    "instrumental_chords" list instead of "chord_track" -- Redo on any pre-existing
    song must not crash just because the file predates this merge."""
    legacy_json = {
        "title": "Old Song",
        "audio_path": "/some/old/path.mp3",
        "vocal_stem_path": None,
        "instrumental_stem_path": None,
        "lines": [
            {
                "words": [
                    {"word": "hello", "chord": "G", "start_time": 0.0, "end_time": 0.5},
                    {"word": "there", "chord": None, "start_time": 0.5, "end_time": 1.0},
                ],
                "start_time": 0.0,
                "end_time": 1.0,
            }
        ],
        "instrumental_chords": [{"chord": "Em7", "start_time": 1.0, "end_time": 2.0}],
        "image_cache": {},
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy_json), encoding="utf-8")

    song = load_song(path)

    assert song.title == "Old Song"
    assert song.lines[0].words[0].word == "hello"
    assert song.lines[0].words[0].start_time == 0.0
    assert not hasattr(song.lines[0].words[0], "chord")
    assert song.chord_track == ChordTrack()  # legacy instrumental_chords is discarded, not migrated


def test_line_hash_stable_and_case_insensitive():
    assert line_hash("Hello There") == line_hash("hello there")
    assert line_hash("Hello There") != line_hash("Something else")


def test_current_chord_at_finds_covering_event():
    track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 5.0, "G")])
    assert current_chord_at(track, 1.0).label == "C"
    assert current_chord_at(track, 2.5).label == "G"


def test_current_chord_at_returns_none_outside_track():
    track = ChordTrack(events=[ChordEvent(1.0, 2.0, "C")])
    assert current_chord_at(track, 0.5) is None
    assert current_chord_at(track, 2.5) is None


def test_current_chord_at_empty_track_returns_none():
    assert current_chord_at(ChordTrack(), 1.0) is None


def test_next_chord_after_finds_next_different_label():
    track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "C"), ChordEvent(4.0, 6.0, "G")])
    # A merged-boundary duplicate "C" segment must not be reported as "next".
    result = next_chord_after(track, 0.5)
    assert result.label == "G"
    assert result.start == 4.0


def test_next_chord_after_returns_none_at_end_of_track():
    track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")])
    assert next_chord_after(track, 1.0) is None
