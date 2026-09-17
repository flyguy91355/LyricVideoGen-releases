from datetime import datetime, timezone

from lyricvideo.youtube_quota_state import load_quota_blocked_until, save_quota_blocked_until


def test_load_quota_blocked_until_returns_none_when_no_file(tmp_path):
    assert load_quota_blocked_until(tmp_path / "no_such_file.json") is None


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "quota.json"
    blocked_until = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)

    save_quota_blocked_until(blocked_until, path)

    assert load_quota_blocked_until(path) == blocked_until


def test_save_quota_blocked_until_overwrites_a_previous_value(tmp_path):
    path = tmp_path / "quota.json"
    save_quota_blocked_until(datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc), path)

    save_quota_blocked_until(datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc), path)

    assert load_quota_blocked_until(path) == datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)


def test_load_quota_blocked_until_returns_none_on_corrupt_file(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("not json", encoding="utf-8")

    assert load_quota_blocked_until(path) is None
