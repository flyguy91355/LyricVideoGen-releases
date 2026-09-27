import json
from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, save_song
from lyricvideo.youtube import is_video_in_playlist
from lyricvideo.youtube_playlists import _split_artists
from lyricvideo.youtube_state import YoutubeState, save_youtube_state


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakePlaylistsResource:
    def __init__(self):
        self._next_id = 0
        self.existing_playlist_ids: set[str] = set()
        self.titles: dict[str, str] = {}              # playlist id -> title (what a mine=True listing reports)
        self.descriptions: dict[str, str] = {}
        self.inserted_titles: list[str] = []
        self.updates: list[dict] = []

    def insert(self, part, body):
        self._next_id += 1
        new_id = f"PL{self._next_id}"
        self.existing_playlist_ids.add(new_id)
        self.titles[new_id] = body["snippet"]["title"]
        self.descriptions[new_id] = body["snippet"].get("description", "")
        self.inserted_titles.append(body["snippet"]["title"])
        return _Exec({"id": new_id})

    def list(self, part, id=None, mine=None, maxResults=None, pageToken=None):
        if mine:
            return _Exec({"items": [
                {"id": pid, "snippet": {"title": self.titles.get(pid, ""), "description": self.descriptions.get(pid, "")}}
                for pid in sorted(self.existing_playlist_ids)
            ]})
        requested = set(id.split(","))
        items = [
            {"id": pid, "snippet": {"title": self.titles.get(pid, ""), "description": self.descriptions.get(pid, "")}}
            for pid in requested if pid in self.existing_playlist_ids
        ]
        return _Exec({"items": items})

    def update(self, part, body):
        self.updates.append(body)
        self.descriptions[body["id"]] = body["snippet"]["description"]
        return _Exec({})


class _FakePlaylistItemsResource:
    def __init__(self):
        self.members: dict[str, set[str]] = {}
        self.deleted: list[tuple[str, str]] = []

    def list(self, part, playlistId, videoId):
        is_member = videoId in self.members.get(playlistId, set())
        return _Exec({"items": [{"id": f"{playlistId}|{videoId}"}] if is_member else []})

    def insert(self, part, body):
        snippet = body["snippet"]
        self.members.setdefault(snippet["playlistId"], set()).add(snippet["resourceId"]["videoId"])
        return _Exec({"id": "item-new"})

    def delete(self, id):
        playlist_id, video_id = id.split("|")
        self.members.get(playlist_id, set()).discard(video_id)
        self.deleted.append((playlist_id, video_id))
        return _Exec({})


class _FakeYoutubeClient:
    def __init__(self):
        self._playlists = _FakePlaylistsResource()
        self._playlist_items = _FakePlaylistItemsResource()

    def playlists(self):
        return self._playlists

    def playlistItems(self):
        return self._playlist_items


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeAnthropicResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text="GENRE: Classic Rock"):
        self.messages = _FakeMessages(text)


def _make_work_dir(
    tmp_path: Path, artist: str, genre: str = "", video_id: str = "vid123", key: str = "", chords: list[str] = (),
) -> Path:
    work_dir = tmp_path / "my-song"
    work_dir.mkdir()
    info = {"title": "My Song", "artist": artist, "duration": 200.0, "alt_titles": []}
    if genre:
        info["genre"] = genre
    (work_dir / "song_info.json").write_text(json.dumps(info), encoding="utf-8")
    events = [ChordEvent(start=float(i), end=float(i + 1), label=label) for i, label in enumerate(chords)]
    song = Song(
        title="My Song", audio_path="song.mp3", lines=[LyricLine(words=[Word(word="hello")])],
        chord_track=ChordTrack(key=key, events=events),
    )
    save_song(song, work_dir / "lyrics_timed.json")
    save_youtube_state(work_dir, YoutubeState(video_id=video_id, uploaded_at="2026-09-10T15:00:00", title="My Song"))
    return work_dir


