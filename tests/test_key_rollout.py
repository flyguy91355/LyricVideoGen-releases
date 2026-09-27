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
    # the EASY CHORD version was made for the old key: its whole folder is moved aside (issue #7 review, F124)
    assert not easy.exists()
    [prior] = [p for p in work_dir.iterdir() if p.name.startswith("easychords_prior_")]
    assert (prior / "some-song-easychords.mp4").exists()
    held = json.loads((work_dir / HELD_MARKER).read_text())["reason"]
    assert "A major" in held and "D major" in held and "EASY CHORD" in held
    assert "easychords_prior_" in result.detail
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


# --- issue #7 review: a re-run never re-asks or demotes a settled song (F055); a key change sets the EASY folder aside (F124)

def test_a_rerun_never_reasks_or_demotes_a_confirmed_song(tmp_path):
    work_dir = make_song(tmp_path, "D major")
    settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=True)
    assert load_decision(work_dir).confirmed

    again = FakeClient("KEY: G major")                  # a fresh second opinion that would now disagree
    result = settle_saved_song(work_dir, again, apply=True)

    assert result.action == "already-settled" and again.calls == 0
    assert load_decision(work_dir).confirmed and load_decision(work_dir).key == "D major"


def test_a_song_the_pipeline_already_settled_is_left_alone(tmp_path):
    from lyricvideo.key_decision import KeyDecision, save_decision
    work_dir = make_song(tmp_path, "D major")
    save_decision(work_dir, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major",
                                        published_key="D major", at="earlier"))
    client = FakeClient("KEY: G major")

    result = settle_saved_song(work_dir, client, apply=True)

    assert result.action == "already-settled" and client.calls == 0
    assert load_decision(work_dir).at == "earlier"                     # not rewritten
    assert (work_dir / "some-song.mp4").exists() and not (work_dir / HELD_MARKER).exists()


def test_a_confirmed_key_the_saved_chords_do_not_carry_is_corrected_without_asking(tmp_path):
    from lyricvideo.key_decision import KeyDecision, save_decision
    work_dir = make_song(tmp_path, "A major")
    save_decision(work_dir, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major"))
    client = FakeClient("KEY: G major")

    result = settle_saved_song(work_dir, client, apply=True)

    assert result.action == "corrected" and result.new_key == "D major" and client.calls == 0
    assert load_song(work_dir / "lyrics_timed.json").chord_track.key == "D major"
    assert (work_dir / HELD_MARKER).exists()


def test_a_song_still_in_review_is_asked_again(tmp_path):
    from lyricvideo.key_decision import KeyDecision, save_decision
    work_dir = make_song(tmp_path, "D major")
    save_decision(work_dir, KeyDecision(status="review", chord_key="D major", published_key="G major"))
    client = FakeClient("KEY: D major")

    result = settle_saved_song(work_dir, client, apply=True)

    assert client.calls == 1 and result.action == "already-right" and load_decision(work_dir).confirmed


def test_a_newer_owner_key_is_used_over_an_older_agreed_decision(tmp_path):
    from lyricvideo.key_decision import KeyDecision, save_decision
    work_dir = make_song(tmp_path, "D major")
    save_decision(work_dir, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major"))
    save_owner_key(work_dir, "B minor")
    client = FakeClient("KEY: D major")

    result = settle_saved_song(work_dir, client, apply=True)

    assert result.action == "corrected" and result.new_key == "B minor" and client.calls == 0
    assert load_decision(work_dir).source == "owner"


def test_a_corrected_hard_key_song_is_offered_for_a_new_easy_version(tmp_path, monkeypatch):
    import lyricvideo.pipeline as pipeline
    work_dir = make_song(tmp_path, "Eb minor", chords=(("C#", 8), ("F#", 4), ("G#", 4), ("C#", 8)))
    easy = work_dir / "easychords"
    easy.mkdir()
    (easy / "easy_chord_capo.json").write_text(json.dumps({"capo_fret": 1, "shape_key": "Dm", "original_key": "Eb minor",
                                                           "original_title": "Some Song"}), encoding="utf-8")
    (easy / "some-song-easychords.mp4").write_bytes(b"easy video")

    result = settle_saved_song(work_dir, FakeClient("KEY: Db major"), apply=True)

    assert result.action == "corrected" and result.new_key == "Db major"
    assert not easy.exists()
    (work_dir / "some-song.mp3").write_bytes(b"audio")                                    # the song's own audio copy
    monkeypatch.setattr(pipeline, "list_uploadable_songs", lambda root: ["some-song"])     # once its video is made again
    assert pipeline.list_easy_chord_backfill_candidates(tmp_path) == ["some-song"]


def test_an_easy_version_already_on_youtube_is_left_alone(tmp_path):
    work_dir = make_song(tmp_path, "A major")
    easy = work_dir / "easychords"
    easy.mkdir()
    (easy / "some-song-easychords.mp4").write_bytes(b"easy video")
    (easy / "youtube_state.json").write_text("{}", encoding="utf-8")

    result = settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=True)

    assert result.action == "corrected" and "YouTube" in result.detail
    assert (easy / "some-song-easychords.mp4").exists() and (easy / "youtube_state.json").exists()


def test_a_dry_run_of_a_correction_moves_no_easy_folder(tmp_path):
    work_dir = make_song(tmp_path, "A major")
    (work_dir / "easychords").mkdir()

    result = settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=False)

    assert result.action == "corrected" and (work_dir / "easychords").is_dir()


def test_an_easy_folder_that_cannot_be_moved_still_leaves_the_song_held(tmp_path, monkeypatch):
    from pathlib import Path
    work_dir = make_song(tmp_path, "A major")
    easy = work_dir / "easychords"
    easy.mkdir()
    real_rename = Path.rename

    def rename(self, target):
        if self.name == "easychords":
            raise PermissionError("in use")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)

    result = settle_saved_song(work_dir, FakeClient("KEY: D major"), apply=True)

    assert result.action == "corrected" and "could not be moved" in result.detail
    assert easy.is_dir() and (work_dir / HELD_MARKER).exists()
