"""SETKEY-PARENT (issue #7 review, wave 2): an EASY CHORD version waiting only on its already-uploaded song's key must
bring that SONG into Flagged for Lyrics Review, where Set Key lives."""

import json

from lyricvideo.key_decision import KeyDecision, save_decision
from lyricvideo.models import ChordEvent, ChordTrack, Song, save_song
from lyricvideo.pipeline import easy_version_waits_on_key, list_flagged_songs, list_pending_uploads


def _song(folder, title, uploaded=False, video=True):
    folder.mkdir(parents=True)
    save_song(
        Song(title=title, audio_path="a.mp3", chord_track=ChordTrack(events=[ChordEvent(0.0, 5.0, "D")], key="D major")),
        folder / "lyrics_timed.json",
    )
    if video:
        (folder / f"{title.lower().replace(' ', '-')}.mp4").write_bytes(b"video")
    if uploaded:
        (folder / "youtube_state.json").write_text(json.dumps({"video_id": "abc"}), encoding="utf-8")
    return folder


def test_an_uploaded_song_whose_easy_version_waits_for_its_key_is_listed_for_set_key(tmp_path):
    root = tmp_path / "work"
    parent = _song(root / "some-song", "Some Song", uploaded=True)
    _song(parent / "easychords", "Some Song EasyChords")

    assert easy_version_waits_on_key(parent)
    assert list_flagged_songs(root, include_uploaded=True) == ["some-song", "some-song/easychords"]
    assert "some-song" in list_flagged_songs(root)
    assert list_pending_uploads(root) == []

    save_decision(parent, KeyDecision(status="confirmed", key="D major", source="owner", chord_key="D major"))

    assert not easy_version_waits_on_key(parent)
    assert "some-song" not in list_flagged_songs(root, include_uploaded=True)


def test_an_uploaded_song_whose_easy_version_is_uploaded_too_is_left_alone(tmp_path):
    root = tmp_path / "work"
    parent = _song(root / "some-song", "Some Song", uploaded=True)
    _song(parent / "easychords", "Some Song EasyChords", uploaded=True)

    assert not easy_version_waits_on_key(parent)
    assert list_flagged_songs(root, include_uploaded=True) == []


def test_an_uploaded_song_with_no_easy_version_is_left_alone(tmp_path):
    root = tmp_path / "work"
    _song(root / "some-song", "Some Song", uploaded=True)

    assert list_flagged_songs(root, include_uploaded=True) == []
