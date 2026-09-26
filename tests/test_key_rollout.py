import json
from types import SimpleNamespace

from lyricvideo.key_decision import KEY_DECISION_FILE, load_decision, load_owner_key, save_owner_key
from lyricvideo.key_rollout import settle_saved_song
from lyricvideo.models import ChordEvent, ChordTrack, Song, load_song, save_song
from lyricvideo.pipeline import HELD_MARKER


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls, self.messages = reply, 0, self

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def make_song(tmp_path, stored_key, name="some-song", chords=(("D", 8), ("G", 4), ("A", 4), ("D", 8))):
    work_dir = tmp_path / name
    work_dir.mkdir()
    events, t = [], 0.0
    for label, secs in chords:
        events.append(ChordEvent(t, t + secs, label))
        t += secs
    save_song(Song(title="Some Song", audio_path="a.mp3", chord_track=ChordTrack(events=events, key=stored_key, bpm=100.0)),
              work_dir / "lyrics_timed.json")
    (work_dir / "song_info.json").write_text(json.dumps({"title": "Some Song", "artist": "Some Artist"}), encoding="utf-8")
    (work_dir / "some-song.mp4").write_bytes(b"video")
    return work_dir


def test_a_song_already_on_youtube_is_left_completely_alone(tmp_path):
    work_dir = make_song(tmp_path, "A major")
    (work_dir / "youtube_state.json").write_text("{}", encoding="utf-8")
    client = FakeClient("KEY: D major")

    result = settle_saved_song(work_dir, client, apply=True)

    assert result.action == "skipped-uploaded" and client.calls == 0
    assert not (work_dir / KEY_DECISION_FILE).exists() and load_song(work_dir / "lyrics_timed.json").chord_track.key == "A major"


def test_a_song_whose_key_is_already_right_only_gets_its_decision_recorded(tmp_path):
    work_dir = make_song(tmp_path, "D major")

    result = settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=True)

    assert result.action == "already-right" and load_decision(work_dir).confirmed
    assert (work_dir / "some-song.mp4").exists() and not (work_dir / HELD_MARKER).exists()


def test_a_wrong_key_is_corrected_and_its_video_is_set_aside_to_be_made_again(tmp_path):
    work_dir = make_song(tmp_path, "A major")
    easy = work_dir / "easychords"
    easy.mkdir()
    (easy / "some-song-easychords.mp4").write_bytes(b"easy video")

    result = settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=True)

    assert result.action == "corrected" and (result.old_key, result.new_key) == ("A major", "D major")
    assert load_song(work_dir / "lyrics_timed.json").chord_track.key == "D major"
    assert not (work_dir / "some-song.mp4").exists() and (work_dir / "some-song.previous.mp4").exists()
    assert not (easy / "some-song-easychords.mp4").exists() and (easy / "some-song-easychords.previous.mp4").exists()
    held = json.loads((work_dir / HELD_MARKER).read_text())["reason"]
    assert "A major" in held and "D major" in held
    assert load_decision(work_dir).confirmed


def test_a_song_whose_opinions_disagree_waits_for_the_owner_and_keeps_its_video(tmp_path):
    work_dir = make_song(tmp_path, "A major")

    result = settle_saved_song(work_dir, FakeClient("KEY: G major"), apply=True)

    assert result.action == "review" and not load_decision(work_dir).confirmed
    assert (work_dir / "some-song.mp4").exists() and not (work_dir / HELD_MARKER).exists()
    assert load_song(work_dir / "lyrics_timed.json").chord_track.key == "A major"


def test_the_owners_saved_key_is_used_without_asking_claude(tmp_path):
    work_dir = make_song(tmp_path, "A major")
    save_owner_key(work_dir, "G major")
    client = FakeClient("KEY: D major")

    result = settle_saved_song(work_dir, client, apply=True)

    assert result.action == "corrected" and result.new_key == "G major" and client.calls == 0
    assert load_owner_key(work_dir) == "G major"


def test_a_dry_run_changes_nothing_on_disk_but_reports_what_it_would_do(tmp_path):
    work_dir = make_song(tmp_path, "A major")

    result = settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=False)

    assert result.action == "corrected" and result.new_key == "D major"
    assert not (work_dir / KEY_DECISION_FILE).exists() and not (work_dir / HELD_MARKER).exists()
    assert (work_dir / "some-song.mp4").exists() and load_song(work_dir / "lyrics_timed.json").chord_track.key == "A major"


def test_a_song_with_no_chords_is_reported_not_guessed(tmp_path):
    work_dir = make_song(tmp_path, "A major", chords=())

    assert settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=True).action == "no-chords"
