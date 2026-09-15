from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.youtube_schedule import compute_next_publish_slot, schedule_upload


def test_compute_next_publish_slot_returns_today_when_nothing_reserved_yet():
    now = datetime(2026, 9, 10, 10, 0, 0)

    slot = compute_next_publish_slot(now, set(), min_days_between=2, preferred_hour=15)

    assert slot == datetime(2026, 9, 10, 15, 0, 0)


def test_compute_next_publish_slot_spaces_past_a_claimed_date():
    claimed = {date(2026, 9, 10)}
    now = datetime(2026, 9, 10, 10, 5, 0)

    slot = compute_next_publish_slot(now, claimed, min_days_between=2, preferred_hour=15)

    assert slot == datetime(2026, 9, 12, 15, 0, 0)


def test_compute_next_publish_slot_snaps_to_the_preferred_hour():
    claimed = {date(2026, 9, 10)}

    slot = compute_next_publish_slot(datetime(2026, 9, 10), claimed, min_days_between=1, preferred_hour=15)

    assert slot == datetime(2026, 9, 11, 15, 0, 0)


def test_compute_next_publish_slot_snaps_to_preferred_hour_in_local_time_not_utc():
    """Real bug, 2026-09-13: `now` may arrive UTC-aware (as it always does
    from the real YouTube API's timestamps converted to a local `now`) --
    snapping the hour without first converting to local time would
    silently turn a 2pm-Eastern cadence into 2pm UTC (10am Eastern)."""
    now = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)

    slot = compute_next_publish_slot(now, set(), min_days_between=1, preferred_hour=14)

    assert slot.astimezone().hour == 14


def test_compute_next_publish_slot_fills_a_gap_between_two_claimed_dates():
    """Real scenario, 2026-09-13: the owner manually publishes an already-
    scheduled video early, or a stale counter once inflated the schedule
    by extra days -- either way, a multi-day gap opens up in the real
    channel schedule. The next new upload should land IN that gap, not
    stack on past the furthest-out already-claimed date. Mirrors the real
    incident's actual dates: today plus a solid run of already-claimed
    days (9/13-9/19), then a 14-day gap, then one stray far-future claim
    (10/3, the mis-scheduled video from that incident)."""
    claimed = {date(2026, 9, d) for d in range(13, 20)} | {date(2026, 10, 3)}
    now = datetime(2026, 9, 13, 8, 0)

    slot = compute_next_publish_slot(now, claimed, min_days_between=1, preferred_hour=14)

    assert slot == datetime(2026, 9, 20, 14, 0, 0)  # the real opened-up gap, not past 10/3


def test_compute_next_publish_slot_skips_every_day_already_claimed():
    claimed = {date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)}
    now = datetime(2026, 9, 14, 8, 0)

    slot = compute_next_publish_slot(now, claimed, min_days_between=1, preferred_hour=14)

    assert slot == datetime(2026, 9, 17, 14, 0, 0)


def test_compute_next_publish_slot_respects_spacing_on_both_sides_of_a_gap():
    """min_days_between=2 means a candidate must be 2+ days from EVERY
    claimed date, not just the nearest one -- so a single-day gap between
    two claimed dates 2 days apart is too narrow to use."""
    claimed = {date(2026, 9, 10), date(2026, 9, 12)}  # a 1-day gap on the 11th
    now = datetime(2026, 9, 10, 8, 0)

    slot = compute_next_publish_slot(now, claimed, min_days_between=2, preferred_hour=15)

    assert slot == datetime(2026, 9, 14, 15, 0, 0)  # the 11th is skipped -- too close to both sides


def test_compute_next_publish_slot_never_returns_a_time_already_in_the_past():
    """Real incident, night of 2026-09-14 into 2026-09-15: two videos
    uploaded late in the evening, after that day's preferred_hour had
    already passed, both landed on an unclaimed "today" and got a
    publishAt already in the past -- YouTube auto-published them within a
    few hours instead of scheduling them into the future like every other
    video that same night. An unclaimed date isn't enough on its own; the
    resulting datetime must still be ahead of `now`."""
    now = datetime(2026, 9, 14, 20, 0, 0)  # 8pm, well past preferred_hour=14

    slot = compute_next_publish_slot(now, set(), min_days_between=1, preferred_hour=14)

    assert slot == datetime(2026, 9, 15, 14, 0, 0)


def test_compute_next_publish_slot_keeps_walking_past_a_claim_after_skipping_a_stale_today():
    claimed = {date(2026, 9, 15)}
    now = datetime(2026, 9, 14, 20, 0, 0)  # today's preferred_hour already passed

    slot = compute_next_publish_slot(now, claimed, min_days_between=1, preferred_hour=14)

    assert slot == datetime(2026, 9, 16, 14, 0, 0)