def _patch_state(monkeypatch, playlist_ids=None, genres=None, pending=None):
    playlist_ids = {} if playlist_ids is None else playlist_ids
    genres = ["Classic Rock"] if genres is None else genres
    pending = [] if pending is None else pending
    monkeypatch.setattr("lyricvideo.youtube_playlists.load_playlist_ids", lambda: dict(playlist_ids))
    monkeypatch.setattr(
        "lyricvideo.youtube_playlists.save_playlist_id",
        lambda key, pid: playlist_ids.__setitem__(key, pid),
    )
    monkeypatch.setattr("lyricvideo.youtube_playlists.load_genres", lambda: list(genres))
    monkeypatch.setattr("lyricvideo.youtube_playlists.add_genre_if_new", lambda g: genres.append(g))
    monkeypatch.setattr("lyricvideo.youtube_playlists.load_pending_comments", lambda: list(pending))
    monkeypatch.setattr("lyricvideo.youtube_playlists.add_pending_comment", lambda c: pending.append(c))
    return playlist_ids, genres, pending


def test_organize_video_adds_to_all_artist_and_genre_playlists(tmp_path, monkeypatch):
    playlist_ids, _genres, added_comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert is_video_in_playlist(client, playlist_ids["all"], "vid123")
    assert is_video_in_playlist(client, playlist_ids["artist:Pink Floyd"], "vid123")
    assert is_video_in_playlist(client, playlist_ids["genre:Classic Rock"], "vid123")
    assert len(added_comments) == 1
    assert added_comments[0].video_id == "vid123"


def test_organize_video_adds_to_every_listed_artists_playlist(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Bryan Adams, Sting, Rod Stewart", genre="Pop Rock / Soft Rock")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient(), work_dir)

    for artist in ["Bryan Adams", "Sting", "Rod Stewart"]:
        assert is_video_in_playlist(client, playlist_ids[f"artist:{artist}"], "vid123")


def test_split_artists_splits_a_genuine_multi_artist_collaboration():
    assert _split_artists("Bryan Adams, Sting, Rod Stewart") == ["Bryan Adams", "Sting", "Rod Stewart"]


def test_split_artists_keeps_a_band_whose_own_name_has_a_comma_in_it():
    """Owner, 2026-09-23: "Crosby, Stills, Nash & Young" got split into three broken playlists -- "Crosby",
    "Stills", "Nash & Young" -- because the split logic can't tell a band's own name from a real
    multi-artist collaboration list. It's one band; it must never split."""
    assert _split_artists("Crosby, Stills, Nash & Young") == ["Crosby, Stills, Nash & Young"]
    assert _split_artists("Crosby, Stills & Nash") == ["Crosby, Stills & Nash"]
    assert _split_artists("Emerson, Lake & Palmer") == ["Emerson, Lake & Palmer"]
    assert _split_artists("Blood, Sweat & Tears") == ["Blood, Sweat & Tears"]
    assert _split_artists("Earth, Wind & Fire") == ["Earth, Wind & Fire"]


def test_organize_video_classifies_and_caches_genre_when_blank(tmp_path, monkeypatch):
    _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Lynyrd Skynyrd")  # no genre yet
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("GENRE: Southern Rock"), work_dir)

    info = json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
    assert info["genre"] == "Southern Rock"


def test_organize_video_does_not_reclassify_when_genre_already_cached(tmp_path, monkeypatch):
    _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Lynyrd Skynyrd", genre="Southern Rock")
    client = _FakeYoutubeClient()

    def _must_not_run(*a, **k):
        raise AssertionError("should not reclassify a song that already has a genre")

    monkeypatch.setattr("lyricvideo.youtube_playlists.classify_genre", _must_not_run)

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient(), work_dir)  # must not raise


def test_organize_video_adds_an_easy_key_song_to_the_easy_chord_playlist(tmp_path, monkeypatch):
    """Owner, 2026-09-23: "i want all the videos that have easy chords already in the same playlist."""
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock", key="C major")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert is_video_in_playlist(client, playlist_ids["easy_chord"], "vid123")


