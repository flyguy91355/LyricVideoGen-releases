from datetime import datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace

from lyricvideo.chord_theory import save_easy_chord_capo_marker
from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.youtube_schedule import (
    compute_next_publish_slot,
    evenly_spaced_upload_times,
    format_upload_times,
    parse_upload_times,
    schedule_upload,
)


def test_parse_upload_times_parses_and_sorts_a_comma_separated_list():
    assert parse_upload_times("14:00,9:30,19:00") == [time(9, 30), time(14, 0), time(19, 0)]


def test_parse_upload_times_tolerates_whitespace():
    assert parse_upload_times(" 9:00 , 14:00 ") == [time(9, 0), time(14, 0)]


def test_parse_upload_times_dedupes_identical_times():
    assert parse_upload_times("9:00,9:00,14:00") == [time(9, 0), time(14, 0)]


def test_parse_upload_times_skips_a_malformed_entry_rather_than_raising():
    assert parse_upload_times("9:00,not-a-time,14:00") == [time(9, 0), time(14, 0)]


def test_parse_upload_times_falls_back_to_a_default_when_everything_is_unusable():
    assert parse_upload_times("garbage") == [time(15, 0)]


def test_parse_upload_times_falls_back_to_a_default_when_blank():
    assert parse_upload_times("") == [time(15, 0)]


def test_format_upload_times_round_trips_through_parse():
    times = [time(19, 0), time(9, 30), time(14, 0)]

    assert parse_upload_times(format_upload_times(times)) == sorted(times)


def test_format_upload_times_zero_pads():
    assert format_upload_times([time(9, 5)]) == "09:05"


def test_evenly_spaced_upload_times_count_one_keeps_the_original_default():
    assert evenly_spaced_upload_times(1) == [time(15, 0)]


def test_evenly_spaced_upload_times_count_two_uses_both_window_endpoints():
    assert evenly_spaced_upload_times(2) == [time(9, 0), time(21, 0)]


def test_evenly_spaced_upload_times_count_three_spreads_across_the_day():
    assert evenly_spaced_upload_times(3) == [time(9, 0), time(15, 0), time(21, 0)]


def test_evenly_spaced_upload_times_count_five():
    assert evenly_spaced_upload_times(5) == [time(9, 0), time(12, 0), time(15, 0), time(18, 0), time(21, 0)]


def test_evenly_spaced_upload_times_returns_exactly_count_entries():
    assert len(evenly_spaced_upload_times(10)) == 10


def test_evenly_spaced_upload_times_clamps_below_one():
    assert evenly_spaced_upload_times(0) == [time(15, 0)]


def test_compute_next_publish_slot_returns_today_when_nothing_reserved_yet():
    now = datetime(2026, 9, 10, 10, 0, 0)

    slot = compute_next_publish_slot(now, set(), [time(15, 0)])

    assert slot == datetime(2026, 9, 10, 15, 0, 0)


def test_compute_next_publish_slot_fills_every_configured_time_before_rolling_to_tomorrow():
    """Three times/day: the first three uploads of the day land at 9/14/19
    on the SAME day, and only the fourth spills into tomorrow."""
    now = datetime(2026, 9, 10, 8, 0, 0)
    upload_times = [time(9, 0), time(14, 0), time(19, 0)]

    slot1 = compute_next_publish_slot(now, set(), upload_times)
    slot2 = compute_next_publish_slot(now, {slot1}, upload_times)
    slot3 = compute_next_publish_slot(now, {slot1, slot2}, upload_times)
    slot4 = compute_next_publish_slot(now, {slot1, slot2, slot3}, upload_times)

    assert slot1 == datetime(2026, 9, 10, 9, 0, 0)
    assert slot2 == datetime(2026, 9, 10, 14, 0, 0)
    assert slot3 == datetime(2026, 9, 10, 19, 0, 0)
    assert slot4 == datetime(2026, 9, 11, 9, 0, 0)


def test_compute_next_publish_slot_snaps_to_local_time_not_utc():
    """Real bug, 2026-09-13: `now` may arrive UTC-aware (as it always does
    from the real YouTube API's timestamps converted to a local `now`) --
    snapping the hour without first converting to local time would
    silently turn a 2pm-Eastern cadence into 2pm UTC (10am Eastern)."""
    now = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)

    slot = compute_next_publish_slot(now, set(), [time(14, 0)])

    assert slot.astimezone().hour == 14


