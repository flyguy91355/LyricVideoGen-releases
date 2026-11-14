"""schedule_upload checks every picture of a video before it goes out (owner, 2026-11-13: Quarters and Dimes, made
before the picture check, had a cut-off hand on a truck fender). A bad one is replaced and the video made again right
then, and the new video is checked and uploaded ("its replaced and then it rendered anyways, without me doing it.. then
i will review after"); a remake that fails leaves the song held; an unchecked picture makes the upload wait."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from lyricvideo.models import line_hash
from lyricvideo.pipeline import BAD_PICTURES_PREFIX, HELD_MARKER, list_pending_uploads
from lyricvideo.youtube_schedule import BadPictures, PicturesNotChecked, schedule_upload
from tests.test_youtube_schedule import _FakeAnthropicClient, _FakeYoutubeClient, _make_song_work_dir

BAD = (200, 0, 0)


@pytest.fixture(autouse=True)
def _whole_videos(monkeypatch):
    monkeypatch.setattr("lyricvideo.youtube_schedule.rendered_stream_seconds", lambda path: (200.0, 200.0))


class _NoCalls:
    """Neither Claude nor YouTube may be reached when the pictures stop an upload."""

    def __getattr__(self, name):
        raise AssertionError(f"called {name} although the upload was refused")


def _picture(work_dir, color=BAD, size=(16, 9)):
    path = work_dir / "images" / f"{line_hash('hello there')}.png"
    path.parent.mkdir(exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def _song_with_picture(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    _picture(work_dir)
    return work_dir


def _settings():
    return SimpleNamespace(youtube_privacy="public", youtube_category_id="10", youtube_made_for_kids=False,
                           youtube_upload_times="9:00", generate_thumbnails=False)


def _verdict_by_colour(monkeypatch):
    """Claude Code's answer, judged from the picture it was given: the BAD colour is a hand with no arm."""
    asked = []

    def run(args, cwd):
        name = args[-1].split()[3]           # the check prompt: "Read the picture <name> and decide ..."
        asked.append(name)
        bad = Image.open(Path(cwd) / name).convert("RGB").getpixel((0, 0)) == BAD
        return json.dumps({"structured_output": {"verdict": "bad" if bad else "good",
                                                 "problem": "a hand with no arm on a truck fender" if bad else ""}})

    monkeypatch.setattr("lyricvideo.picture_check._run_claude", run)
    return asked


def _fake_remake(monkeypatch, new_color=(10, 120, 10), new_size=(16, 9), fail: Exception | None = None):
    """remake_video_with_new_pictures as the real one ends: the bad picture swapped, a new video, the hold removed."""
    calls = []

    def remake(work_dir, settings):
        calls.append((work_dir, settings))
        # The song is held while it is made again: if the app dies now, Flagged shows it, never Pending.
        assert not (work_dir / "my-song.mp4").exists()
        assert "if this stays" in json.loads((work_dir / HELD_MARKER).read_text())["reason"]
        if fail is not None:
            raise fail
        _picture(work_dir, new_color, new_size)
        (work_dir / "my-song.mp4").write_bytes(b"the new video")
        (work_dir / HELD_MARKER).unlink()

    monkeypatch.setattr("lyricvideo.pipeline.remake_video_with_new_pictures", remake)
    return calls


def test_a_bad_picture_is_replaced_the_video_made_again_and_the_new_video_uploaded(tmp_path, monkeypatch):
    work_dir = _song_with_picture(tmp_path)
    asked = _verdict_by_colour(monkeypatch)
    remakes = _fake_remake(monkeypatch)
    settings = _settings()
    youtube = _FakeYoutubeClient(video_id="vid-new")

    assert schedule_upload(youtube, _FakeAnthropicClient(), work_dir, settings) == "vid-new"

    assert remakes == [(work_dir, settings)]                    # made again once, with the owner's own Settings
    assert len(asked) == 2                                      # the old picture, then its replacement
    assert youtube._videos.insert_kwargs is not None            # the new video went up
    assert json.loads((work_dir / "youtube_state.json").read_text())["video_id"] == "vid-new"
    assert not (work_dir / HELD_MARKER).exists()


