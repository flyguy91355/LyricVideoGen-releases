"""schedule_upload checks every picture of a video before it goes out (owner, 2026-11-13: Quarters and Dimes, made
before the picture check, had a cut-off hand on a truck fender): a bad one holds the song, an unchecked one waits."""
import json

import pytest
from PIL import Image

from lyricvideo.models import line_hash
from lyricvideo.pipeline import BAD_PICTURES_PREFIX, HELD_MARKER, list_pending_uploads
from lyricvideo.youtube_schedule import BadPictures, PicturesNotChecked, schedule_upload
from tests.test_youtube_schedule import _make_song_work_dir


@pytest.fixture(autouse=True)
def _whole_videos(monkeypatch):
    monkeypatch.setattr("lyricvideo.youtube_schedule.rendered_stream_seconds", lambda path: (200.0, 200.0))


class _NoCalls:
    """Neither Claude nor YouTube may be reached when the pictures stop an upload."""

    def __getattr__(self, name):
        raise AssertionError(f"called {name} although the upload was refused")


def _song_with_picture(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    (work_dir / "images").mkdir()
    Image.new("RGB", (16, 9), (200, 0, 0)).save(work_dir / "images" / f"{line_hash('hello there')}.png")
    return work_dir


def _settings():
    from types import SimpleNamespace
    return SimpleNamespace(youtube_privacy="public", youtube_category_id="10", youtube_made_for_kids=False,
                           youtube_upload_times="9:00")


def test_a_bad_picture_holds_the_song_before_any_paid_or_youtube_call(tmp_path, monkeypatch):
    work_dir = _song_with_picture(tmp_path)
    monkeypatch.setattr("lyricvideo.picture_check._run_claude", lambda args, cwd: json.dumps(
        {"structured_output": {"verdict": "bad", "problem": "a hand with no arm on a truck fender"}}))

    with pytest.raises(BadPictures, match="hand with no arm"):
        schedule_upload(_NoCalls(), _NoCalls(), work_dir, _settings())

    assert not (work_dir / "my-song.mp4").exists() and (work_dir / "my-song.previous.mp4").exists()
    assert json.loads((work_dir / HELD_MARKER).read_text())["reason"].startswith(BAD_PICTURES_PREFIX)
    assert not (work_dir / "youtube_state.json").exists()
    assert list_pending_uploads(tmp_path) == []


def test_pictures_that_cannot_be_checked_wait_for_a_later_try(tmp_path, monkeypatch):
    work_dir = _song_with_picture(tmp_path)
    monkeypatch.setattr("lyricvideo.picture_check._run_claude",
                        lambda args, cwd: json.dumps({"is_error": True, "result": "Claude usage limit reached"}))

    with pytest.raises(PicturesNotChecked, match="usage limit"):
        schedule_upload(_NoCalls(), _NoCalls(), work_dir, _settings())

    assert (work_dir / "my-song.mp4").exists() and not (work_dir / HELD_MARKER).exists()
    assert list_pending_uploads(tmp_path) == ["my-song"]
