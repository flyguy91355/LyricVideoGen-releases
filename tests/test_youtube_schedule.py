from datetime import datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo.key_decision import KeyDecision, KeyNotConfirmed, save_decision

from lyricvideo.chord_theory import save_easy_chord_capo_marker
from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.youtube_schedule import (
    compute_next_publish_slot,
    evenly_spaced_upload_times,
    format_upload_times,
    parse_upload_times,
    schedule_upload,
)


@pytest.fixture(autouse=True)
def _whole_videos(monkeypatch):
    """The mp4s here are a few fake bytes; schedule_upload's cut-short check (a real ffmpeg read of the file) is told
    every video is whole. tests/test_upload_guard_truncated.py covers the check itself."""
    monkeypatch.setattr("lyricvideo.youtube_schedule.rendered_stream_seconds", lambda path: (200.0, 200.0))


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
    save_decision(work_dir, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major"))
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
    save_decision(work_dir, KeyDecision(status="confirmed", key="Eb major", source="agreed", chord_key="Eb major"))
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
    assert body["snippet"]["description"] == (
        "A great song.\n\nEASY CHORDS version -- Capo 1, play it in D shapes (original key: Eb major)."
    )   # the AI-authored part reads first now, the capo/shape line right after it (owner, 2026-09-29)


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


def test_schedule_upload_never_uploads_a_video_whose_metadata_could_not_be_generated(tmp_path):
    """Owner, 2026-09-25, after "Blackbird" went up with no description and no tags: "should not have been
    uploaded like that." When Claude never returns usable metadata the upload must not happen at all -- the
    song stays in Pending uploads to be retried, rather than publishing a bare video."""
    import pytest

    from lyricvideo.youtube_metadata import MetadataGenError

    class _ProseMessages:
        def create(self, **kwargs):
            return _FakeAnthropicResponse("I should clarify something important before providing this metadata...")

    class _ProseClient:
        messages = _ProseMessages()

    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="public", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_upload_times="15:00", support_description_text="Support: https://ko-fi.com/x",
    )
    client = _FakeYoutubeClient(video_id="vid123")

    with pytest.raises(MetadataGenError):
        schedule_upload(client, _ProseClient(), work_dir, settings, now=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc))

    assert client._videos.insert_kwargs is None       # nothing was uploaded
    assert not (work_dir / "youtube_state.json").exists()


# --- the support text is a small TEMPLATE (owner, 2026-09-26): what goes above the song description, the marker
# {description}, and what goes below. A layout the owner can edit in Settings, not a fixed rule in code. ------------
from lyricvideo.youtube_schedule import description_body, render_description  # noqa: E402

_TOP = '☕ Tips are never expected, but always appreciated: https://ko-fi.com/playalongvideos\n▼ Click "more" for the song info ▼'
_BOTTOM = "I hope you're enjoying the Play Alongs and that they're helping you grow as a musician. Thanks for playing along!"
_TEMPLATE = f"{_TOP}\n{{description}}\n{_BOTTOM}"
_OLD = "Support: https://ko-fi.com/playalongvideos"


def test_render_description_puts_the_top_the_song_text_and_the_bottom_in_order():
    assert render_description(_TEMPLATE, "A great song. More.") == f"{_TOP}\n\nA great song. More.\n\n{_BOTTOM}"


def test_render_description_without_a_marker_puts_the_whole_block_below_the_description():
    assert render_description("Support: https://x", "A great song.") == "A great song.\n\nSupport: https://x"


def test_render_description_leaves_the_description_alone_when_the_template_is_blank():
    assert render_description("", "A song.") == "A song."
    assert render_description("  \n ", "A song.") == "A song."


def test_render_description_supports_a_top_only_and_a_bottom_only_template():
    assert render_description("Top line\n{description}", "Body.") == "Top line\n\nBody."
    assert render_description("{description}\nBottom line", "Body.") == "Body.\n\nBottom line"


