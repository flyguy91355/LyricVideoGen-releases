"""owner_lyrics: the owner's own edited lyrics for a song set aside for review. Saved as work/<song>/lyrics_owner.txt
and used by the next Redo instead of any online source -- the owner's word is final (no AI or audio check overrides
it); the aligner and the sync check still time it."""

import json

import pytest

from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.owner_lyrics import OWNER_LYRICS_FILE, load_editable_lyrics, owner_lyrics_lines, save_owner_lyrics


def make_song(work_dir, lines, concern="Only 50% match."):
    work_dir.mkdir(parents=True, exist_ok=True)
    save_song(
        Song(title="T", audio_path="a.mp3", lines=[LyricLine(words=[Word(word=w) for w in line.split()]) for line in lines],
             lyrics_accuracy_concern=concern),
        work_dir / "lyrics_timed.json",
    )


def test_the_editor_starts_from_the_lyrics_the_video_currently_shows(tmp_path):
    make_song(tmp_path / "s", ["first line here", "second line here"])

    assert load_editable_lyrics(tmp_path / "s") == "first line here\nsecond line here"


def test_the_editor_starts_from_the_owners_saved_edit_when_there_is_one(tmp_path):
    make_song(tmp_path / "s", ["old line"])
    save_owner_lyrics(tmp_path / "s", "my corrected line\nanother line")

    assert load_editable_lyrics(tmp_path / "s") == "my corrected line\nanother line"


def test_the_editor_falls_back_to_the_fetched_lyrics_when_no_song_was_timed_yet(tmp_path):
    work_dir = tmp_path / "s"
    work_dir.mkdir()
    (work_dir / "lyric_lines.json").write_text(json.dumps({"lines": ["a", "b"], "source": "lrclib", "concern": ""}))

    assert load_editable_lyrics(work_dir) == "a\nb"
    assert load_editable_lyrics(tmp_path / "missing") == ""


def test_saving_writes_one_lyric_per_line_ignoring_blank_lines_and_stray_spaces(tmp_path):
    lines = save_owner_lyrics(tmp_path, "  first line  \n\n\nsecond line\t\n   \nthird line")

    assert lines == ["first line", "second line", "third line"]
    assert (tmp_path / OWNER_LYRICS_FILE).read_text(encoding="utf-8") == "first line\nsecond line\nthird line\n"


def test_saving_nothing_is_refused_so_the_owner_cannot_wipe_a_songs_lyrics_by_accident(tmp_path):
    with pytest.raises(ValueError):
        save_owner_lyrics(tmp_path, "   \n\n  ")

    assert not (tmp_path / OWNER_LYRICS_FILE).exists()


def test_owner_lyrics_lines_reads_back_what_was_saved_or_none(tmp_path):
    assert owner_lyrics_lines(tmp_path) is None
    save_owner_lyrics(tmp_path, "one\ntwo")
    assert owner_lyrics_lines(tmp_path) == ["one", "two"]


def test_an_unreadable_or_empty_owner_file_is_treated_as_absent(tmp_path):
    (tmp_path / OWNER_LYRICS_FILE).write_text("   \n", encoding="utf-8")

    assert owner_lyrics_lines(tmp_path) is None
