import json

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


def test_engagement_comment_posted_defaults_to_false_for_a_legacy_file(tmp_path):
    """A youtube_state.json written before this field existed has none of
    these keys -- YoutubeState(**data) must still succeed, or every one of
    the 67 already-uploaded songs would suddenly look never-uploaded."""
    (tmp_path / "youtube_state.json").write_text(
        json.dumps({"video_id": "abc", "uploaded_at": "2026-09-10T15:00:00", "title": "t"}),
        encoding="utf-8",
    )

    state = load_youtube_state(tmp_path)

    assert state is not None
    assert state.engagement_comment_posted is False


def test_save_then_load_round_trips_engagement_comment_posted(tmp_path):
    state = YoutubeState(
        video_id="abc123", uploaded_at="2026-09-10T15:00:00", title="My Song",
        engagement_comment_posted=True,
    )

    save_youtube_state(tmp_path, state)

    assert load_youtube_state(tmp_path) == state


# --- every uploaded video, EASY CHORD versions included (issue #7 review, F087) -----------------------------------------

def _uploaded(folder, video_id):
    folder.mkdir(parents=True, exist_ok=True)
    save_youtube_state(folder, YoutubeState(video_id=video_id, uploaded_at="2026-09-20T10:00:00", title="t"))


def test_uploaded_song_dirs_includes_nested_easy_chord_versions(tmp_path):
    from lyricvideo.youtube_state import uploaded_song_dirs

    _uploaded(tmp_path / "some-song", "MAIN")
    _uploaded(tmp_path / "some-song" / "easychords", "EASY")
    (tmp_path / "not-uploaded").mkdir()
    _uploaded(tmp_path / "other-song" / "easychords", "EASY2")        # EASY version up, the song itself not

    found = [(slug, state.video_id) for slug, _path, state in uploaded_song_dirs(tmp_path)]

    assert found == [("other-song/easychords", "EASY2"), ("some-song", "MAIN"), ("some-song/easychords", "EASY")]


def test_uploaded_song_dirs_of_a_missing_folder_is_empty(tmp_path):
    from lyricvideo.youtube_state import uploaded_song_dirs

    assert uploaded_song_dirs(tmp_path / "no-work-here") == []


def test_mark_engagement_comment_posted_finds_an_easy_chord_version(tmp_path):
    from lyricvideo.youtube_state import mark_engagement_comment_posted

    _uploaded(tmp_path / "some-song", "MAIN")
    _uploaded(tmp_path / "some-song" / "easychords", "EASY")

    assert mark_engagement_comment_posted(tmp_path, "EASY") is True
    assert load_youtube_state(tmp_path / "some-song" / "easychords").engagement_comment_posted is True
    assert load_youtube_state(tmp_path / "some-song").engagement_comment_posted is False
    assert mark_engagement_comment_posted(tmp_path, "NOPE") is False


def test_save_youtube_state_leaves_no_temp_file(tmp_path):
    save_youtube_state(tmp_path, YoutubeState(video_id="v", uploaded_at="u", title="t"))

    assert [p.name for p in tmp_path.iterdir()] == ["youtube_state.json"]