def test_render_description_with_an_empty_description_gives_just_the_template_text():
    assert render_description(_TEMPLATE, "") == f"{_TOP}\n\n{_BOTTOM}"


def test_render_description_keeps_an_easy_chords_intro_line_with_the_song_text():
    body = "EASY CHORDS version -- Capo 3, play it in D shapes (original key: F).\n\nA great song."

    assert render_description(_TEMPLATE, body) == f"{_TOP}\n\n{body}\n\n{_BOTTOM}"


def test_description_body_strips_the_old_bottom_support_line():
    assert description_body(f"A great song. More.\n\n{_OLD}", _OLD, _TEMPLATE) == "A great song. More."


def test_description_body_strips_a_description_already_rendered_with_the_template():
    rendered = render_description(_TEMPLATE, "A great song. More.")

    assert description_body(rendered, _OLD, _TEMPLATE) == "A great song. More."


def test_rendering_after_extracting_the_body_is_stable():
    """What the update script relies on: a video already in the new layout comes out unchanged."""
    rendered = render_description(_TEMPLATE, "A great song. More.")

    assert render_description(_TEMPLATE, description_body(rendered, _OLD, _TEMPLATE)) == rendered


def test_description_body_leaves_a_description_that_never_had_any_support_text_alone():
    assert description_body("Just a song.", _OLD, _TEMPLATE) == "Just a song."


def test_schedule_upload_renders_the_support_template_around_the_description(tmp_path):
    class _Messages:
        def create(self, **kwargs):
            return _FakeAnthropicResponse("DESCRIPTION: A great song. It is about hope.\nTAGS: a, b")

    class _Client:
        messages = _Messages()

    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_upload_times="15:00", support_description_text=_TEMPLATE,
    )
    client = _FakeYoutubeClient(video_id="vid1")

    schedule_upload(client, _Client(), work_dir, settings)

    assert client._videos.insert_kwargs["body"]["snippet"]["description"] == (
        f"{_TOP}\n\nA great song. It is about hope.\n\n{_BOTTOM}"
    )


def test_description_body_can_strip_several_old_pieces_at_once():
    """Changing the sign-off wording later: the previous sentence AND the very old "Support:" line both go."""
    text = f"A song.\n\n{_OLD}\n\nOld sign-off."

    assert description_body(text, [_OLD, "Old sign-off."], _TEMPLATE) == "A song."


def test_swapping_the_sign_off_leaves_exactly_one_new_sign_off():
    old_template = f"{_TOP}\n{{description}}\nOld sign-off."
    new_template = f"{_TOP}\n{{description}}\nNew sign-off."
    rendered_old = render_description(old_template, "A song.")

    body = description_body(rendered_old, [_OLD, "Old sign-off."], new_template)

    assert render_description(new_template, body) == f"{_TOP}\n\nA song.\n\nNew sign-off."


# --- the song's key: settled before any upload, stated the same everywhere (owner, 2026-09-26) -------------------------

_PUBLIC_SETTINGS = dict(youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False, youtube_upload_times="15:00")


