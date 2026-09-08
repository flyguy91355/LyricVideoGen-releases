import json
from pathlib import Path

from lyricvideo.models import (
    ChordWord,
    InstrumentalChord,
    LyricLine,
    Song,
    save_song,
    load_song,
    line_hash,
)


def test_lyric_line_text_joins_words():
    line = LyricLine(words=[ChordWord(word="hello"), ChordWord(word="there")])
    assert line.text == "hello there"


def test_save_and_load_song_round_trip(tmp_path):
    song = Song(
        title="Test Song",
        audio_path="audio.mp3",
        vocal_stem_path="vocals.wav",
        lines=[
            LyricLine(
                words=[ChordWord(word="hi", chord="G", start_time=0.0, end_time=0.5)],
                start_time=0.0,
                end_time=0.5,
            )
        ],
        instrumental_stem_path="no_vocals.wav",
        instrumental_chords=[InstrumentalChord(chord="Em7", start_time=0.0, end_time=2.5)],
        image_cache={"abc123": "images/abc123.png"},
    )
    path = tmp_path / "song.json"

    save_song(song, path)
    restored = load_song(path)

    assert restored == song


def test_line_hash_stable_and_case_insensitive():
    assert line_hash("Hello There") == line_hash("hello there")
    assert line_hash("Hello There") != line_hash("Something else")
