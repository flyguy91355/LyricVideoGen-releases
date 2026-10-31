from lyricvideo.ai_generated import AI_FILE, is_ai_generated, mark_ai_generated


def test_a_tagged_song_and_its_easy_version_are_ai_generated(tmp_path):
    song = tmp_path / "lantern"
    (song / "easychords").mkdir(parents=True)
    assert not is_ai_generated(song) and not is_ai_generated(song / "easychords")

    assert mark_ai_generated(song) == song / AI_FILE

    assert is_ai_generated(song)
    assert is_ai_generated(song / "easychords")


def test_only_an_easychords_folder_follows_its_parent(tmp_path):
    mark_ai_generated(tmp_path)
    (tmp_path / "other").mkdir()
    assert not is_ai_generated(tmp_path / "other")