def test_a_song_whose_key_was_never_checked_is_not_uploaded(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    (work_dir / "key_decision.json").unlink()
    client = _FakeYoutubeClient(video_id="vid123")

    with pytest.raises(KeyNotConfirmed):
        schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    assert client._videos.insert_kwargs is None


def test_a_song_whose_key_is_waiting_for_the_owner_is_not_uploaded(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    save_decision(work_dir, KeyDecision(status="review", chord_key="D major", published_key="G major"))
    client = _FakeYoutubeClient(video_id="vid123")

    with pytest.raises(KeyNotConfirmed):
        schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    assert client._videos.insert_kwargs is None


def test_a_plain_uploads_description_states_no_key_since_its_badge_is_already_correct(tmp_path):
    """Owner, 2026-09-29: "the key if you need one, not all do if there correct" -- a freshly uploaded video's own
    Key/BPM badge was rendered from this same settled key, so restating it in text would be pure redundancy. A real
    correction is a separate, later fix (key_note.apply_key_note) for an OLDER video whose badge was wrong."""
    work_dir = _make_song_work_dir(tmp_path)
    client = _FakeYoutubeClient(video_id="vid123")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    assert client._videos.insert_kwargs["body"]["snippet"]["description"] == "A great song."


def test_an_easy_chord_description_states_the_songs_original_key(tmp_path):
    work_dir = _make_capo_song_work_dir(tmp_path)
    client = _FakeYoutubeClient(video_id="vid123")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    assert "(original key: Eb major)" in client._videos.insert_kwargs["body"]["snippet"]["description"]


def test_an_easy_chord_video_built_for_a_different_key_than_the_songs_is_not_uploaded(tmp_path):
    work_dir = _make_capo_song_work_dir(tmp_path)
    save_decision(work_dir, KeyDecision(status="confirmed", key="Bb major", source="owner", chord_key="Eb major"))   # the key was corrected later
    client = _FakeYoutubeClient(video_id="vid123")

    with pytest.raises(KeyNotConfirmed, match="EASY"):
        schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    assert client._videos.insert_kwargs is None


# --- daylight saving: every slot is localized for its OWN date (issue #7 review, F023) --------------------------------

def _chicago():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        return zoneinfo.ZoneInfo("America/Chicago")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no tz database for America/Chicago on this machine")


def _as_read_back(slot, tz):
    """What the channel reports for a scheduled slot: its UTC publishAt, read back as local time at its own instant
    (youtube._publish_at_string, then youtube.reserved_publish_datetimes)."""
    from lyricvideo.youtube import _publish_at_string

    utc = datetime.fromisoformat(_publish_at_string(slot).replace(".0Z", "+00:00"))
    return utc.astimezone(tz)


def test_a_slot_past_the_november_change_publishes_at_its_configured_local_time():
    tz = _chicago()
    now = datetime(2026, 10, 31, 22, 0, tzinfo=tz)            # still daylight time (-05:00)

    slot = compute_next_publish_slot(now, [], [time(9, 0)], tz=tz)

    assert slot.astimezone(timezone.utc) == datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)   # 09:00 CST, not 08:00
    assert _as_read_back(slot, tz).hour == 9


def test_uploads_across_the_november_change_each_get_their_own_slot():
    """The review's reproduction: before the fix all of these piled onto ONE moment (Nov 1 08:00 CST)."""
    tz = _chicago()
    now = datetime(2026, 10, 31, 22, 0, tzinfo=tz)
    times = parse_upload_times("09:00,12:00,15:00,18:00,21:00")
    claims = []

    for _ in range(7):
        slot = compute_next_publish_slot(now, claims, times, tz=tz)
        claims.append(_as_read_back(slot, tz))

    assert len(set(claims)) == 7
    assert [f"{c:%m-%d %H:%M}" for c in claims] == [
        "11-01 09:00", "11-01 12:00", "11-01 15:00", "11-01 18:00", "11-01 21:00", "11-02 09:00", "11-02 12:00",
    ]


def test_uploads_across_the_march_change_each_get_their_own_slot():
    tz = _chicago()
    now = datetime(2027, 3, 13, 22, 0, tzinfo=tz)             # still standard time (-06:00)
    times = parse_upload_times("09:00,12:00,15:00,18:00,21:00")
    claims = []

    for _ in range(5):
        slot = compute_next_publish_slot(now, claims, times, tz=tz)
        claims.append(_as_read_back(slot, tz))

    assert [f"{c:%m-%d %H:%M}" for c in claims] == ["03-14 09:00", "03-14 12:00", "03-14 15:00", "03-14 18:00", "03-14 21:00"]
    assert claims[0].astimezone(timezone.utc).hour == 14      # 09:00 CDT, not 10:00


def test_the_default_machine_zone_path_also_localizes_each_date():
    """The app's own call (no tz): this machine's zone. Only meaningful where that zone changes offset at the start of
    November 2026 (e.g. any US zone); skipped elsewhere."""
    before, after = datetime(2026, 10, 31, 12, 0).astimezone(), datetime(2026, 11, 2, 12, 0).astimezone()
    if before.utcoffset() == after.utcoffset():
        pytest.skip("this machine's time zone has no daylight-saving change in early November 2026")
    now = datetime(2026, 10, 31, 22, 0).astimezone()

    slots = []
    for _ in range(3):
        slot = compute_next_publish_slot(now, [s.astimezone(timezone.utc).astimezone() for s in slots], [time(9, 0)])
        slots.append(slot)

    assert [s.astimezone().hour for s in slots] == [9, 9, 9]           # every one at 09:00 local, none at 08:00
    assert len({s.date() for s in slots}) == 3


def test_several_videos_at_one_moment_each_use_up_a_slot():
    """A pile-up already on the channel (e.g. from before the fix) must make its day look full, not like one video."""
    times = [time(9, 0), time(12, 0)]
    now = datetime(2026, 11, 1, 7, 0, 0)
    claims = [datetime(2026, 11, 1, 8, 0, 0)] * 2               # two videos at the same off-slot moment, still ahead

    assert compute_next_publish_slot(now, claims, times) == datetime(2026, 11, 2, 9, 0, 0)


# --- EASY CHORD checks run before any paid call, and compare keys as keys (issue #7 review, F086) ---------------------

class _CountingAnthropicClient(_FakeAnthropicClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        real_create = self.messages.create

        def create(**kwargs):
            self.calls += 1
            return real_create(**kwargs)

        self.messages.create = create


def test_a_stale_easy_chord_version_is_refused_before_the_paid_description_call(tmp_path):
    from lyricvideo.youtube_schedule import EasyChordVersionStale

    work_dir = _make_capo_song_work_dir(tmp_path)
    save_decision(work_dir, KeyDecision(status="confirmed", key="F major", source="owner", chord_key="Eb major"))
    claude = _CountingAnthropicClient()
    client = _FakeYoutubeClient(video_id="vid123")

    with pytest.raises(EasyChordVersionStale, match="make the EASY CHORD version again"):
        schedule_upload(client, claude, work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    assert claude.calls == 0
    assert client._videos.insert_kwargs is None


def test_an_easy_chord_marker_spelling_the_same_key_differently_still_uploads(tmp_path):
    """A marker written before 2026-09-26 with prefer_flats off says "D# major"; the settled key says "Eb major"."""
    work_dir = _make_capo_song_work_dir(tmp_path)
    save_easy_chord_capo_marker(work_dir, capo_fret=1, shape_key="D", original_key="D# major",
                                original_title="Bridge Over Troubled Water")
    client = _FakeYoutubeClient(video_id="vid123")

    assert schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS)) == "vid123"
    assert "(original key: Eb major)" in client._videos.insert_kwargs["body"]["snippet"]["description"]


def _make_nested_easy_chord_dir(tmp_path, parent_chords, easy_chords, capo=1):
    from lyricvideo.models import ChordEvent, ChordTrack

    def track(labels, key):
        return ChordTrack(key=key, events=[ChordEvent(start=float(i), end=float(i + 1), label=lab)
                                           for i, lab in enumerate(labels)])

    song_dir = tmp_path / "some-song"
    song_dir.mkdir()
    save_song(Song(title="Some Song", audio_path="a.mp3", lines=[LyricLine(words=[Word(word="la")])],
                   chord_track=track(parent_chords, "Eb major")), song_dir / "lyrics_timed.json")
    save_decision(song_dir, KeyDecision(status="confirmed", key="Eb major", source="agreed", chord_key="Eb major"))
    easy_dir = song_dir / "easychords"
    easy_dir.mkdir()
    save_song(Song(title="Some Song EasyChords", audio_path="a.mp3", lines=[LyricLine(words=[Word(word="la")])],
                   chord_track=track(easy_chords, "D major")), easy_dir / "lyrics_timed.json")
    (easy_dir / "some-song-easychords.mp4").write_bytes(b"fake video bytes")
    save_easy_chord_capo_marker(easy_dir, capo_fret=capo, shape_key="D", original_key="Eb major",
                                original_title="Some Song")
    return easy_dir


def test_a_nested_easy_chord_version_whose_chords_match_the_song_uploads(tmp_path):
    from lyricvideo.youtube_schedule import easy_chord_upload_problem

    easy_dir = _make_nested_easy_chord_dir(tmp_path, ["Eb", "Ab", "Bb"], ["D", "G", "A"])
    client = _FakeYoutubeClient(video_id="vid9")

    assert easy_chord_upload_problem(easy_dir) == ""
    assert schedule_upload(client, _FakeAnthropicClient(), easy_dir, SimpleNamespace(**_PUBLIC_SETTINGS)) == "vid9"


def test_a_nested_easy_chord_version_whose_song_was_redone_with_other_chords_is_refused(tmp_path):
    from lyricvideo.youtube_schedule import EasyChordVersionStale, easy_chord_upload_problem

    easy_dir = _make_nested_easy_chord_dir(tmp_path, ["Eb", "Cm", "Bb"], ["D", "G", "A"])   # the song's chords changed
    claude = _CountingAnthropicClient()
    client = _FakeYoutubeClient(video_id="vid9")

    assert "no longer the song's own chords" in easy_chord_upload_problem(easy_dir)
    with pytest.raises(EasyChordVersionStale):
        schedule_upload(client, claude, easy_dir, SimpleNamespace(**_PUBLIC_SETTINGS))
    assert claude.calls == 0 and client._videos.insert_kwargs is None


def test_easy_chord_upload_problem_is_blank_for_an_ordinary_song(tmp_path):
    from lyricvideo.youtube_schedule import easy_chord_upload_problem

    assert easy_chord_upload_problem(_make_song_work_dir(tmp_path)) == ""


# --- a title YouTube would refuse never reaches YouTube (issue #7 review, F142) ----------------------------------------

def test_a_very_long_song_title_is_fitted_to_youtubes_limit(tmp_path):
    work_dir = _make_song_work_dir(tmp_path, artist="Some Very Long Band Name Indeed")
    song = Song(title="An Extremely Long Made Up Song Title That Keeps Going On And On Past Any Limit",
                audio_path="song.mp3", lines=[LyricLine(words=[Word(word="la")])])
    save_song(song, work_dir / "lyrics_timed.json")
    from lyricvideo.pipeline import slugify

    (work_dir / f"{slugify(song.title)}.mp4").write_bytes(b"fake")
    client = _FakeYoutubeClient(video_id="vid1")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    title = client._videos.insert_kwargs["body"]["snippet"]["title"]
    assert len(title) <= 100 and title.endswith("(Play Along Lyrics & Chords)")


# --- uploads never overlap, and a pending retry never re-sends a finished song -----------------------------------------

def test_a_pending_retry_does_not_resend_a_song_uploaded_meanwhile(tmp_path):
    from lyricvideo.youtube_schedule import AlreadyUploaded
    from lyricvideo.youtube_state import YoutubeState, save_youtube_state

    work_dir = _make_song_work_dir(tmp_path)
    save_youtube_state(work_dir, YoutubeState(video_id="first", uploaded_at="2026-09-26T10:00:00", title="t"))
    claude = _CountingAnthropicClient()
    client = _FakeYoutubeClient(video_id="second")

    with pytest.raises(AlreadyUploaded):
        schedule_upload(client, claude, work_dir, SimpleNamespace(**_PUBLIC_SETTINGS), only_if_not_uploaded=True)

    assert client._videos.insert_kwargs is None and claude.calls == 0
    # the owner's deliberate re-upload (the default) still goes through
    assert schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS)) == "second"