def test_a_remake_that_fails_leaves_the_song_held_and_sends_nothing(tmp_path, monkeypatch):
    work_dir = _song_with_picture(tmp_path)
    _verdict_by_colour(monkeypatch)
    _fake_remake(monkeypatch, fail=RuntimeError("Replicate is down"))

    with pytest.raises(BadPictures, match="Replicate is down"):
        schedule_upload(_NoCalls(), _NoCalls(), work_dir, _settings())

    assert not (work_dir / "my-song.mp4").exists() and (work_dir / "my-song.previous.mp4").exists()
    reason = json.loads((work_dir / HELD_MARKER).read_text())["reason"]
    assert reason.startswith(BAD_PICTURES_PREFIX) and "hand with no arm" in reason and "Render Anyway" in reason
    assert not (work_dir / "youtube_state.json").exists()
    assert list_pending_uploads(tmp_path) == []


def test_a_remade_video_whose_new_picture_is_bad_too_is_held_not_made_a_third_time(tmp_path, monkeypatch):
    work_dir = _song_with_picture(tmp_path)
    _verdict_by_colour(monkeypatch)
    remakes = _fake_remake(monkeypatch, new_color=BAD, new_size=(20, 9))       # another picture, just as bad

    with pytest.raises(BadPictures, match="hand with no arm"):
        schedule_upload(_NoCalls(), _NoCalls(), work_dir, _settings())

    assert len(remakes) == 1
    reason = json.loads((work_dir / HELD_MARKER).read_text())["reason"]
    assert reason.startswith(BAD_PICTURES_PREFIX) and "Use Render Anyway" in reason and "if this stays" not in reason
    assert not (work_dir / "my-song.mp4").exists() and not (work_dir / "youtube_state.json").exists()


def test_a_remake_held_for_its_new_pictures_says_why(tmp_path, monkeypatch):
    from lyricvideo.pipeline import HeldBeforeVideo

    work_dir = _song_with_picture(tmp_path)
    _verdict_by_colour(monkeypatch)
    _fake_remake(monkeypatch, fail=HeldBeforeVideo("Pictures not checked yet: Claude usage limit reached"))

    with pytest.raises(BadPictures, match="usage limit"):
        schedule_upload(_NoCalls(), _NoCalls(), work_dir, _settings())


def test_pictures_that_cannot_be_checked_wait_for_a_later_try(tmp_path, monkeypatch):
    work_dir = _song_with_picture(tmp_path)
    monkeypatch.setattr("lyricvideo.picture_check._run_claude",
                        lambda args, cwd: json.dumps({"is_error": True, "result": "Claude usage limit reached"}))
    remakes = _fake_remake(monkeypatch)

    with pytest.raises(PicturesNotChecked, match="usage limit"):
        schedule_upload(_NoCalls(), _NoCalls(), work_dir, _settings())

    assert remakes == []
    assert (work_dir / "my-song.mp4").exists() and not (work_dir / HELD_MARKER).exists()
    assert list_pending_uploads(tmp_path) == ["my-song"]


def test_an_easy_version_is_remade_from_its_song(tmp_path, monkeypatch):
    from lyricvideo.pipeline import remake_video_with_new_pictures

    built = []
    monkeypatch.setattr("lyricvideo.pipeline.build_capo_variant",
                        lambda work_dir, settings=None, **k: built.append((work_dir, settings)))
    monkeypatch.setattr("lyricvideo.pipeline.run_pipeline",
                        lambda *a, **k: pytest.fail("an EASY CHORD version is never run through the pipeline itself"))
    settings = object()

    remake_video_with_new_pictures(tmp_path / "song" / "easychords", settings)

    assert built == [(tmp_path / "song", settings)]
