import queue
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lyricvideo.gui import (
    PROJECT_ROOT, _default_work_dir_from_audio, _maybe_upload_to_youtube, _retry_pending_uploads,
    _slugify, _split_log_text,
)
from lyricvideo.settings import Settings
from lyricvideo.youtube_state import YoutubeState, load_youtube_state, save_youtube_state


@pytest.fixture(autouse=True)
def _no_quota_block_by_default(monkeypatch):
    # Isolates every test in this file from whatever's actually on disk at
    # ~/.playalongvideoproduction/youtube_quota_state.json -- a test that
    # specifically wants a quota block in effect overrides this with its
    # own later monkeypatch.setattr call on the same name.
    monkeypatch.setattr("lyricvideo.gui.load_quota_blocked_until", lambda: None)


@pytest.fixture(autouse=True)
def _no_upload_cap_by_default(monkeypatch):
    # Isolates every test in this file from whatever's actually on disk at
    # ~/.playalongvideoproduction/youtube_upload_count.json, and from ever
    # writing to it -- a test that specifically wants to exercise the daily
    # upload cap overrides load_uploads_today with its own later
    # monkeypatch.setattr call on the same name.
    monkeypatch.setattr("lyricvideo.gui.load_uploads_today", lambda: 0)
    monkeypatch.setattr("lyricvideo.gui.record_upload", lambda: None)


def test_maybe_upload_to_youtube_skips_when_auto_upload_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.youtube_auth.load_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("should not check credentials when disabled")),
    )
    settings = Settings(youtube_auto_upload=False)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_skips_when_not_connected(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: None)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: calls.append(True) or (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise

    assert calls == []


def test_maybe_upload_to_youtube_skips_when_already_uploaded_and_still_live(tmp_path, monkeypatch):
    save_youtube_state(tmp_path, YoutubeState(video_id="abc", uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.video_exists", lambda client, video_id: True)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not re-upload a still-live video")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_re_uploads_when_saved_video_was_deleted_on_youtube(tmp_path, monkeypatch):
    """Real incident 2026-09-10: the owner deleted "Come As You Are" directly
    on YouTube after a redo; the stale local youtube_state.json kept the
    song permanently stuck as "already uploaded" with no way to recover
    short of manually deleting the JSON file. video_exists() now catches
    this and lets the real upload proceed."""
    save_youtube_state(tmp_path, YoutubeState(video_id="deleted-id", uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.video_exists", lambda client, video_id: False)
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [tmp_path]


def test_maybe_upload_to_youtube_skips_when_verification_itself_fails(tmp_path, monkeypatch):
    """A network hiccup while checking video_exists must fail CLOSED (skip,
    not upload) -- never risk a duplicate video because a status check
    happened to time out."""
    save_youtube_state(tmp_path, YoutubeState(video_id="abc", uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")

    def _raise(client, video_id):
        raise RuntimeError("network hiccup")

    monkeypatch.setattr("lyricvideo.gui.video_exists", _raise)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload when verification failed")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_calls_schedule_upload_when_eligible(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(
            (youtube_client, anthropic_client, work_dir, settings)
        ),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [("fake-youtube-client", "fake-anthropic-client", tmp_path, settings)]


def test_maybe_upload_to_youtube_skips_a_song_flagged_for_lyrics_review(tmp_path, monkeypatch):
    from lyricvideo.models import Song, save_song

    save_song(
        Song(title="t", audio_path="a.mp3", lyrics_accuracy_concern="looks like the wrong song"),
        tmp_path / "lyrics_timed.json",
    )
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    calls = []
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: calls.append("build") or "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: calls.append("schedule_upload"))
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == []  # never even got as far as building a YouTube client


def test_maybe_upload_to_youtube_uploads_a_song_with_no_concern(tmp_path, monkeypatch):
    from lyricvideo.models import Song, save_song

    save_song(Song(title="t", audio_path="a.mp3"), tmp_path / "lyrics_timed.json")
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [tmp_path]


def test_maybe_upload_to_youtube_never_raises_on_upload_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", _raise)
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_skips_when_still_in_quota_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload during a quota cooldown")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_proceeds_once_quota_cooldown_has_expired(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() - timedelta(hours=1),
    )
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [tmp_path]


def test_maybe_upload_to_youtube_saves_a_quota_cooldown_on_a_quota_exceeded_error(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr("lyricvideo.gui.is_quota_exceeded_error", lambda e: True)
    saved = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: saved.append(dt))
    settings = Settings(youtube_auto_upload=True, youtube_quota_retry_hours=6)

    before = datetime.now().astimezone()
    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise

    assert len(saved) == 1
    assert before + timedelta(hours=5, minutes=59) < saved[0] < before + timedelta(hours=6, minutes=1)


def test_maybe_upload_to_youtube_stops_retrying_after_a_real_upload_limit_exceeded_error(tmp_path, monkeypatch):
    """Regression, 2026-09-19: every other test here stubs is_quota_exceeded_error
    to `lambda e: True`, so none noticed that the real predicate never recognized
    YouTube's actual HTTP 400 uploadLimitExceeded -- no cooldown was recorded and
    every following song in a Batch attempted (and failed) its own upload. This
    one uses the real error and the real predicate, across two songs."""
    from types import SimpleNamespace

    from googleapiclient.errors import ResumableUploadError

    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.load_uploads_today", lambda: 0)
    cooldown = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: cooldown.append(dt))
    monkeypatch.setattr("lyricvideo.gui.load_quota_blocked_until", lambda: cooldown[-1] if cooldown else None)
    error = ResumableUploadError(
        SimpleNamespace(status=400, reason="Bad Request"),
        b'{"error": {"code": 400, "message": "The user has exceeded the number of videos they may upload.", '
        b'"errors": [{"domain": "youtube.video", "reason": "uploadLimitExceeded"}]}}',
    )
    attempts = []

    def failing_upload(youtube_client, anthropic_client, work_dir, settings):
        attempts.append(work_dir)
        raise error

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", failing_upload)
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path / "song-one", settings)
    _maybe_upload_to_youtube(tmp_path / "song-two", settings)

    assert attempts == [tmp_path / "song-one"]  # song-two must be skipped by the recorded cooldown
    assert len(cooldown) == 1


def test_maybe_upload_to_youtube_skips_when_todays_upload_cap_is_reached(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.load_uploads_today", lambda: 3)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload once today's cap is reached")),
    )
    settings = Settings(youtube_auto_upload=True, youtube_max_uploads_per_day=3)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_proceeds_when_todays_upload_cap_still_has_room(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.load_uploads_today", lambda: 2)
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings(youtube_auto_upload=True, youtube_max_uploads_per_day=3)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [tmp_path]


def test_maybe_upload_to_youtube_records_the_upload_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    recorded = []
    monkeypatch.setattr("lyricvideo.gui.record_upload", lambda: recorded.append(True))
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert recorded == [True]


def test_maybe_upload_to_youtube_calls_organize_video_after_a_successful_upload(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.organize_video",
        lambda youtube_client, anthropic_client, work_dir: calls.append((youtube_client, work_dir)),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [("fake-youtube-client", tmp_path)]


def test_maybe_upload_to_youtube_survives_organize_video_failure(tmp_path, monkeypatch):
    """The video itself is the important part -- a playlist/comment
    organization failure must never make an otherwise-successful upload
    look like it failed."""
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.organize_video", _raise)
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_saves_a_quota_cooldown_when_organize_video_hits_quota(tmp_path, monkeypatch):
    """A fresh quota-exceeded error discovered while organizing playlists
    must engage the same global cooldown as one discovered during the
    upload itself (2026-09-18) -- otherwise every other song's own
    organize_video call keeps hammering the API right alongside it."""
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    monkeypatch.setattr(
        "lyricvideo.gui.organize_video",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr("lyricvideo.gui.is_quota_exceeded_error", lambda e: True)
    saved = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: saved.append(dt))
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise

    assert len(saved) == 1


def test_retry_pending_uploads_uploads_every_pending_song(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings()

    results = _retry_pending_uploads(tmp_path, settings)

    assert calls == [tmp_path / "song-a", tmp_path / "song-b"]
    assert results == {"succeeded": ["song-a", "song-b"], "failed": [], "deferred": []}


def test_retry_pending_uploads_continues_after_a_single_song_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")

    def upload(youtube_client, anthropic_client, work_dir, settings):
        if work_dir.name == "song-a":
            raise RuntimeError("uploadLimitExceeded")

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", upload)
    settings = Settings()

    results = _retry_pending_uploads(tmp_path, settings)

    assert results == {
        "succeeded": ["song-b"],
        "failed": [("song-a", "RuntimeError: uploadLimitExceeded")],
        "deferred": [],
    }


def test_retry_pending_uploads_raises_when_not_connected(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: None)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )

    try:
        _retry_pending_uploads(tmp_path, Settings())
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_retry_pending_uploads_raises_while_still_in_a_quota_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )

    try:
        _retry_pending_uploads(tmp_path, Settings())
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_retry_pending_uploads_force_bypasses_the_quota_cooldown_check(tmp_path, monkeypatch):
    """force=True is how a manual retry-upload trigger proceeds after the
    owner explicitly confirms past a quota-cooldown warning (2026-09-18) --
    see gui.py's _start_retry_upload / _confirm_quota_override_if_blocked."""
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )

    results = _retry_pending_uploads(tmp_path, Settings(), force=True)

    assert calls == [tmp_path / "song-a"]
    assert results["succeeded"] == ["song-a"]