def test_organize_video_does_not_add_a_hard_key_song_to_the_easy_chord_playlist(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock", key="Db major")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert "easy_chord" not in playlist_ids


def test_organize_video_does_not_add_a_song_with_no_detected_key_to_the_easy_chord_playlist(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock")   # no key set
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert "easy_chord" not in playlist_ids


def test_organize_video_adds_a_three_chord_song_to_the_three_chord_playlist(tmp_path, monkeypatch):
    """Owner, 2026-09-23: "lets do 2 more.. 3 chord songs and 4 chord songs and easy chords" -- then "3 and 4
    chord still has to have the easy chord rule": chord count alone isn't enough, the song's key must also be
    a natural tonic."""
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock", key="C major", chords=["G", "C", "D"])
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert is_video_in_playlist(client, playlist_ids["three_chord"], "vid123")
    assert "four_chord" not in playlist_ids


def test_organize_video_adds_a_four_chord_song_to_the_four_chord_playlist(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(
        tmp_path, artist="Pink Floyd", genre="Classic Rock", key="C major", chords=["G", "C", "D", "Em"],
    )
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert is_video_in_playlist(client, playlist_ids["four_chord"], "vid123")
    assert "three_chord" not in playlist_ids


def test_organize_video_does_not_add_a_three_chord_song_in_a_hard_key_to_the_three_chord_playlist(tmp_path, monkeypatch):
    """Owner, 2026-09-23: "3 and 4 chord still has to have the easy chord rule"."""
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock", key="Db major", chords=["Db", "Gb", "Ab"])
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert "three_chord" not in playlist_ids


def test_organize_video_does_not_add_a_five_chord_song_to_either_chord_count_playlist(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(
        tmp_path, artist="Pink Floyd", genre="Classic Rock", key="C major", chords=["G", "C", "D", "Em", "Am"],
    )
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)

    assert "three_chord" not in playlist_ids and "four_chord" not in playlist_ids


def test_organize_video_is_idempotent_on_a_second_run(tmp_path, monkeypatch):
    """Re-running the backfill must never duplicate a playlist entry or
    queue a second pending comment for the same video."""
    playlist_ids, _genres, added_comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice!"), work_dir)
    organize_video(client, _FakeAnthropicClient("COMMENT: Nice again!"), work_dir)

    assert client._playlist_items.members[playlist_ids["all"]] == {"vid123"}
    assert len(added_comments) == 1


def test_organize_video_does_not_queue_a_comment_already_pending(tmp_path, monkeypatch):
    from lyricvideo.youtube_comment_state import PendingComment
    existing = PendingComment(video_id="vid123", song_title="My Song", draft_text="already drafted")
    _playlist_ids, _genres, pending_comments = _patch_state(monkeypatch, pending=[existing])
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient(), work_dir)

    assert pending_comments == [existing]  # no second draft queued alongside it


def test_organize_video_does_not_queue_a_comment_already_posted(tmp_path, monkeypatch):
    _playlist_ids, _genres, added_comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock")
    save_youtube_state(work_dir, YoutubeState(
        video_id="vid123", uploaded_at="2026-09-10T15:00:00", title="My Song",
        engagement_comment_posted=True,
    ))
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient(), work_dir)

    assert added_comments == []


def test_organize_video_returns_early_when_never_uploaded(tmp_path, monkeypatch):
    _patch_state(monkeypatch)
    work_dir = tmp_path / "not-uploaded"
    work_dir.mkdir()
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient(), work_dir)  # must not raise


# --- a missing or unusable genre never keeps a video out of its playlists (issue #7 review, F085) ----------------------

class _ThinkingOnlyMessages:
    def create(self, **kwargs):
        return type("Response", (), {"content": [type("Block", (), {"type": "thinking", "thinking": ""})()]})()


class _NoTextAnthropicClient:
    messages = _ThinkingOnlyMessages()