def test_compute_next_publish_slot_fills_a_gap_left_by_an_early_manual_publish():
    """Real scenario, 2026-09-13: the owner manually publishes an already-
    scheduled video early, freeing up that slot -- the next new upload
    should land back in that freed slot rather than stacking past a
    still-claimed later one."""
    now = datetime(2026, 9, 13, 8, 0, 0)
    upload_times = [time(9, 0), time(15, 0)]
    # 9:00 today already freed by an early manual publish; 15:00 still claimed.
    claimed = {datetime(2026, 9, 13, 15, 0, 0)}

    slot = compute_next_publish_slot(now, claimed, upload_times)

    assert slot == datetime(2026, 9, 13, 9, 0, 0)


def test_compute_next_publish_slot_skips_every_slot_already_claimed_today():
    now = datetime(2026, 9, 14, 8, 0, 0)
    upload_times = [time(9, 0), time(14, 0)]
    claimed = {datetime(2026, 9, 14, 9, 0, 0), datetime(2026, 9, 14, 14, 0, 0)}

    slot = compute_next_publish_slot(now, claimed, upload_times)

    assert slot == datetime(2026, 9, 15, 9, 0, 0)


def test_compute_next_publish_slot_never_returns_a_time_already_in_the_past():
    """Real incident, night of 2026-09-14 into 2026-09-15: an upload late
    in the evening, after that day's slot time had already passed, landed
    on an unclaimed "today" and got a publishAt already in the past --
    YouTube auto-published it immediately instead of scheduling into the
    future. An unclaimed slot isn't enough on its own; it must still be
    ahead of `now`."""
    now = datetime(2026, 9, 14, 20, 0, 0)  # 8pm, well past 14:00

    slot = compute_next_publish_slot(now, set(), [time(14, 0)])

    assert slot == datetime(2026, 9, 15, 14, 0, 0)


def test_compute_next_publish_slot_rolls_to_tomorrows_first_slot_after_todays_are_gone():
    now = datetime(2026, 9, 14, 20, 0, 0)  # both of today's times already past
    upload_times = [time(9, 0), time(14, 0)]

    slot = compute_next_publish_slot(now, set(), upload_times)

    assert slot == datetime(2026, 9, 15, 9, 0, 0)


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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
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
        youtube_upload_times="15:00",
    )
    client = _FakeAnthropicClient()

    video_id = schedule_upload(
        _FakeYoutubeClient(video_id="vidabc"), client, work_dir, settings,
    )

    assert video_id == "vidabc"
    assert "performed by" not in client.messages.prompt_text().lower()


# --- EASY CHORD (capo) variants get their own deterministic title/description marker ------------------

def _make_capo_song_work_dir(tmp_path, artist: str = "Simon and Garfunkel") -> Path:
    work_dir = tmp_path / "bridge-over-troubled-water-capo"
    work_dir.mkdir()
    song = Song(
        title="Bridge Over Troubled Water EasyChords", audio_path="song.mp3",
        lines=[LyricLine(words=[Word(word="hello"), Word(word="there")])],
    )
    save_song(song, work_dir / "lyrics_timed.json")
    (work_dir / "bridge-over-troubled-water-easychords.mp4").write_bytes(b"fake video bytes")
    import json
    (work_dir / "song_info.json").write_text(
        json.dumps({
            "title": "Bridge Over Troubled Water EasyChords", "artist": artist,
            "duration": 200.0, "alt_titles": [],
        }),
        encoding="utf-8",
    )
    save_easy_chord_capo_marker(
        work_dir, capo_fret=1, shape_key="D", original_key="Eb major",
        original_title="Bridge Over Troubled Water",
    )
    return work_dir


