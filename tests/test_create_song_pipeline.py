"""A song Create a Song made (owner, 2026-10-31): its chords' N.C. over music is filled, and it waits for the owner to
watch it ("Wait for me") until Mark Verified or Upload Anyway."""

import json
from pathlib import Path

from lyricvideo.ai_generated import AI_SONG_FILE, WATCH_FIRST_REASON, mark_ai_generated
from lyricvideo.key_decision import KeyDecision, save_decision
from lyricvideo.models import ChordEvent, ChordTrack, Song, save_song
from lyricvideo.owner_verified import mark_verified
from lyricvideo.pipeline import (
    list_flagged_songs, list_pending_uploads, review_concern, run_pipeline, waits_to_be_watched,
)
from tests.test_pipeline import _patch_common


def _fills(monkeypatch):
    calls = []

    def fill(track, music_stem, full_mix, **kwargs):
        calls.append((Path(music_stem).name, Path(full_mix).name))
        return ChordTrack(events=[ChordEvent(0.0, 10.0, "C")], key=track.key, bpm=track.bpm)

    monkeypatch.setattr("lyricvideo.pipeline.fill_gaps_from_audio", fill)
    return calls


def test_only_our_own_ai_songs_get_their_no_chord_gaps_filled(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    calls = _fills(monkeypatch)

    run_pipeline(Path("audio.mp3"), tmp_path / "plain")
    assert calls == []

    (tmp_path / "ours").mkdir()
    mark_ai_generated(tmp_path / "ours")
    run_pipeline(Path("audio.mp3"), tmp_path / "ours")
    assert calls == [("no_vocals.wav", "audio.mp3")]


def _rendered(folder, created):
    folder.mkdir(parents=True)
    save_song(Song(title="New One", audio_path="a.mp3", chord_track=ChordTrack([ChordEvent(0.0, 5.0, "D")], "D major")),
              folder / "lyrics_timed.json")
    (folder / "new-one.mp4").write_bytes(b"video")
    save_decision(folder, KeyDecision(status="confirmed", key="D major", source="requested", chord_key="D major"))
    if created:
        (folder / AI_SONG_FILE).write_text(json.dumps({"key": "D major"}), encoding="utf-8")
    return folder


def test_a_created_song_waits_in_flagged_until_the_owner_approves_it(tmp_path):
    root = tmp_path / "work"
    _rendered(root / "other-song", created=False)
    song = _rendered(root / "new-one", created=True)

    assert list_pending_uploads(root) == ["other-song"]
    assert list_flagged_songs(root) == ["new-one"]
    assert review_concern(song) == WATCH_FIRST_REASON and waits_to_be_watched(song)

    mark_verified(song)

    assert list_pending_uploads(root) == ["new-one", "other-song"]
    assert list_flagged_songs(root) == [] and not waits_to_be_watched(song)
    assert not waits_to_be_watched(root / "other-song")