def test_organize_video_still_fills_the_all_and_artist_playlists_when_no_genre_comes_back(tmp_path, monkeypatch):
    playlist_ids, genres, comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band")          # no genre cached yet
    before = (work_dir / "song_info.json").read_text(encoding="utf-8")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _NoTextAnthropicClient(), work_dir)          # must not raise

    assert is_video_in_playlist(client, playlist_ids["all"], "vid123")
    assert is_video_in_playlist(client, playlist_ids["artist:Made Up Band"], "vid123")
    assert genres == ["Classic Rock"]                                   # no "" (or fragment) added to the shared list
    assert not any(key.startswith("genre:") for key in playlist_ids)
    assert (work_dir / "song_info.json").read_text(encoding="utf-8") == before
    assert comments == []                                               # a blank engagement comment is never queued


def test_add_genre_if_new_ignores_a_blank_genre(tmp_path):
    from lyricvideo.youtube_playlist_state import DEFAULT_GENRES, add_genre_if_new, load_genres

    path = tmp_path / "genres.json"
    add_genre_if_new("", path)
    add_genre_if_new("   ", path)

    assert load_genres(path) == DEFAULT_GENRES


# --- song_info.json is never replaced by a genre-only file (issue #7 review, F147) -------------------------------------

def test_organize_video_never_creates_a_song_info_json_holding_only_the_genre(tmp_path, monkeypatch):
    playlist_ids, _genres, comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band")
    (work_dir / "song_info.json").unlink()                               # a pre-merge song never had one
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("GENRE: Southern Rock\nCOMMENT: Which instrument?"), work_dir)

    assert not (work_dir / "song_info.json").exists()
    assert is_video_in_playlist(client, playlist_ids["all"], "vid123")
    assert is_video_in_playlist(client, playlist_ids["genre:Southern Rock"], "vid123")
    assert comments[0].song_title == "My Song"                          # the song's real title, not the folder name


def test_organize_video_leaves_an_unreadable_song_info_json_byte_for_byte(tmp_path, monkeypatch):
    _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band")
    (work_dir / "song_info.json").write_text("{not json", encoding="utf-8")

    from lyricvideo.youtube_playlists import organize_video
    organize_video(_FakeYoutubeClient(), _FakeAnthropicClient("GENRE: Pop\nCOMMENT: Hi!"), work_dir)

    assert (work_dir / "song_info.json").read_text(encoding="utf-8") == "{not json"


def test_organize_video_keeps_every_other_song_info_field_when_caching_the_genre(tmp_path, monkeypatch):
    _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band")

    from lyricvideo.youtube_playlists import organize_video
    organize_video(_FakeYoutubeClient(), _FakeAnthropicClient("GENRE: Pop\nCOMMENT: Hi!"), work_dir)

    info = json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
    assert info["genre"] == "Pop" and info["title"] == "My Song" and info["artist"] == "Made Up Band"


# --- artist credits split into the real artists (issue #7 review, F146) ------------------------------------------------

def test_split_artists_splits_the_final_ampersand_of_a_collaboration_credit():
    assert _split_artists("Bryan Adams, Rod Stewart & Sting") == ["Bryan Adams", "Rod Stewart", "Sting"]


def test_split_artists_keeps_comma_named_acts_whole_however_the_and_is_written():
    assert _split_artists("Crosby, Stills, Nash and Young") == ["Crosby, Stills, Nash and Young"]
    assert _split_artists("Earth, Wind and Fire") == ["Earth, Wind and Fire"]
    assert _split_artists("Peter, Paul and Mary") == ["Peter, Paul and Mary"]
    assert _split_artists("Tyler, the Creator") == ["Tyler, the Creator"]


def test_split_artists_keeps_a_plain_duo_and_a_band_with_the_whole():
    assert _split_artists("Simon & Garfunkel") == ["Simon & Garfunkel"]
    assert _split_artists("Made Up Singer, Tom Petty & the Heartbreakers") == [
        "Made Up Singer", "Tom Petty & the Heartbreakers",
    ]


