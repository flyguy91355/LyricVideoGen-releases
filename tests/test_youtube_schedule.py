from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.youtube_schedule import compute_next_publish_slot, schedule_upload


def test_compute_next_publish_slot_returns_now_when_nothing_reserved_yet():
    now = datetime(2026, 9, 10, 10, 0, 0)

    assert compute_next_publish_slot(now, None, min_days_between=2, preferred_hour=15) == now


def test_compute_next_publish_slot_spaces_from_the_reserved_slot_not_now():
    reserved = datetime(2026, 9, 10, 15, 0, 0)
    now = datetime(2026, 9, 10, 10, 5, 0)  # much earlier than the reserved slot

    slot = compute_next_publish_slot(now, reserved, min_days_between=2, preferred_hour=15)

    assert slot == datetime(2026, 9, 12, 15, 0, 0)


def test_compute_next_publish_slot_snaps_to_the_preferred_hour():
    reserved = datetime(2026, 9, 10, 9, 47, 33)

    slot = compute_next_publish_slot(datetime(2026, 9, 10), reserved, min_days_between=1, preferred_hour=15)

    assert slot == datetime(2026, 9, 11, 15, 0, 0)


def test_compute_next_publish_slot_chain_spaces_multiple_batch_items_evenly():
    slot1 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 0), None, min_days_between=2, preferred_hour=15)
    slot2 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 1), slot1, min_days_between=2, preferred_hour=15)
    slot3 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 2), slot2, min_days_between=2, preferred_hour=15)

    assert slot1 == datetime(2026, 9, 10, 8, 0)
    assert slot2 == datetime(2026, 9, 12, 15, 0, 0)
    assert slot3 == datetime(2026, 9, 14, 15, 0, 0)


class _FakeUploadRequest:
    def __init__(self, video_id):
        self._video_id = video_id

    def next_chunk(self):
        return None, {"id": self._video_id}


class _FakeVideosResource:
    def __init__(self, video_id):
        self._video_id = video_id
        self.insert_kwargs = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeUploadRequest(self._video_id)


class _FakeYoutubeClient:
    def __init__(self, video_id="vid123"):
        self._videos = _FakeVideosResource(video_id)

    def videos(self):
        return self._videos


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self):
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicResponse(
            "TITLE: My Song - Play Along\nDESCRIPTION: A great song.\nTAGS: tag1, tag2"
        )

    def prompt_text(self) -> str:
        return self.last_kwargs["messages"][0]["content"]


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _make_song_work_dir(tmp_path, artist: str | None = None) -> Path:
    work_dir = tmp_path / "my-song"
    work_dir.mkdir()
    song = Song(
        title="My Song", audio_path="song.mp3",
        lines=[LyricLine(words=[Word(word="hello"), Word(word="there")])],
    )
    save_song(song, work_dir / "lyrics_timed.json")
    (work_dir / "my-song.mp4").write_bytes(b"fake video bytes")
    if artist is not None:
        import json
        (work_dir / "song_info.json").write_text(
            json.dumps({"title": "My Song", "artist": artist, "duration": 200.0, "alt_titles": []}),
            encoding="utf-8",
        )
    return work_dir


def test_schedule_upload_public_uses_private_status_with_publish_at(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="public", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid123")
    next_slot_path = tmp_path / "next_slot.json"

    video_id = schedule_upload(
        client, _FakeAnthropicClient(), work_dir, settings,
        now=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc), next_slot_path=next_slot_path,
    )

    assert video_id == "vid123"
    body = client._videos.insert_kwargs["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert "publishAt" in body["status"]
    assert next_slot_path.exists()


def test_schedule_upload_unlisted_skips_publish_at_and_next_slot_file(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid456")
    next_slot_path = tmp_path / "next_slot.json"

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings, next_slot_path=next_slot_path)

    body = client._videos.insert_kwargs["body"]
    assert body["status"]["privacyStatus"] == "unlisted"
    assert "publishAt" not in body["status"]
    assert not next_slot_path.exists()


def test_schedule_upload_appends_support_description_text_to_the_description(tmp_path):
    """Deliberately a SEPARATE field from support_overlay_text (the on-screen
    watermark) -- real mixup found live, 2026-09-11: sharing one field left
    the description saying "link in description" with no real link in it."""
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
        support_description_text="Support: https://ko-fi.com/x",
    )
    client = _FakeYoutubeClient(video_id="vid999")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings, next_slot_path=tmp_path / "next_slot.json")

    description = client._videos.insert_kwargs["body"]["snippet"]["description"]
    assert "Support: https://ko-fi.com/x" in description
    assert "A great song." in description  # the AI-written description is still there too


def test_schedule_upload_leaves_description_unchanged_when_support_description_text_is_blank(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
        support_description_text="",
    )
    client = _FakeYoutubeClient(video_id="vid999")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings, next_slot_path=tmp_path / "next_slot.json")

    description = client._videos.insert_kwargs["body"]["snippet"]["description"]
    assert description == "A great song."


def test_schedule_upload_saves_youtube_state(tmp_path):
    from lyricvideo.youtube_state import load_youtube_state

    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="private", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid789")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings, next_slot_path=tmp_path / "next_slot.json")

    state = load_youtube_state(work_dir)
    assert state is not None
    assert state.video_id == "vid789"
    assert state.title == "My Song - Play Along"


def test_schedule_upload_passes_the_real_artist_from_song_info_json(tmp_path):
    """identify.py already resolves the real artist and writes it to
    song_info.json -- the title-writing prompt must be told this real fact
    rather than guessing, so an upload never ships a title missing a known
    artist."""
    work_dir = _make_song_work_dir(tmp_path, artist="Pink Floyd")
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeAnthropicClient()

    schedule_upload(
        _FakeYoutubeClient(), client, work_dir, settings, next_slot_path=tmp_path / "next_slot.json",
    )

    assert "Pink Floyd" in client.messages.prompt_text()


def test_schedule_upload_tolerates_a_missing_song_info_json(tmp_path):
    """A song created before song_info.json existed, or any other missing-
    file edge case, must still upload -- just without a known artist to
    state (never a fabricated one)."""
    work_dir = _make_song_work_dir(tmp_path)  # no song_info.json written at all
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeAnthropicClient()

    video_id = schedule_upload(
        _FakeYoutubeClient(video_id="vidabc"), client, work_dir, settings, next_slot_path=tmp_path / "next_slot.json",
    )

    assert video_id == "vidabc"
    assert "performed by" not in client.messages.prompt_text().lower()