def test_retry_pending_uploads_stops_and_saves_a_cooldown_on_quota_exceeded(tmp_path, monkeypatch):
    """Every remaining song would fail identically right now -- stop after
    the first quota-exceeded failure instead of reporting the same root
    cause for every one of them."""
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b", "song-c"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.is_quota_exceeded_error", lambda e: True)
    saved = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: saved.append(dt))

    def upload(youtube_client, anthropic_client, work_dir, settings):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", upload)

    results = _retry_pending_uploads(tmp_path, Settings())

    assert results["succeeded"] == []
    assert len(results["failed"]) == 1  # only song-a attempted -- b and c never even tried
    assert results["failed"][0][0] == "song-a"
    assert len(saved) == 1


def test_retry_pending_uploads_defers_songs_once_todays_upload_cap_is_reached(tmp_path, monkeypatch):
    """A big "Select All" + Upload Selected must be safe to click without
    blowing through a day's quota in one run (2026-09-18) -- once the cap's
    hit, the rest stay pending for a later day instead of all being
    attempted (and likely all failing on YouTube's own quota error) in one
    go."""
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b", "song-c"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    # A real mutable counter, not a fixed stub -- must reflect that
    # record_upload() actually ran, or every song would wrongly see "0
    # uploaded today" and the cap would never engage mid-loop.
    count = [0]
    monkeypatch.setattr("lyricvideo.gui.load_uploads_today", lambda: count[0])
    monkeypatch.setattr("lyricvideo.gui.record_upload", lambda: count.__setitem__(0, count[0] + 1))
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings(youtube_max_uploads_per_day=1)

    results = _retry_pending_uploads(tmp_path, settings)

    assert calls == [tmp_path / "song-a"]  # only one upload call made, matching the cap
    assert count[0] == 1
    assert results == {"succeeded": ["song-a"], "failed": [], "deferred": ["song-b", "song-c"]}


def test_retry_pending_uploads_uploads_only_seven_of_twenty_pending_songs(tmp_path, monkeypatch):
    """Owner's intended behavior (2026-09-19): 20 songs are ready but the
    Maximum uploads per day is 7 -- exactly 7 upload today, the other 13 stay
    pending for a later day."""
    slugs = [f"song-{i:02d}" for i in range(20)]
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: list(slugs))
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    count = [0]
    monkeypatch.setattr("lyricvideo.gui.load_uploads_today", lambda: count[0])
    monkeypatch.setattr("lyricvideo.gui.record_upload", lambda: count.__setitem__(0, count[0] + 1))
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.gui.organize_video", lambda *a, **k: None)

    results = _retry_pending_uploads(tmp_path, Settings())  # default cap

    assert Settings().youtube_max_uploads_per_day == 7
    assert results["succeeded"] == slugs[:7]
    assert results["deferred"] == slugs[7:]
    assert results["failed"] == []


def test_retry_pending_uploads_defers_everything_when_todays_upload_cap_is_already_used_up(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.load_uploads_today", lambda: 3)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload once today's cap is reached")),
    )
    settings = Settings(youtube_max_uploads_per_day=3)

    results = _retry_pending_uploads(tmp_path, settings)

    assert results == {"succeeded": [], "failed": [], "deferred": ["song-a", "song-b"]}


def test_retry_pending_uploads_only_attempts_the_given_slugs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.list_pending_uploads",
        lambda work_root: (_ for _ in ()).throw(AssertionError("should not list all pending songs")),
    )
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )

    results = _retry_pending_uploads(tmp_path, Settings(), slugs=["song-a"])

    assert calls == [tmp_path / "song-a"]
    assert results == {"succeeded": ["song-a"], "failed": [], "deferred": []}


