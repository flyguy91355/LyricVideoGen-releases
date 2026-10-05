import json
from pathlib import Path

import pytest

from lyricvideo import pipeline
from lyricvideo.banned import BANNED_FILE, SongBanned, is_banned, mark_banned
from lyricvideo.youtube_state import song_dirs, uploaded_song_dirs


def make_song(root: Path, slug: str, uploaded: bool = True, easy: bool = True) -> Path:
    folder = root / slug
    folder.mkdir(parents=True)
    (folder / "lyrics_timed.json").write_text("{}", encoding="utf-8")
    if uploaded:
        (folder / "youtube_state.json").write_text(json.dumps({"video_id": "vid-" + slug, "uploaded_at": "x", "title": "t"}), encoding="utf-8")
    if easy:
        (folder / "easychords").mkdir()
        (folder / "easychords" / "lyrics_timed.json").write_text("{}", encoding="utf-8")
        if uploaded:
            (folder / "easychords" / "youtube_state.json").write_text(json.dumps({"video_id": "easy-" + slug, "uploaded_at": "x", "title": "t"}), encoding="utf-8")
    return folder


def test_marking_writes_a_readable_tag_and_is_banned_sees_it(tmp_path):
    folder = make_song(tmp_path, "dylan")
    assert not is_banned(folder)
    tag = mark_banned(folder, "YouTube banned this video")
    assert tag.name == BANNED_FILE and "banned" in tag.read_text().lower() and "Delete this file" in tag.read_text()
    assert is_banned(folder)
    assert is_banned(folder / "easychords")                 # the EASY version follows its song
    assert not is_banned(tmp_path / "other")


def test_a_banned_song_and_its_easy_version_vanish_from_every_walk(tmp_path):
    make_song(tmp_path, "dylan")
    make_song(tmp_path, "hendrix")
    mark_banned(tmp_path / "dylan")
    names = [n for n, _ in song_dirs(tmp_path)]
    assert names == ["hendrix", "hendrix/easychords"]
    assert [n for n, _p, _s in uploaded_song_dirs(tmp_path)] == ["hendrix", "hendrix/easychords"]
    assert [n for n, _ in pipeline._candidate_song_dirs(tmp_path)] == ["hendrix", "hendrix/easychords"]
    assert pipeline.list_redoable_songs(tmp_path) == ["hendrix"]


def test_removing_the_tag_brings_the_song_back(tmp_path):
    make_song(tmp_path, "dylan")
    mark_banned(tmp_path / "dylan")
    (tmp_path / "dylan" / BANNED_FILE).unlink()
    assert [n for n, _ in song_dirs(tmp_path)] == ["dylan", "dylan/easychords"]


def test_nothing_is_deleted_by_tagging(tmp_path):
    folder = make_song(tmp_path, "dylan")
    (folder / "dylan.mp4").write_bytes(b"video")
    mark_banned(folder)
    assert (folder / "dylan.mp4").read_bytes() == b"video" and (folder / "youtube_state.json").exists()


def test_schedule_upload_refuses_a_banned_song(tmp_path):
    from types import SimpleNamespace
    from lyricvideo.youtube_schedule import schedule_upload
    folder = make_song(tmp_path, "dylan", uploaded=False, easy=False)
    mark_banned(folder)
    with pytest.raises(SongBanned):
        schedule_upload(object(), object(), folder, SimpleNamespace(youtube_privacy="unlisted"))