def test_organize_video_never_creates_a_playlist_for_a_made_up_artist_pair(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Bryan Adams, Rod Stewart & Sting", genre="Pop Rock / Soft Rock")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), work_dir)

    assert "Rod Stewart & Sting - Play Along Videos" not in client._playlists.inserted_titles
    for artist in ["Bryan Adams", "Rod Stewart", "Sting"]:
        assert is_video_in_playlist(client, playlist_ids[f"artist:{artist}"], "vid123")


def test_organize_video_prefers_the_individual_artists_identify_saved(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Hall & Oates, Made Up Singer", genre="Pop")
    info = json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
    info["artists"] = ["Hall & Oates", "Made Up Singer"]
    (work_dir / "song_info.json").write_text(json.dumps(info), encoding="utf-8")

    from lyricvideo.youtube_playlists import organize_video
    organize_video(_FakeYoutubeClient(), _FakeAnthropicClient("COMMENT: Hi!"), work_dir)

    assert {k for k in playlist_ids if k.startswith("artist:")} == {"artist:Hall & Oates", "artist:Made Up Singer"}


# --- EASY/3-/4-CHORD membership follows the song's real key, both ways (issue #7 review, F148) -------------------------

def _set_key(work_dir, key):
    from lyricvideo.models import load_song

    song = load_song(work_dir / "lyrics_timed.json")
    song.chord_track = ChordTrack(key=key, events=song.chord_track.events)
    save_song(song, work_dir / "lyrics_timed.json")


def test_a_video_whose_key_turns_out_hard_is_taken_back_out_of_the_easy_playlists(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band", genre="Pop", key="D minor", chords=["Dm", "G", "A"])
    client = _FakeYoutubeClient()
    from lyricvideo.youtube_playlists import organize_video

    organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), work_dir)
    assert is_video_in_playlist(client, playlist_ids["easy_chord"], "vid123")
    assert is_video_in_playlist(client, playlist_ids["three_chord"], "vid123")

    _set_key(work_dir, "C minor")                                      # the real key: no open C minor shape
    removed = organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), work_dir)

    assert not is_video_in_playlist(client, playlist_ids["easy_chord"], "vid123")
    assert not is_video_in_playlist(client, playlist_ids["three_chord"], "vid123")
    assert sorted(removed) == ["easy_chord", "three_chord"]


def test_an_audited_key_override_decides_easy_membership(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch, playlist_ids={})
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band", genre="Pop", key="G major", chords=["G", "C", "D"])
    client = _FakeYoutubeClient()
    from lyricvideo.youtube_playlists import organize_video

    organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), work_dir)
    organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), work_dir, key_override="G minor")

    assert not is_video_in_playlist(client, playlist_ids["easy_chord"], "vid123")


def test_the_owners_own_key_puts_a_song_saved_in_a_hard_key_into_easy_chord(tmp_path, monkeypatch):
    from lyricvideo.key_decision import save_owner_key

    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band", genre="Pop", key="C minor", chords=["C", "F", "G"])
    save_owner_key(work_dir, "C major")

    from lyricvideo.youtube_playlists import organize_video
    client = _FakeYoutubeClient()
    organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), work_dir)

    assert is_video_in_playlist(client, playlist_ids["easy_chord"], "vid123")


def test_the_owners_own_key_beats_an_audited_override(tmp_path, monkeypatch):
    # key_owner.json always wins (CLAUDE.md) -- a later Set Key answer outranks the older audit file's key.
    from lyricvideo.key_decision import save_owner_key

    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    work_dir = _make_work_dir(tmp_path, artist="Made Up Band", genre="Pop", key="C minor", chords=["C", "F", "G"])
    save_owner_key(work_dir, "C major")

    from lyricvideo.youtube_playlists import organize_video
    client = _FakeYoutubeClient()
    organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), work_dir, key_override="C minor")

    assert is_video_in_playlist(client, playlist_ids["easy_chord"], "vid123")