def test_retry_pending_uploads_calls_organize_video_for_each_succeeded_song(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.organize_video",
        lambda youtube_client, anthropic_client, work_dir: calls.append(work_dir),
    )

    results = _retry_pending_uploads(tmp_path, Settings())

    assert calls == [tmp_path / "song-a"]
    assert results == {"succeeded": ["song-a"], "failed": [], "deferred": []}


def test_retry_pending_uploads_still_succeeds_when_organize_video_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.organize_video", _raise)

    results = _retry_pending_uploads(tmp_path, Settings())

    assert results == {"succeeded": ["song-a"], "failed": [], "deferred": []}


def test_retry_pending_uploads_stops_and_saves_a_cooldown_when_organize_video_hits_quota(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    monkeypatch.setattr(
        "lyricvideo.gui.organize_video",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr("lyricvideo.gui.is_quota_exceeded_error", lambda e: True)
    saved = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: saved.append(dt))

    results = _retry_pending_uploads(tmp_path, Settings())

    assert results["succeeded"] == ["song-a"]  # the upload itself still succeeded
    assert len(saved) == 1  # but song-b was never even attempted


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Wish You Were Here") == "wish-you-were-here"


def test_slugify_strips_punctuation():
    assert _slugify("Turn The Page (Live!)") == "turn-the-page-live"


def test_slugify_collapses_whitespace_and_trims_hyphens():
    assert _slugify("  Some   Song  ") == "some-song"


def test_slugify_falls_back_on_empty_title():
    assert _slugify("") == "untitled-song"
    assert _slugify("   ") == "untitled-song"


def test_default_work_dir_from_audio_uses_the_audio_filenames_own_stem():
    """Real incident, 2026-09-13: Generate clicked before (or despite) the
    GUI's own background title-identification finishing left both the title
    and work-directory fields blank, blocking Generate with no way to
    proceed short of typing something into Title -- since there's no Browse
    button for the work-directory field itself. This fallback (wired into
    _on_generate) means a blank work directory never blocks Generate."""
    result = _default_work_dir_from_audio("/some/path/01 My Song.mp3")
    assert result == str(PROJECT_ROOT / "work" / "01-my-song")


def test_default_work_dir_from_audio_matches_title_based_slug_convention():
    """Uses the exact same slugify() call _on_title_changed already uses for
    a real identified title, just fed the filename stem instead."""
    assert _default_work_dir_from_audio("/x/Some Song.mp3") == str(
        PROJECT_ROOT / "work" / _slugify("Some Song")
    )


def test_split_log_text_plain_text_with_no_newline_stays_pending():
    pending, to_commit = _split_log_text("", "loading")
    assert pending == "loading"
    assert to_commit == ""


def test_split_log_text_newline_commits_the_line():
    pending, to_commit = _split_log_text("", "done\n")
    assert pending == ""
    assert to_commit == "done\n"


def test_split_log_text_accumulates_across_calls_until_newline():
    pending, to_commit = _split_log_text("load", "ing")
    assert pending == "loading"
    assert to_commit == ""
    pending, to_commit = _split_log_text(pending, "...\n")
    assert pending == ""
    assert to_commit == "loading...\n"


def test_split_log_text_carriage_return_discards_pending_line():
    # tqdm-style: prints a whole new percentage after \r, not an appendix to
    # the old one -- \r must throw away whatever was pending, not keep it.
    pending, to_commit = _split_log_text("50%", "\r75%")
    assert pending == "75%"
    assert to_commit == ""


def test_split_log_text_progress_bar_burst_collapses_to_one_committed_line():
    # A realistic tqdm burst: many \r-separated updates in one chunk, then a
    # final newline -- only the LAST update should ever get committed.
    text = "\r10%|#|\r50%|#####|\r100%|##########|\n"
    pending, to_commit = _split_log_text("", text)
    assert pending == ""
    assert to_commit == "100%|##########|\n"


def test_split_log_text_multiple_real_lines_all_committed():
    pending, to_commit = _split_log_text("", "line one\nline two\n")
    assert pending == ""
    assert to_commit == "line one\nline two\n"


def test_split_log_text_empty_chunk_is_a_noop():
    pending, to_commit = _split_log_text("partial", "")
    assert pending == "partial"
    assert to_commit == ""


# --- GUI worker error paths ------------------------------------------------
# These drive real LyricVideoGUI methods against a plain stub `self` (no Tk
# window) with threading.Thread swapped for a synchronous stand-in, so the
# callbacks a worker schedules via root.after() can be asserted on directly.

from types import SimpleNamespace  # noqa: E402

from lyricvideo.batch import BatchItem  # noqa: E402
from lyricvideo.gui import LyricVideoGUI  # noqa: E402
from lyricvideo.youtube_comment_state import PendingComment, PendingReply  # noqa: E402


class _ImmediateThread:
    """Stands in for threading.Thread: runs the target synchronously on
    start(), so a worker's after()-scheduled callbacks fire before the test
    asserts."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _ImmediateRoot:
    """A root whose after() runs the callback immediately -- the exact call
    site where the deferred error-dialog lambdas below used to raise."""

    def after(self, _delay, callback, *args):
        callback(*args)


def _gui_stub(**attrs):
    attrs.setdefault("settings", Settings())
    # Default: no quota cooldown in effect, so every existing test's manual
    # action proceeds exactly as before this gate was added (2026-09-18). A
    # test exercising the gate itself overrides this attribute directly.
    attrs.setdefault("_confirm_quota_override_if_blocked", lambda: True)
    return SimpleNamespace(root=_ImmediateRoot(), **attrs)


def _must_not_run(*args, **kwargs):
    raise AssertionError("this must not be reached")


def test_connect_youtube_failure_shows_the_error_dialog(monkeypatch):
    """Real latent bug (static analysis, 2026-09-14): the worker's except
    block handed `e` to a lambda that root.after() ran later -- but Python
    unbinds `e` the moment the except block ends, so that lambda raised
    NameError inside the Tk event loop and the owner never saw why the
    connect failed. Same bug in the manual-upload and Approve workers."""
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)

    def failing_connect(path):
        raise RuntimeError("consent screen closed")

    monkeypatch.setattr("lyricvideo.gui.youtube_auth.connect", failing_connect)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub(settings=Settings(youtube_client_secrets_path="secrets.json"))
    LyricVideoGUI._on_connect_youtube(stub)

    assert shown == [("Could not connect to YouTube", "RuntimeError: consent screen closed")]


def test_connect_youtube_success_refreshes_the_status_label(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.connect", lambda path: "credentials")
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", _must_not_run)
    refreshed = []

    stub = _gui_stub(
        settings=Settings(youtube_client_secrets_path="secrets.json"),
        _refresh_youtube_status=lambda: refreshed.append(True),
    )
    LyricVideoGUI._on_connect_youtube(stub)

    assert refreshed == [True]


def test_approve_reply_failure_shows_the_error_dialog_and_keeps_the_draft(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")

    def failing_post(client, comment_id, text):
        raise RuntimeError("commentsDisabled")

    monkeypatch.setattr("lyricvideo.gui.post_reply", failing_post)
    monkeypatch.setattr("lyricvideo.gui.remove_pending_reply", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    reply = PendingReply(
        comment_id="c1", video_id="v1", author="fan", comment_text="wrong chord at 1:02?",
        draft_reply="Thanks!", is_error_report=True,
    )
    text_box = SimpleNamespace(get=lambda start, end: "Thanks for the heads-up!\n")
    LyricVideoGUI._on_approve_reply(_gui_stub(), reply, text_box)

    assert shown == [("Could not post reply", "RuntimeError: commentsDisabled")]


def test_mark_engagement_comment_posted_updates_the_matching_song(tmp_path, monkeypatch):
    from lyricvideo.gui import _mark_engagement_comment_posted

    work_dir = tmp_path / "work" / "my-song"
    work_dir.mkdir(parents=True)
    save_youtube_state(work_dir, YoutubeState(video_id="vid123", uploaded_at="2026-09-10T15:00:00", title="t"))
    other_dir = tmp_path / "work" / "other-song"
    other_dir.mkdir(parents=True)
    save_youtube_state(other_dir, YoutubeState(video_id="vid999", uploaded_at="2026-09-10T15:00:00", title="t2"))
    monkeypatch.setattr("lyricvideo.gui.PROJECT_ROOT", tmp_path)

    _mark_engagement_comment_posted("vid123")

    assert load_youtube_state(work_dir).engagement_comment_posted is True
    assert load_youtube_state(other_dir).engagement_comment_posted is False


def test_approve_comment_posts_marks_posted_and_removes_from_queue(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.is_video_public", lambda *a, **k: True)
    posted = []
    monkeypatch.setattr(
        "lyricvideo.gui.post_top_level_comment",
        lambda client, video_id, text: posted.append((video_id, text)) or "new-comment-id",
    )
    removed = []
    monkeypatch.setattr("lyricvideo.gui.remove_pending_comment", lambda video_id: removed.append(video_id))
    marked = []
    monkeypatch.setattr("lyricvideo.gui._mark_engagement_comment_posted", lambda video_id: marked.append(video_id))
    monkeypatch.setattr("lyricvideo.gui.messagebox.showinfo", lambda *a, **k: None)
    invalidated = []

    comment = PendingComment(video_id="vid123", song_title="My Song", draft_text="Which instrument?")
    text_box = SimpleNamespace(get=lambda start, end: "Which instrument are you playing?\n")
    stub = _gui_stub(_invalidate_pending_comments=lambda: invalidated.append(True))
    LyricVideoGUI._on_approve_comment(stub, comment, text_box)

    assert posted == [("vid123", "Which instrument are you playing?")]
    assert removed == ["vid123"]
    assert marked == ["vid123"]
    assert invalidated == [True]


def test_approve_comment_skips_posting_while_the_video_is_still_private(monkeypatch):
    """A just-scheduled video is private until its own publishAt -- YouTube
    refuses commentThreads.insert on it the same way it refuses reads
    (is_video_public's own docstring), so Approve must check first instead
    of surfacing a raw 403 HttpError (real 2026-09-18 bug)."""
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.is_video_public", lambda *a, **k: False)
    monkeypatch.setattr("lyricvideo.gui.post_top_level_comment", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui.remove_pending_comment", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showinfo", lambda title, msg: shown.append((title, msg)))

    comment = PendingComment(video_id="vid123", song_title="My Song", draft_text="Which instrument?")
    text_box = SimpleNamespace(get=lambda start, end: "Which instrument are you playing?\n")
    LyricVideoGUI._on_approve_comment(_gui_stub(), comment, text_box)

    assert shown == [("Video not public yet", shown[0][1])]
    assert "still scheduled/private" in shown[0][1]


def test_approve_comment_failure_shows_the_error_dialog_and_keeps_the_draft(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.is_video_public", lambda *a, **k: True)

    def failing_post(client, video_id, text):
        raise RuntimeError("commentsDisabled")

    monkeypatch.setattr("lyricvideo.gui.post_top_level_comment", failing_post)
    monkeypatch.setattr("lyricvideo.gui.remove_pending_comment", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    comment = PendingComment(video_id="vid123", song_title="My Song", draft_text="Which instrument?")
    text_box = SimpleNamespace(get=lambda start, end: "Which instrument are you playing?\n")
    LyricVideoGUI._on_approve_comment(_gui_stub(), comment, text_box)

    assert shown == [("Could not post comment", "RuntimeError: commentsDisabled")]


def test_dismiss_comment_removes_from_queue_and_invalidates(monkeypatch):
    removed = []
    monkeypatch.setattr("lyricvideo.gui.remove_pending_comment", lambda video_id: removed.append(video_id))
    invalidated = []

    comment = PendingComment(video_id="vid123", song_title="My Song", draft_text="Which instrument?")
    stub = _gui_stub(_invalidate_pending_comments=lambda: invalidated.append(True))
    LyricVideoGUI._on_dismiss_comment(stub, comment)

    assert removed == ["vid123"]
    assert invalidated == [True]


def test_retry_pending_uploads_if_due_skips_while_generate_redo_or_batch_is_running(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", _must_not_run)
    stub = _gui_stub(_running=True, settings=Settings(youtube_auto_upload=True))

    LyricVideoGUI._retry_pending_uploads_if_due(stub)  # must not raise


def test_retry_pending_uploads_if_due_skips_when_auto_upload_is_off(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", _must_not_run)
    stub = _gui_stub(_running=False, settings=Settings(youtube_auto_upload=False))

    LyricVideoGUI._retry_pending_uploads_if_due(stub)  # must not raise


def test_retry_pending_uploads_if_due_skips_when_not_connected(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: None)
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", _must_not_run)
    stub = _gui_stub(_running=False, settings=Settings(youtube_auto_upload=True))

    LyricVideoGUI._retry_pending_uploads_if_due(stub)  # must not raise


def test_retry_pending_uploads_if_due_skips_during_a_quota_cooldown(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", _must_not_run)
    stub = _gui_stub(_running=False, settings=Settings(youtube_auto_upload=True))

    LyricVideoGUI._retry_pending_uploads_if_due(stub)  # must not raise


def test_retry_pending_uploads_if_due_skips_when_nothing_is_pending(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: [])
    monkeypatch.setattr("lyricvideo.gui.load_dismissed", lambda list_name: set())
    monkeypatch.setattr("lyricvideo.gui._retry_pending_uploads", _must_not_run)
    stub = _gui_stub(_running=False, settings=Settings(youtube_auto_upload=True))

    LyricVideoGUI._retry_pending_uploads_if_due(stub)  # must not raise


def test_retry_pending_uploads_if_due_retries_pending_songs_excluding_dismissed_ones(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.load_dismissed", lambda list_name: {"song-b"})
    monkeypatch.setattr("lyricvideo.gui.list_flagged_songs", lambda work_root: [])
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui._retry_pending_uploads",
        lambda work_root, settings, slugs: calls.append(slugs),
    )
    refreshed = []
    stub = _gui_stub(
        _running=False, settings=Settings(youtube_auto_upload=True),
        _refresh_retry_upload_options=lambda: refreshed.append(True),
    )

    LyricVideoGUI._retry_pending_uploads_if_due(stub)

    assert calls == [["song-a"]]
    assert refreshed == [True]


def test_retry_pending_uploads_if_due_excludes_flagged_songs(monkeypatch):
    """A song flagged for lyrics review must never auto-retry -- only a
    deliberate owner click (Upload Anyway) uploads one of these."""
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.load_dismissed", lambda list_name: set())
    monkeypatch.setattr("lyricvideo.gui.list_flagged_songs", lambda work_root: ["song-b"])
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui._retry_pending_uploads",
        lambda work_root, settings, slugs: calls.append(slugs),
    )
    stub = _gui_stub(
        _running=False, settings=Settings(youtube_auto_upload=True),
        _refresh_retry_upload_options=lambda: None,
    )

    LyricVideoGUI._retry_pending_uploads_if_due(stub)

    assert calls == [["song-a"]]


def test_confirm_quota_override_returns_true_without_a_prompt_when_not_blocked(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.load_quota_blocked_until", lambda: None)
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", _must_not_run)

    assert LyricVideoGUI._confirm_quota_override_if_blocked(_gui_stub()) is True


def test_confirm_quota_override_prompts_and_honors_a_no(monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", lambda title, msg: False)

    assert LyricVideoGUI._confirm_quota_override_if_blocked(_gui_stub()) is False


def test_confirm_quota_override_prompts_and_honors_a_yes(monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", lambda title, msg: True)

    assert LyricVideoGUI._confirm_quota_override_if_blocked(_gui_stub()) is True


def test_start_retry_upload_aborts_without_uploading_when_the_owner_declines_the_prompt(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui._retry_pending_uploads", _must_not_run)
    stub = _gui_stub(_confirm_quota_override_if_blocked=lambda: False)

    LyricVideoGUI._start_retry_upload(stub, ["song-a"])  # must not raise, must not start a thread


def test_check_youtube_comments_aborts_when_the_owner_declines_the_prompt(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    stub = _gui_stub(_confirm_quota_override_if_blocked=lambda: False)

    LyricVideoGUI._on_check_youtube_comments(stub)  # must not raise, must not start a thread


def test_check_youtube_comments_proceeds_when_the_owner_confirms(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: None)
    ran = []
    stub = _gui_stub(
        _confirm_quota_override_if_blocked=lambda: True,
        _check_youtube_comments_worker=lambda: ran.append(True),
    )

    LyricVideoGUI._on_check_youtube_comments(stub)

    assert ran == [True]


def test_approve_reply_aborts_without_posting_when_the_owner_declines_the_prompt(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui.post_reply", _must_not_run)
    stub = _gui_stub(_confirm_quota_override_if_blocked=lambda: False)
    reply = PendingReply(
        comment_id="c1", video_id="v1", author="fan", comment_text="wrong chord at 1:02?",
        draft_reply="Thanks!", is_error_report=True,
    )
    text_box = SimpleNamespace(get=lambda start, end: "Thanks!\n")

    LyricVideoGUI._on_approve_reply(stub, reply, text_box)  # must not raise, must not post


def test_approve_comment_aborts_without_posting_when_the_owner_declines_the_prompt(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui.post_top_level_comment", _must_not_run)
    stub = _gui_stub(_confirm_quota_override_if_blocked=lambda: False)
    comment = PendingComment(video_id="v1", song_title="Song", draft_text="Thanks for watching!")
    text_box = SimpleNamespace(get=lambda start, end: "Thanks for watching!\n")

    LyricVideoGUI._on_approve_comment(stub, comment, text_box)  # must not raise, must not post


def test_approve_reply_saves_a_quota_cooldown_on_a_fresh_quota_exceeded_error(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr(
        "lyricvideo.gui.post_reply",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr("lyricvideo.gui.is_quota_exceeded_error", lambda e: True)
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: None)
    saved = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: saved.append(dt))
    stub = _gui_stub()
    reply = PendingReply(
        comment_id="c1", video_id="v1", author="fan", comment_text="wrong chord at 1:02?",
        draft_reply="Thanks!", is_error_report=True,
    )
    text_box = SimpleNamespace(get=lambda start, end: "Thanks!\n")

    LyricVideoGUI._on_approve_reply(stub, reply, text_box)

    assert len(saved) == 1


def test_youtube_status_text_skips_the_api_call_while_quota_blocked(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now(timezone.utc) + timedelta(hours=6),  # was a hardcoded date that expired 2026-09-19
    )
    monkeypatch.setattr(
        "lyricvideo.gui.youtube_auth.get_channel_title",
        _must_not_run,
    )
    stub = _gui_stub()

    text = LyricVideoGUI._youtube_status_text(stub)

    assert text.startswith("YouTube: quota exceeded")


def test_youtube_status_text_saves_a_cooldown_on_a_fresh_quota_exceeded_error(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.load_quota_blocked_until", lambda: None)
    monkeypatch.setattr(
        "lyricvideo.gui.youtube_auth.get_channel_title",
        lambda creds: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr("lyricvideo.gui.is_quota_exceeded_error", lambda e: True)
    saved = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: saved.append(dt))
    stub = _gui_stub(settings=Settings(youtube_quota_retry_hours=6))

    text = LyricVideoGUI._youtube_status_text(stub)

    assert text.startswith("YouTube: quota exceeded")
    assert len(saved) == 1


def test_check_youtube_comments_worker_stops_and_saves_a_cooldown_on_quota_exceeded(tmp_path, monkeypatch):
    """One video's comment-check hitting quota means every other video would
    fail identically right now -- stop the whole scan instead of hammering
    the API for each remaining one (2026-09-18)."""
    monkeypatch.setattr("lyricvideo.gui.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    for slug in ("song-a", "song-b"):
        work_dir = tmp_path / "work" / slug
        work_dir.mkdir(parents=True)
        save_youtube_state(work_dir, YoutubeState(video_id=slug, uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr(
        "lyricvideo.gui.is_video_public",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr("lyricvideo.gui.is_quota_exceeded_error", lambda e: True)
    saved = []
    monkeypatch.setattr("lyricvideo.gui.save_quota_blocked_until", lambda dt: saved.append(dt))
    stub = _gui_stub(settings=Settings(youtube_quota_retry_hours=6), _render_pending_replies=lambda: None)

    LyricVideoGUI._check_youtube_comments_worker(stub)

    assert len(saved) == 1


def test_youtube_periodic_tick_skips_comment_check_and_retry_while_quota_blocked(monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    ran = []
    stub = _gui_stub(
        _youtube_status_text=lambda: "YouTube: quota exceeded, retrying after 2026-09-19 06:00",
        _check_youtube_comments_worker=lambda: ran.append("comments"),
        _retry_pending_uploads_if_due=lambda: ran.append("retry"),
        youtube_status_var=SimpleNamespace(set=lambda text: None),
    )

    LyricVideoGUI._youtube_periodic_tick(stub)

    assert ran == []


def test_youtube_periodic_tick_still_updates_the_status_label_while_quota_blocked(monkeypatch):
    """_youtube_status_text() itself makes no real API call while blocked, so
    it's safe (and useful, so the label doesn't go stale) to keep calling it
    every tick even during a cooldown."""
    monkeypatch.setattr(
        "lyricvideo.gui.load_quota_blocked_until",
        lambda: datetime.now().astimezone() + timedelta(hours=1),
    )
    set_values = []
    stub = _gui_stub(
        _youtube_status_text=lambda: "YouTube: quota exceeded, retrying after 2026-09-19 06:00",
        _check_youtube_comments_worker=_must_not_run,
        _retry_pending_uploads_if_due=_must_not_run,
        youtube_status_var=SimpleNamespace(set=lambda text: set_values.append(text)),
    )

    LyricVideoGUI._youtube_periodic_tick(stub)

    assert set_values == ["YouTube: quota exceeded, retrying after 2026-09-19 06:00"]


def test_youtube_periodic_tick_runs_normally_when_not_blocked(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.load_quota_blocked_until", lambda: None)
    ran = []
    stub = _gui_stub(
        _youtube_status_text=lambda: "YouTube: connected as Test Channel",
        _check_youtube_comments_worker=lambda: ran.append("comments"),
        _retry_pending_uploads_if_due=lambda: ran.append("retry"),
        youtube_status_var=SimpleNamespace(set=lambda text: None),
    )

    LyricVideoGUI._youtube_periodic_tick(stub)

    assert ran == ["comments", "retry"]


def test_generate_refuses_a_missing_audio_file_before_starting_anything(monkeypatch, tmp_path):
    """A typo'd or moved path used to surface only minutes later as an
    obscure Demucs/ffmpeg failure deep in the log."""
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)

    stub = _gui_stub(
        _running=False,
        title_var=SimpleNamespace(get=lambda: "Song"),
        audio_var=SimpleNamespace(get=lambda: str(tmp_path / "gone.mp3")),
        work_dir_var=SimpleNamespace(get=lambda: "", set=_must_not_run),
    )
    LyricVideoGUI._on_generate(stub)

    assert [title for title, _ in shown] == ["Audio file not found"]
    assert str(tmp_path / "gone.mp3") in shown[0][1]


def test_redo_refuses_when_the_original_audio_file_is_gone(monkeypatch, tmp_path):
    """The redo re-reads the original audio (sidecar lyrics, final render);
    if the owner moved it since, fail with the path up front -- before
    backing anything up or starting a run that would die at render time."""
    monkeypatch.setattr("lyricvideo.gui.load_redo_inputs", lambda song_dir: (tmp_path / "gone.mp3", "Angie"))
    monkeypatch.setattr("lyricvideo.gui.backup_song_outputs", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub(_running=False, redo_song_var=SimpleNamespace(get=lambda: "angie"))
    LyricVideoGUI._on_redo(stub)

    assert [title for title, _ in shown] == ["Original audio file not found"]
    assert str(tmp_path / "gone.mp3") in shown[0][1]
    assert "Angie" in shown[0][1]


def test_retry_upload_refuses_when_nothing_is_selected(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub(_running=False, retry_upload_song_var=SimpleNamespace(get=lambda: "  "))
    LyricVideoGUI._on_retry_upload(stub)

    assert [title for title, _ in shown] == ["No song selected"]


def test_retry_upload_confirms_before_re_uploading_an_already_uploaded_song(monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.load_youtube_state",
        lambda work_dir: YoutubeState(video_id="abc", uploaded_at="2026-01-01T00:00:00", title="t"),
    )
    asked = []
    monkeypatch.setattr(
        "lyricvideo.gui.messagebox.askyesno", lambda title, msg: asked.append((title, msg)) or False,
    )
    started = []

    stub = _gui_stub(
        _running=False,
        retry_upload_song_var=SimpleNamespace(get=lambda: "angie"),
        _start_retry_upload=lambda slugs: started.append(slugs),
    )
    LyricVideoGUI._on_retry_upload(stub)

    assert len(asked) == 1
    assert started == []  # declined -- must not proceed


def test_retry_upload_proceeds_after_confirming_an_already_uploaded_song(monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.load_youtube_state",
        lambda work_dir: YoutubeState(video_id="abc", uploaded_at="2026-01-01T00:00:00", title="t"),
    )
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", lambda title, msg: True)
    started = []

    stub = _gui_stub(
        _running=False,
        retry_upload_song_var=SimpleNamespace(get=lambda: "angie"),
        _start_retry_upload=lambda slugs: started.append(slugs),
    )
    LyricVideoGUI._on_retry_upload(stub)

    assert started == [["angie"]]


def test_retry_upload_skips_confirmation_for_a_never_uploaded_song(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.load_youtube_state", lambda work_dir: None)
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", _must_not_run)
    started = []

    stub = _gui_stub(
        _running=False,
        retry_upload_song_var=SimpleNamespace(get=lambda: "angie"),
        _start_retry_upload=lambda slugs: started.append(slugs),
    )
    LyricVideoGUI._on_retry_upload(stub)

    assert started == [["angie"]]


def test_retry_upload_all_shows_a_summary_and_refreshes_the_lists(monkeypatch, tmp_path):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(
        "lyricvideo.gui._retry_pending_uploads",
        lambda work_root, settings, slugs, force=False: {
            "succeeded": ["song-a"], "failed": [("song-b", "RuntimeError: boom")],
        },
    )
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showinfo", lambda title, msg: shown.append((title, msg)))
    button_states = []
    refreshed = []

    stub = _gui_stub(
        _running=False,
        retry_upload_button=SimpleNamespace(configure=lambda **kw: button_states.append(("upload", kw))),
        upload_selected_button=SimpleNamespace(configure=lambda **kw: button_states.append(("upload_selected", kw))),
        _refresh_retry_upload_options=lambda: refreshed.append(True),
    )
    stub._on_retry_upload_done = lambda results: LyricVideoGUI._on_retry_upload_done(stub, results)
    LyricVideoGUI._start_retry_upload(stub, None)

    assert shown == [(
        "Retry upload results",
        "Uploaded 1 song(s).\n1 failed:\n  song-b: RuntimeError: boom",
    )]
    assert ("upload", {"state": "disabled"}) in button_states
    assert ("upload_selected", {"state": "disabled"}) in button_states
    assert refreshed == [True]


def test_upload_selected_pending_refuses_when_nothing_is_checked(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub(_running=False, _pending_upload_vars={"song-a": SimpleNamespace(get=lambda: False)})
    LyricVideoGUI._on_upload_selected_pending(stub)

    assert [title for title, _ in shown] == ["No songs selected"]


def test_upload_selected_pending_uploads_only_the_checked_songs(monkeypatch):
    started = []
    stub = _gui_stub(
        _running=False,
        _pending_upload_vars={
            "song-a": SimpleNamespace(get=lambda: True),
            "song-b": SimpleNamespace(get=lambda: False),
            "song-c": SimpleNamespace(get=lambda: True),
        },
        _start_retry_upload=lambda slugs: started.append(slugs),
    )
    LyricVideoGUI._on_upload_selected_pending(stub)

    assert started == [["song-a", "song-c"]]


class _FakeBooleanVar:
    def __init__(self, value: bool):
        self._value = value

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = value


def test_toggle_pending_select_all_sets_every_checkbox(monkeypatch):
    var_a = _FakeBooleanVar(False)
    var_b = _FakeBooleanVar(False)
    stub = _gui_stub(
        pending_select_all_var=SimpleNamespace(get=lambda: True),
        _pending_upload_vars={"song-a": var_a, "song-b": var_b},
    )
    LyricVideoGUI._on_toggle_pending_select_all(stub)

    assert var_a.get() is True
    assert var_b.get() is True


def test_watch_song_shows_error_when_not_rendered_yet(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.song_video_path", lambda work_dir: None)
    opened = []
    monkeypatch.setattr("lyricvideo.gui._open_with_default_app", lambda path: opened.append(path))
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub()
    LyricVideoGUI._on_watch_song(stub, "angie")

    assert opened == []
    assert [title for title, _ in shown] == ["No video yet"]


def test_watch_song_opens_the_rendered_video(monkeypatch, tmp_path):
    video_path = tmp_path / "angie.mp4"
    monkeypatch.setattr("lyricvideo.gui.song_video_path", lambda work_dir: video_path)
    opened = []
    monkeypatch.setattr("lyricvideo.gui._open_with_default_app", lambda path: opened.append(path))
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", _must_not_run)

    stub = _gui_stub()
    LyricVideoGUI._on_watch_song(stub, "angie")

    assert opened == [video_path]


def test_watch_song_shows_error_when_the_default_app_fails_to_open(monkeypatch, tmp_path):
    video_path = tmp_path / "angie.mp4"
    monkeypatch.setattr("lyricvideo.gui.song_video_path", lambda work_dir: video_path)

    def failing_open(path):
        raise OSError("no player registered")

    monkeypatch.setattr("lyricvideo.gui._open_with_default_app", failing_open)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub()
    LyricVideoGUI._on_watch_song(stub, "angie")

    assert [title for title, _ in shown] == ["Could not open video"]


def test_remove_song_does_nothing_when_declined(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", lambda title, msg: False)
    monkeypatch.setattr("lyricvideo.gui.dismiss_song", _must_not_run)

    stub = _gui_stub(_refresh_song_list=_must_not_run)
    LyricVideoGUI._on_remove_song(stub, "pending", "angie")


def test_remove_song_dismisses_and_refreshes_just_that_list(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", lambda title, msg: True)
    dismissed = []
    monkeypatch.setattr("lyricvideo.gui.dismiss_song", lambda list_name, slug: dismissed.append((list_name, slug)))
    refreshed = []

    stub = _gui_stub(_refresh_song_list=lambda list_name: refreshed.append(list_name))
    LyricVideoGUI._on_remove_song(stub, "pending", "angie")

    assert dismissed == [("pending", "angie")]
    assert refreshed == ["pending"]


def test_refresh_song_list_redo_repopulates_the_redo_list(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_redoable_songs", lambda work_root: ["angie", "crazy"])
    calls = []

    stub = _gui_stub(
        redo_list_frame="redo-frame", redo_song_var="redo-var",
        _populate_song_radio_list=lambda *a, **kw: calls.append((a, kw)),
    )
    LyricVideoGUI._refresh_song_list(stub, "redo")

    assert calls == [(("redo-frame", "redo", ["angie", "crazy"], "redo-var"), {"auto_select_first": True})]


def test_refresh_song_list_upload_repopulates_the_upload_list(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_rendered_songs", lambda work_root: ["angie"])
    calls = []

    stub = _gui_stub(
        retry_upload_list_frame="upload-frame", retry_upload_song_var="upload-var",
        _populate_song_radio_list=lambda *a, **kw: calls.append((a, kw)),
    )
    LyricVideoGUI._refresh_song_list(stub, "upload")

    assert calls == [(("upload-frame", "upload", ["angie"], "upload-var"), {"auto_select_first": True})]


def test_refresh_song_list_pending_refreshes_the_pending_checklist(monkeypatch):
    refreshed = []
    stub = _gui_stub(_refresh_pending_uploads_list=lambda: refreshed.append(True))
    LyricVideoGUI._refresh_song_list(stub, "pending")

    assert refreshed == [True]


def test_refresh_retry_upload_options_invalidates_rather_than_rebuilds_directly(monkeypatch):
    """Real owner complaint, 2026-09-15: rebuilding the (possibly closed,
    never-opened) Upload-to-YouTube and Pending lists directly after every
    upload was the same expensive CTk-widget-construction cost as the
    launch-time slowness fixed the same day, just re-triggered by a
    different event -- freezing the window for a stretch even when nobody
    had those lists open. _invalidate_*_list() defers the actual rebuild
    unless that section is currently expanded."""
    button_states = []
    invalidated = []
    stub = _gui_stub(
        retry_upload_button=SimpleNamespace(configure=lambda **kw: button_states.append(("upload", kw))),
        upload_selected_button=SimpleNamespace(configure=lambda **kw: button_states.append(("selected", kw))),
        _invalidate_upload_list=lambda: invalidated.append("upload"),
        _invalidate_pending_list=lambda: invalidated.append("pending"),
        _invalidate_flagged_list=lambda: invalidated.append("flagged"),
    )
    LyricVideoGUI._refresh_retry_upload_options(stub)

    assert invalidated == ["upload", "pending", "flagged"]
    assert ("upload", {"state": "normal"}) in button_states
    assert ("selected", {"state": "normal"}) in button_states


def test_redo_flagged_sets_the_redo_dropdown_and_calls_on_redo(monkeypatch):
    calls = []
    stub = _gui_stub(
        redo_song_var=SimpleNamespace(set=lambda slug: calls.append(("set", slug))),
        _on_redo=lambda: calls.append(("on_redo",)),
    )

    LyricVideoGUI._on_redo_flagged(stub, "angie-rolling-stones")

    assert calls == [("set", "angie-rolling-stones"), ("on_redo",)]


def test_upload_anyway_flagged_reuses_the_existing_retry_upload_path(monkeypatch):
    calls = []
    stub = _gui_stub(_start_retry_upload=lambda slugs: calls.append(slugs))

    LyricVideoGUI._on_upload_anyway_flagged(stub, "angie-rolling-stones")

    assert calls == [["angie-rolling-stones"]]


def test_poll_queue_refreshes_retry_upload_options_on_each_batch_item_done():
    """Real gap found 2026-09-18: a 100-song Batch run only refreshed the
    Pending/Flagged/Upload lists once, at the very end -- a song flagged
    for lyrics review mid-batch wouldn't show up until the whole batch
    finished. _run_batch_worker now emits one "batch_item_done" per
    completed song specifically so this fires live."""
    refreshed = []
    q = queue.Queue()
    q.put(("batch_item_done", None))
    stub = _gui_stub(
        _queue=q, _running=False, _refresh_retry_upload_options=lambda: refreshed.append(True),
    )

    LyricVideoGUI._poll_queue(stub)

    assert refreshed == [True]


def test_run_batch_worker_releases_memory_after_every_item_success_or_failure(monkeypatch):
    """Real incident, 2026-09-18: earlyoom killed the app on song #24 of an
    overnight 100-song Batch run after memory crept up across dozens of
    songs in this one long-lived process. release_memory() must run after
    EVERY item -- including one that raises -- not just the ones that
    succeed, since a failed song can still have allocated real memory
    before failing."""
    released = []
    monkeypatch.setattr("lyricvideo.gui.release_memory", lambda: released.append(True))
    monkeypatch.setattr("lyricvideo.gui._maybe_upload_to_youtube", lambda work_dir, settings: None)

    def fake_run_pipeline(audio_path, work_dir, title, **kwargs):
        if title == "Bad Song":
            raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.run_pipeline", fake_run_pipeline)

    items = [
        BatchItem(audio_path=Path("a.mp3"), title="Good Song", work_dir=Path("work/good"), already_done=False),
        BatchItem(audio_path=Path("b.mp3"), title="Bad Song", work_dir=Path("work/bad"), already_done=False),
    ]
    stub = _gui_stub(_queue=queue.Queue(), settings=Settings())

    LyricVideoGUI._run_batch_worker(stub, items)

    assert released == [True, True]


def test_run_batch_worker_resumes_an_interrupted_item_at_its_own_resume_stage(monkeypatch):
    """resolve_batch_items() computes resume_stage="fetch_lyrics" for an item
    whose Demucs stems already exist on disk (interrupted after separate()
    finished) -- _run_batch_worker must actually pass that through to
    run_pipeline for a not-already-done item, not always default to
    "identify" (real owner complaint, 2026-09-18: closing mid-batch left the
    interrupted song with "no way to resume" other than redoing everything,
    including the slowest stage in the whole pipeline)."""
    monkeypatch.setattr("lyricvideo.gui.release_memory", lambda: None)
    monkeypatch.setattr("lyricvideo.gui._maybe_upload_to_youtube", lambda work_dir, settings: None)
    calls = []

    def fake_run_pipeline(audio_path, work_dir, title, **kwargs):
        calls.append(kwargs.get("start_stage"))

    monkeypatch.setattr("lyricvideo.gui.run_pipeline", fake_run_pipeline)

    items = [
        BatchItem(
            audio_path=Path("a.mp3"), title="Fresh Song", work_dir=Path("work/fresh"),
            already_done=False, resume_stage="identify",
        ),
        BatchItem(
            audio_path=Path("b.mp3"), title="Interrupted Song", work_dir=Path("work/interrupted"),
            already_done=False, resume_stage="fetch_lyrics",
        ),
    ]
    stub = _gui_stub(_queue=queue.Queue(), settings=Settings())

    LyricVideoGUI._run_batch_worker(stub, items)

    assert calls == ["identify", "fetch_lyrics"]


def test_on_batch_done_refreshes_retry_upload_options(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.messagebox.showinfo", lambda *a, **k: None)
    refreshed = []
    stub = _gui_stub(
        generate_button=SimpleNamespace(configure=lambda **kw: None),
        redo_button=SimpleNamespace(configure=lambda **kw: None),
        batch_button=SimpleNamespace(configure=lambda **kw: None),
        progress_bar=SimpleNamespace(set=lambda v: None),
        status_var=SimpleNamespace(set=lambda v: None),
        _refresh_retry_upload_options=lambda: refreshed.append(True),
    )

    LyricVideoGUI._on_batch_done(stub, {"succeeded": ["Angie"], "skipped_already_done": [], "failed": []})

    assert refreshed == [True]