def test_two_uploads_at_once_run_one_after_the_other(tmp_path):
    import threading
    import time as _time

    from lyricvideo.youtube_schedule import upload_in_progress

    active, overlaps, seen_in_progress = [0], [0], []

    class _SlowRequest:
        def next_chunk(self):
            active[0] += 1
            overlaps[0] = max(overlaps[0], active[0])
            seen_in_progress.append(upload_in_progress())
            _time.sleep(0.05)
            active[0] -= 1
            return None, {"id": "v"}

    class _Videos(_FakeVideosResource):
        def insert(self, **kwargs):
            self.insert_kwargs = kwargs
            return _SlowRequest()

    dirs = []
    for i in range(3):
        root = tmp_path / f"r{i}"
        root.mkdir()
        dirs.append(_make_song_work_dir(root))

    def run(work_dir):
        client = _FakeYoutubeClient()
        client._videos = _Videos("v")
        schedule_upload(client, _FakeAnthropicClient(), work_dir, SimpleNamespace(**_PUBLIC_SETTINGS))

    threads = [threading.Thread(target=run, args=(d,)) for d in dirs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps[0] == 1                 # never two uploads in flight at once
    assert seen_in_progress == [True, True, True]
    assert upload_in_progress() is False


# --- a key-correction note lands right after the description through a support-template re-render (owner, 2026-09-29,
# reversing the 2026-09-26 "note first" decision tested here before -- issue #7 review, F054's no-duplication
# guarantee still holds, just at the new position) --------------------------------------------------------------------

def test_rerendering_moves_the_key_note_after_the_description_and_is_stable():
    from lyricvideo.key_note import apply_key_note
    from lyricvideo.youtube_schedule import rerender_description

    note = "📌 Song key: C minor (not D minor as shown in the video)"
    front = apply_key_note(render_description(_TEMPLATE, "Body."), "C minor", "D minor")   # apply_key_note just prepends

    once = rerender_description(front, _TEMPLATE, [_OLD])

    assert once == f"{_TOP}\n\nBody.\n\n{note}\n\n{_BOTTOM}"
    assert rerender_description(once, _TEMPLATE, [_OLD]) == once           # stable once it's in the new position
    assert once.count("📌") == 1


def test_rerendering_moves_a_misplaced_note_to_right_after_the_description():
    from lyricvideo.youtube_schedule import rerender_description

    note = "📌 Song key: C minor (not D minor as shown in the video)"
    pushed_up_top = f"{note}\n\n{_TOP}\n\nBody.\n\n{_BOTTOM}"

    assert rerender_description(pushed_up_top, _TEMPLATE, [_OLD]) == f"{_TOP}\n\nBody.\n\n{note}\n\n{_BOTTOM}"


def test_rerendering_a_description_without_a_note_is_unchanged_behavior():
    from lyricvideo.youtube_schedule import rerender_description

    assert rerender_description(f"A song.\n\n{_OLD}", _TEMPLATE, [_OLD]) == render_description(_TEMPLATE, "A song.")


def test_a_template_whose_top_starts_with_a_pin_is_not_taken_for_a_key_note():
    # Review follow-up: only "📌 Song key:" / "📌 Correction" paragraphs are key notes. A support template whose top line
    # happens to start with 📌 must re-render unchanged -- never lifted out and then rendered a second time.
    from lyricvideo.key_note import apply_key_note
    from lyricvideo.youtube_schedule import rerender_description

    pinned_template = "📌 Tip jar: https://example.invalid/tip\n\n{description}\n\nThanks!"
    rendered = render_description(pinned_template, "Body.")

    assert rerender_description(rendered, pinned_template, []) == rendered
    note = "📌 Song key: C minor (not D minor as shown in the video)"
    noted = apply_key_note(rendered, "C minor", "D minor")
    assert noted.startswith("📌 Song key: C minor") and "📌 Tip jar" in noted
    assert rerender_description(noted, pinned_template, []) == (
        f"📌 Tip jar: https://example.invalid/tip\n\nBody.\n\n{note}\n\nThanks!"
    )
