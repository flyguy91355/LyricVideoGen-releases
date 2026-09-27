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


# --- the playlist cache is written atomically under a lock, and a corrupt one is kept, not overwritten -----------------
# Issue #7 review, F143/F144: an unlocked read-modify-write lost cached ids under contention, and an unreadable cache read
# as {} and was then overwritten by the next save -- after which organize_video created duplicate public playlists.

def test_concurrent_saves_keep_every_playlist_id(tmp_path):
    import threading

    path = tmp_path / "playlists.json"
    save_playlist_id("all", "PL", path)

    def worker(prefix):
        for i in range(100):
            save_playlist_id(f"{prefix}{i}", f"id-{prefix}{i}", path)

    threads = [threading.Thread(target=worker, args=(p,)) for p in ("a:", "b:")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = load_playlist_ids(path)
    assert len(ids) == 201 and ids["all"] == "PL"


def test_a_corrupt_playlist_cache_is_moved_aside_with_its_bytes_intact(tmp_path):
    path = tmp_path / "playlists.json"
    path.write_text('{"all": "PL_ALL", "artist:X": ', encoding="utf-8")      # truncated mid-write

    assert load_playlist_ids(path) == {}
    backups = list(tmp_path.glob("playlists.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"all": "PL_ALL", "artist:X": '

    save_playlist_id("genre:Pop", "PL_POP", path)
    assert load_playlist_ids(path) == {"genre:Pop": "PL_POP"}
    assert backups[0].read_text(encoding="utf-8") == '{"all": "PL_ALL", "artist:X": '     # never overwritten


def test_saving_leaves_no_temp_files_behind(tmp_path):
    path = tmp_path / "playlists.json"

    save_playlist_id("all", "PL", path)
    add_genre_if_new("Bluegrass", tmp_path / "genres.json")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["genres.json", "playlists.json"]


def test_remove_playlist_ids_drops_only_the_named_keys(tmp_path):
    from lyricvideo.youtube_playlist_state import remove_playlist_ids

    path = tmp_path / "playlists.json"
    for key in ("all", "artist:Crosby", "artist:Stills"):
        save_playlist_id(key, f"id-{key}", path)

    remove_playlist_ids(["artist:Crosby", "artist:Stills"], path)

    assert load_playlist_ids(path) == {"all": "id-all"}


def test_a_corrupt_genre_list_keeps_its_bytes_and_falls_back_to_the_defaults(tmp_path):
    path = tmp_path / "genres.json"
    path.write_text("[\"Bluegrass\", ", encoding="utf-8")

    assert load_genres(path) == DEFAULT_GENRES
    assert len(list(tmp_path.glob("genres.json.corrupt-*"))) == 1
