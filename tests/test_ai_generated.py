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


# --- an AI song someone else made (owner, 2026-11-25: "Storm in My Heart" is a Rockin' Jukebox AI song labeled Elvis) ---

def test_an_ai_song_made_elsewhere_is_known_as_ai_but_is_not_our_own(tmp_path):
    from lyricvideo.ai_generated import AI_OTHER, AI_OTHER_FILE, ai_song_kind, mark_ai_made_by_others

    song = tmp_path / "storm"
    (song / "easychords").mkdir(parents=True)
    assert ai_song_kind(song) == "" and ai_song_kind(song / "easychords") == ""

    assert mark_ai_made_by_others(song, "Rockin' Jukebox AI song") == song / AI_OTHER_FILE

    assert ai_song_kind(song) == AI_OTHER and ai_song_kind(song / "easychords") == AI_OTHER
    assert not is_ai_generated(song)     # our own-song behaviour (lane, timing, description) is left alone


def test_our_own_ai_song_is_the_own_kind(tmp_path):
    from lyricvideo.ai_generated import AI_OWN, ai_song_kind

    (tmp_path / "easychords").mkdir()
    mark_ai_generated(tmp_path)
    assert ai_song_kind(tmp_path) == AI_OWN and ai_song_kind(tmp_path / "easychords") == AI_OWN
