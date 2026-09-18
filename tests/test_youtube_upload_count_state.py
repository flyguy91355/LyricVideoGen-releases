from datetime import date, timedelta

from lyricvideo.youtube_upload_count_state import load_uploads_today, record_upload


def test_load_uploads_today_returns_zero_when_no_file(tmp_path):
    assert load_uploads_today(tmp_path / "no_such_file.json") == 0


def test_record_upload_then_load_round_trips(tmp_path):
    path = tmp_path / "count.json"

    record_upload(path)

    assert load_uploads_today(path) == 1


def test_record_upload_accumulates_across_calls(tmp_path):
    path = tmp_path / "count.json"

    record_upload(path)
    record_upload(path)
    record_upload(path)

    assert load_uploads_today(path) == 3


def test_load_uploads_today_returns_zero_for_a_stale_previous_day(tmp_path):
    import json

    path = tmp_path / "count.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"date": yesterday, "count": 7}), encoding="utf-8")

    assert load_uploads_today(path) == 0


def test_record_upload_resets_the_count_on_a_new_day(tmp_path):
    import json

    path = tmp_path / "count.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"date": yesterday, "count": 7}), encoding="utf-8")

    record_upload(path)

    assert load_uploads_today(path) == 1


def test_load_uploads_today_returns_zero_on_corrupt_file(tmp_path):
    path = tmp_path / "count.json"
    path.write_text("not json", encoding="utf-8")

    assert load_uploads_today(path) == 0