def test_an_easy_chord_version_follows_its_own_shape_key_not_the_songs(tmp_path, monkeypatch):
    playlist_ids, _genres, _comments = _patch_state(monkeypatch)
    song_dir = _make_work_dir(tmp_path, artist="Made Up Band", genre="Pop", key="Eb major")
    easy_dir = song_dir / "easychords"
    easy_dir.mkdir()
    (easy_dir / "song_info.json").write_text(json.dumps({"title": "My Song EasyChords", "artist": "Made Up Band",
                                                         "genre": "Pop"}), encoding="utf-8")
    save_song(Song(title="My Song EasyChords", audio_path="a.mp3", lines=[LyricLine(words=[Word(word="la")])],
                   chord_track=ChordTrack(key="D major", events=[])), easy_dir / "lyrics_timed.json")
    save_youtube_state(easy_dir, YoutubeState(video_id="easy1", uploaded_at="2026-09-10T15:00:00", title="t"))

    from lyricvideo.youtube_playlists import organize_video
    client = _FakeYoutubeClient()
    organize_video(client, _FakeAnthropicClient("COMMENT: Hi!"), easy_dir, key_override="Eb major")

    assert is_video_in_playlist(client, playlist_ids["easy_chord"], "easy1")


# --- a lost playlist cache re-finds the channel's playlists instead of duplicating them (issue #7 review, F143/F144) ---

def test_a_cache_miss_reuses_the_channels_existing_playlist_with_that_title(monkeypatch):
    from lyricvideo.youtube_playlists import ALL_PLAYLIST_DESCRIPTION, ALL_PLAYLIST_TITLE, get_or_create_playlist

    saved = {}
    monkeypatch.setattr("lyricvideo.youtube_playlists.load_playlist_ids", lambda: {})
    monkeypatch.setattr("lyricvideo.youtube_playlists.save_playlist_id", lambda key, pid: saved.__setitem__(key, pid))
    client = _FakeYoutubeClient()
    client._playlists.existing_playlist_ids.add("PL_OLD")
    client._playlists.titles["PL_OLD"] = ALL_PLAYLIST_TITLE

    assert get_or_create_playlist(client, "all", ALL_PLAYLIST_TITLE, ALL_PLAYLIST_DESCRIPTION) == "PL_OLD"
    assert client._playlists.inserted_titles == []
    assert saved == {"all": "PL_OLD"}


