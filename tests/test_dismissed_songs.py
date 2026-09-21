from lyricvideo.dismissed_songs import dismiss_song, load_dismissed, undismiss_song


def test_load_dismissed_missing_file_returns_empty(tmp_path):
    assert load_dismissed("pending", tmp_path / "no_such_state.json") == set()


def test_dismiss_then_load_round_trips(tmp_path):
    state_file = tmp_path / "dismissed_songs.json"

    dismiss_song("pending", "song-a", state_file)

    assert load_dismissed("pending", state_file) == {"song-a"}


def test_dismiss_song_creates_parent_dir(tmp_path):
    state_file = tmp_path / "nested" / "dismissed_songs.json"

    dismiss_song("redo", "song-a", state_file)

    assert load_dismissed("redo", state_file) == {"song-a"}


def test_dismiss_is_scoped_per_list(tmp_path):
    state_file = tmp_path / "dismissed_songs.json"

    dismiss_song("pending", "song-a", state_file)

    assert load_dismissed("pending", state_file) == {"song-a"}
    assert load_dismissed("upload", state_file) == set()
    assert load_dismissed("redo", state_file) == set()


def test_dismiss_song_twice_is_idempotent(tmp_path):
    state_file = tmp_path / "dismissed_songs.json"

    dismiss_song("pending", "song-a", state_file)
    dismiss_song("pending", "song-a", state_file)

    assert load_dismissed("pending", state_file) == {"song-a"}


def test_dismiss_accumulates_multiple_songs_in_the_same_list(tmp_path):
    state_file = tmp_path / "dismissed_songs.json"

    dismiss_song("upload", "song-a", state_file)
    dismiss_song("upload", "song-b", state_file)

    assert load_dismissed("upload", state_file) == {"song-a", "song-b"}


def test_load_dismissed_corrupt_file_returns_empty(tmp_path):
    state_file = tmp_path / "dismissed_songs.json"
    state_file.write_text("not json", encoding="utf-8")

    assert load_dismissed("pending", state_file) == set()


def test_undismiss_brings_back_only_that_song_and_is_harmless_when_it_was_never_hidden(tmp_path):
    state_file = tmp_path / "dismissed_songs.json"
    dismiss_song("flagged", "song-a", state_file)
    dismiss_song("flagged", "song-b", state_file)
    dismiss_song("pending", "song-a", state_file)

    undismiss_song("flagged", "song-a", state_file)
    undismiss_song("flagged", "never-hidden", state_file)
    undismiss_song("flagged", "song-a", tmp_path / "no_such_state.json")

    assert load_dismissed("flagged", state_file) == {"song-b"}
    assert load_dismissed("pending", state_file) == {"song-a"}                # other lists are untouched
