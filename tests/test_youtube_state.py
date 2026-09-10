from pathlib import Path

from lyricvideo.youtube_state import YoutubeState, load_youtube_state, save_youtube_state


def test_load_youtube_state_returns_none_when_no_file(tmp_path):
    assert load_youtube_state(tmp_path) is None


def test_save_then_load_round_trips(tmp_path):
    state = YoutubeState(video_id="abc123", uploaded_at="2026-09-10T15:00:00", title="My Song")

    save_youtube_state(tmp_path, state)

    assert load_youtube_state(tmp_path) == state


def test_load_youtube_state_returns_none_on_corrupt_file(tmp_path):
    (tmp_path / "youtube_state.json").write_text("not json", encoding="utf-8")

    assert load_youtube_state(tmp_path) is None


def test_load_youtube_state_returns_none_on_missing_fields(tmp_path):
    (tmp_path / "youtube_state.json").write_text('{"video_id": "abc"}', encoding="utf-8")

    assert load_youtube_state(tmp_path) is None