def test_two_overlapping_organizes_create_a_new_artist_playlist_only_once(tmp_path, monkeypatch):
    import threading
    import time as _time

    from lyricvideo.youtube_playlists import get_or_create_playlist

    cache = {}
    monkeypatch.setattr("lyricvideo.youtube_playlists.load_playlist_ids", lambda: dict(cache))
    monkeypatch.setattr("lyricvideo.youtube_playlists.save_playlist_id", lambda key, pid: cache.__setitem__(key, pid))
    client = _FakeYoutubeClient()
    real_insert = client._playlists.insert

    def slow_insert(part, body):
        _time.sleep(0.05)                    # widen the check -> create window
        return real_insert(part, body)

    client._playlists.insert = slow_insert
    results = []
    threads = [threading.Thread(target=lambda: results.append(get_or_create_playlist(
        client, "artist:Made Up Band", "Made Up Band - Play Along Videos", "d"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1
    assert client._playlists.inserted_titles == ["Made Up Band - Play Along Videos"]


# --- the fixed playlists' public descriptions match the rule (issue #7 review, F145) ----------------------------------

def test_the_easy_playlist_descriptions_name_exactly_the_keys_is_easy_key_accepts():
    from lyricvideo.chord_theory import NOTES_SHARP, is_easy_key
    from lyricvideo.youtube_playlists import (
        EASY_CHORD_PLAYLIST_DESCRIPTION, FOUR_CHORD_PLAYLIST_DESCRIPTION, THREE_CHORD_PLAYLIST_DESCRIPTION,
    )

    for description in (EASY_CHORD_PLAYLIST_DESCRIPTION, THREE_CHORD_PLAYLIST_DESCRIPTION, FOUR_CHORD_PLAYLIST_DESCRIPTION):
        assert "natural-tonic" not in description
        assert "C, D, E, G or A major; A, D or E minor" in description
    assert [n for n in NOTES_SHARP if is_easy_key(f"{n} major")] == ["C", "D", "E", "G", "A"]
    assert sorted(n for n in NOTES_SHARP if is_easy_key(f"{n} minor")) == ["A", "D", "E"]


def test_stale_playlist_descriptions_finds_the_old_easy_chord_text(monkeypatch):
    from lyricvideo.youtube_playlists import (
        ALL_PLAYLIST_DESCRIPTION, EASY_CHORD_PLAYLIST_DESCRIPTION, stale_playlist_descriptions,
    )

    monkeypatch.setattr("lyricvideo.youtube_playlists.load_playlist_ids", lambda: {"all": "PLA", "easy_chord": "PLE"})
    client = _FakeYoutubeClient()
    client._playlists.existing_playlist_ids.update({"PLA", "PLE"})
    client._playlists.descriptions.update({
        "PLA": ALL_PLAYLIST_DESCRIPTION,
        "PLE": "Play-along videos in a natural-tonic key (C, D, E, F, G, A or B, major or minor) -- no sharp or flat key.",
    })

    stale = stale_playlist_descriptions(client)

    assert [(key, pid) for key, pid, *_ in stale] == [("easy_chord", "PLE")]
    assert stale[0][4] == EASY_CHORD_PLAYLIST_DESCRIPTION


def _make_http_error(status: int):
    from googleapiclient.errors import HttpError
    from types import SimpleNamespace

    resp = SimpleNamespace(status=status, reason="")
    return HttpError(resp, b'{"error": {"message": "boom"}}')


def test_add_video_to_playlist_with_retry_succeeds_after_a_transient_playlist_not_found(monkeypatch):
    """Real incident, 2026-09-18: a playlist get_or_create_playlist() just
    created can briefly 404 as playlistNotFound on the very next
    playlistItems() call -- a known Google API propagation lag, not a
    genuine problem."""
    from lyricvideo.youtube_playlists import add_video_to_playlist_with_retry

    calls = []

    def flaky(youtube_client, playlist_id, video_id):
        calls.append(True)
        if len(calls) < 3:
            raise _make_http_error(404)

    monkeypatch.setattr("lyricvideo.youtube_playlists.add_video_to_playlist", flaky)
    slept = []
    monkeypatch.setattr("lyricvideo.youtube_playlists.time.sleep", lambda s: slept.append(s))

    add_video_to_playlist_with_retry("client", "PL1", "vid123")  # must not raise

    assert len(calls) == 3
    assert len(slept) == 2  # slept between attempts, not after the final success


def test_add_video_to_playlist_with_retry_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.youtube_playlists.add_video_to_playlist",
        lambda *a, **k: (_ for _ in ()).throw(_make_http_error(404)),
    )
    monkeypatch.setattr("lyricvideo.youtube_playlists.time.sleep", lambda s: None)

    from lyricvideo.youtube_playlists import add_video_to_playlist_with_retry
    try:
        add_video_to_playlist_with_retry("client", "PL1", "vid123")
        assert False, "expected an HttpError"
    except Exception as e:
        assert e.status_code == 404


def test_add_video_to_playlist_with_retry_reraises_an_unrelated_error_immediately(monkeypatch):
    """A quota-exceeded 429 (or anything else that isn't a 404) would never
    resolve by waiting -- it must propagate straight through so gui.py's
    quota-halt logic still sees it right away, not after a pointless delay."""
    calls = []
    monkeypatch.setattr(
        "lyricvideo.youtube_playlists.add_video_to_playlist",
        lambda *a, **k: calls.append(True) or (_ for _ in ()).throw(_make_http_error(429)),
    )
    monkeypatch.setattr(
        "lyricvideo.youtube_playlists.time.sleep",
        lambda s: (_ for _ in ()).throw(AssertionError("should not sleep for a non-404 error")),
    )

    from lyricvideo.youtube_playlists import add_video_to_playlist_with_retry
    try:
        add_video_to_playlist_with_retry("client", "PL1", "vid123")
        assert False, "expected an HttpError"
    except Exception as e:
        assert e.status_code == 429

    assert len(calls) == 1  # only tried once -- no retry for a non-404