def test_compute_next_publish_slot_chain_spaces_multiple_batch_items_evenly():
    claimed: set[date] = set()
    slot1 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 0), claimed, min_days_between=2, preferred_hour=15)
    claimed = {slot1.date()}
    slot2 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 1), claimed, min_days_between=2, preferred_hour=15)
    claimed = {slot1.date(), slot2.date()}
    slot3 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 2), claimed, min_days_between=2, preferred_hour=15)

    assert slot1 == datetime(2026, 9, 10, 15, 0, 0)
    assert slot2 == datetime(2026, 9, 12, 15, 0, 0)
    assert slot3 == datetime(2026, 9, 14, 15, 0, 0)


class _FakeUploadRequest:
    def __init__(self, video_id):
        self._video_id = video_id

    def next_chunk(self):
        return None, {"id": self._video_id}


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeVideosResource:
    def __init__(self, video_id, existing_videos=()):
        self._video_id = video_id
        self.insert_kwargs = None
        self._existing_videos = list(existing_videos)

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeUploadRequest(self._video_id)

    def list(self, id, part=None):
        requested_ids = set(id.split(","))
        items = [v for v in self._existing_videos if v["id"] in requested_ids]
        return _FakeExecutable({"items": items})


class _FakeYoutubeClient:
    """existing_videos lets a test simulate the channel's real, already-
    uploaded history: latest_reserved_publish_time() reads this instead of
    any local file (see the 2026-09-13 fix to youtube_schedule.py). Each
    entry is a raw videos().list()-shaped dict:
    {"id": ..., "status": {"publishAt": ...}, "snippet": {"publishedAt": ...}}."""

    def __init__(self, video_id="vid123", existing_videos=()):
        self._videos = _FakeVideosResource(video_id, existing_videos)
        self._existing_video_ids = [v["id"] for v in existing_videos]

    def videos(self):
        return self._videos

    def channels(self):
        return SimpleNamespace(list=lambda part, mine: _FakeExecutable(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "uploads-playlist"}}}]}
        ))

    def playlistItems(self):
        video_ids = self._existing_video_ids

        def list_(part, playlistId, maxResults, pageToken):
            return _FakeExecutable({
                "items": [{"contentDetails": {"videoId": vid}} for vid in video_ids],
            })

        return SimpleNamespace(list=list_)


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
        return _FakeAnthropicResponse("DESCRIPTION: A great song.\nTAGS: tag1, tag2")

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

    video_id = schedule_upload(
        client, _FakeAnthropicClient(), work_dir, settings,
        now=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert video_id == "vid123"
    body = client._videos.insert_kwargs["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert "publishAt" in body["status"]


def test_schedule_upload_public_spaces_past_a_date_already_claimed_on_the_real_channel(tmp_path):
    """The reserved dates come from the channel's own live videos, not any
    local file -- a video already claiming today's date pushes a new
    upload to tomorrow, even though nothing local has ever recorded it."""
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="public", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=1, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid123", existing_videos=[
        {"id": "already-scheduled", "status": {"publishAt": "2026-09-10T18:00:00Z"}, "snippet": {}},
    ])

    schedule_upload(
        client, _FakeAnthropicClient(), work_dir, settings,
        now=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc),
    )

    publish_at = client._videos.insert_kwargs["body"]["status"]["publishAt"]
    assert publish_at.startswith("2026-09-11")


def test_schedule_upload_public_fills_a_gap_instead_of_stacking_past_a_far_future_claim(tmp_path):
    """Real scenario, 2026-09-13: one video is mis-scheduled far in the
    future (here, three weeks out) while today is wide open -- the new
    upload must fill today, not queue up behind that far-future outlier."""
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="public", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=1, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid123", existing_videos=[
        {"id": "far-future-outlier", "status": {"publishAt": "2026-10-01T18:00:00Z"}, "snippet": {}},
    ])

    schedule_upload(
        client, _FakeAnthropicClient(), work_dir, settings,
        now=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc),
    )

    publish_at = client._videos.insert_kwargs["body"]["status"]["publishAt"]
    assert publish_at.startswith("2026-09-10")


def test_schedule_upload_unlisted_skips_publish_at(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid456")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings)

    body = client._videos.insert_kwargs["body"]
    assert body["status"]["privacyStatus"] == "unlisted"
    assert "publishAt" not in body["status"]


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

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings)

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

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings)

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

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings)

    state = load_youtube_state(work_dir)
    assert state is not None
    assert state.video_id == "vid789"
    assert state.title == "My Song - (Play Along Lyrics & Chords)"


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
        _FakeYoutubeClient(), client, work_dir, settings,
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
        _FakeYoutubeClient(video_id="vidabc"), client, work_dir, settings,
    )

    assert video_id == "vidabc"
    assert "performed by" not in client.messages.prompt_text().lower()
