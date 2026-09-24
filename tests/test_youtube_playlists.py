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

    def insert(self, part, body):
        self._next_id += 1
        new_id = f"PL{self._next_id}"
        self.existing_playlist_ids.add(new_id)
        return _Exec({"id": new_id})

    def list(self, part, id):
        requested = set(id.split(","))
        items = [{"id": pid} for pid in requested if pid in self.existing_playlist_ids]
        return _Exec({"items": items})


class _FakePlaylistItemsResource:
    def __init__(self):
        self.members: dict[str, set[str]] = {}

    def list(self, part, playlistId, videoId):
        is_member = videoId in self.members.get(playlistId, set())
        return _Exec({"items": [{"id": "item1"}] if is_member else []})

    def insert(self, part, body):
        snippet = body["snippet"]
        self.members.setdefault(snippet["playlistId"], set()).add(snippet["resourceId"]["videoId"])
        return _Exec({"id": "item-new"})


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