def test_schedule_upload_uses_the_easy_chord_title_and_description_for_a_capo_variant(tmp_path):
    """Owner, 2026-09-23: "it must have EASY CHORDS in the title and description" -- and separately,
    "should maybe have that in the upload file too" (the marker must actually be read here, not just at
    render time)."""
    work_dir = _make_capo_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_upload_times="15:00",
    )
    client = _FakeYoutubeClient(video_id="vid123")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings)

    body = client._videos.insert_kwargs["body"]
    assert body["snippet"]["title"] == "Bridge Over Troubled Water - Simon and Garfunkel - (EASY CHORDS Play Along - Capo 1)"
    assert body["snippet"]["description"].startswith(
        "EASY CHORDS version -- Capo 1, play it in D shapes (original key: Eb major).\n\n"
    )
    assert "A great song." in body["snippet"]["description"]   # the AI-authored part is still there, just prefixed


def test_schedule_upload_gives_claude_the_clean_original_title_for_a_capo_variant(tmp_path):
    """The "EasyChords"-suffixed filename title must never leak into the description-writing prompt."""
    work_dir = _make_capo_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_upload_times="15:00",
    )
    client = _FakeAnthropicClient()

    schedule_upload(_FakeYoutubeClient(), client, work_dir, settings)

    assert "EasyChords" not in client.messages.prompt_text()
    assert "Bridge Over Troubled Water" in client.messages.prompt_text()


def test_schedule_upload_uses_the_normal_title_for_an_ordinary_song(tmp_path):
    work_dir = _make_song_work_dir(tmp_path, artist="Pink Floyd")
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_upload_times="15:00",
    )
    client = _FakeYoutubeClient(video_id="vid123")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings)

    body = client._videos.insert_kwargs["body"]
    assert "EASY CHORDS" not in body["snippet"]["title"]
    assert not body["snippet"]["description"].startswith("EASY CHORDS version")


def test_seven_uploads_with_five_publish_times_fill_five_today_then_two_tomorrow():
    """Owner's intended behavior (2026-09-19): 5 publish times a day; uploads
    beyond today's 5 spill onto tomorrow's slots, still 5 a day."""
    from datetime import datetime, timedelta

    times = parse_upload_times("09:00,12:00,15:00,18:00,21:00")
    now = datetime(2026, 9, 19, 8, 0).astimezone()
    claimed: set = set()
    slots = []
    for _ in range(7):
        slot = compute_next_publish_slot(now, claimed, times)
        claimed.add(slot)
        slots.append(slot)

    today = now.date()
    assert [(s.date() - today).days for s in slots] == [0, 0, 0, 0, 0, 1, 1]
    assert [f"{s:%H:%M}" for s in slots] == ["09:00", "12:00", "15:00", "18:00", "21:00", "09:00", "12:00"]


def test_publishing_todays_scheduled_video_early_frees_its_slot_for_the_next_upload():
    """Owner's requirement (2026-09-19): making a scheduled video public by hand
    opens up a slot for another video. Same-day case: at 10:30 the owner
    publishes today's 12:00 video right now. That early publish is a past,
    off-slot moment -- it must not also eat one of today's five slots, so the
    next upload lands in the freed 12:00 slot instead of rolling to tomorrow."""
    times = [time(9, 0), time(12, 0), time(15, 0), time(18, 0), time(21, 0)]
    now = datetime(2026, 9, 19, 10, 30, 0)
    claimed = {
        datetime(2026, 9, 19, 9, 0, 0),    # already published on schedule
        datetime(2026, 9, 19, 10, 30, 0),  # the 12:00 video, published early by hand
        datetime(2026, 9, 19, 15, 0, 0),
        datetime(2026, 9, 19, 18, 0, 0),
        datetime(2026, 9, 19, 21, 0, 0),
    }

    slot = compute_next_publish_slot(now, claimed, times)

    assert slot == datetime(2026, 9, 19, 12, 0, 0)


def test_a_still_scheduled_off_slot_video_still_counts_against_its_day():
    """Unchanged rule: a FUTURE claim at an odd hour (e.g. a manual upload
    scheduled for 10:00/13:00) still uses up that day's capacity."""
    times = [time(9, 0), time(15, 0)]
    now = datetime(2026, 9, 19, 8, 0, 0)
    claimed = {datetime(2026, 9, 19, 10, 0, 0), datetime(2026, 9, 19, 13, 0, 0)}

    slot = compute_next_publish_slot(now, claimed, times)

    assert slot == datetime(2026, 9, 20, 9, 0, 0)
