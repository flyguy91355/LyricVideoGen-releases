from lyricvideo.youtube_playlist_state import (
    DEFAULT_GENRES,
    add_genre_if_new,
    load_genres,
    load_playlist_ids,
    save_playlist_id,
)


def test_load_playlist_ids_empty_when_no_file(tmp_path):
    assert load_playlist_ids(tmp_path / "no_such_file.json") == {}


def test_save_then_load_playlist_id_round_trips(tmp_path):
    path = tmp_path / "playlists.json"

    save_playlist_id("all", "PLall", path)
    save_playlist_id("artist:Pink Floyd", "PLartist", path)

    assert load_playlist_ids(path) == {"all": "PLall", "artist:Pink Floyd": "PLartist"}


def test_save_playlist_id_overwrites_an_existing_key(tmp_path):
    path = tmp_path / "playlists.json"
    save_playlist_id("all", "PLold", path)

    save_playlist_id("all", "PLnew", path)

    assert load_playlist_ids(path) == {"all": "PLnew"}


def test_load_genres_returns_the_default_list_when_no_file(tmp_path):
    assert load_genres(tmp_path / "no_such_file.json") == DEFAULT_GENRES


def test_add_genre_if_new_appends_a_genre_not_already_present(tmp_path):
    path = tmp_path / "genres.json"

    add_genre_if_new("Bluegrass", path)

    assert "Bluegrass" in load_genres(path)


def test_add_genre_if_new_does_not_duplicate_an_existing_genre(tmp_path):
    path = tmp_path / "genres.json"
    add_genre_if_new("Bluegrass", path)

    add_genre_if_new("Bluegrass", path)

    assert load_genres(path).count("Bluegrass") == 1


def test_add_genre_if_new_preserves_the_seeded_defaults(tmp_path):
    path = tmp_path / "genres.json"

    add_genre_if_new("Bluegrass", path)

    genres = load_genres(path)
    assert "Classic Rock" in genres
    assert "Bluegrass" in genres
