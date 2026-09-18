import json
from pathlib import Path

from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.youtube import is_video_in_playlist
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


def _make_work_dir(tmp_path: Path, artist: str, genre: str = "", video_id: str = "vid123") -> Path:
    work_dir = tmp_path / "my-song"
    work_dir.mkdir()
    info = {"title": "My Song", "artist": artist, "duration": 200.0, "alt_titles": []}
    if genre:
        info["genre"] = genre
    (work_dir / "song_info.json").write_text(json.dumps(info), encoding="utf-8")
    song = Song(title="My Song", audio_path="song.mp3", lines=[LyricLine(words=[Word(word="hello")])])
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
    from lyricvideo.youtube_playlists import _add_video_to_playlist_with_retry

    calls = []

    def flaky(youtube_client, playlist_id, video_id):
        calls.append(True)
        if len(calls) < 3:
            raise _make_http_error(404)

    monkeypatch.setattr("lyricvideo.youtube_playlists.add_video_to_playlist", flaky)
    slept = []
    monkeypatch.setattr("lyricvideo.youtube_playlists.time.sleep", lambda s: slept.append(s))

    _add_video_to_playlist_with_retry("client", "PL1", "vid123")  # must not raise

    assert len(calls) == 3
    assert len(slept) == 2  # slept between attempts, not after the final success


def test_add_video_to_playlist_with_retry_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.youtube_playlists.add_video_to_playlist",
        lambda *a, **k: (_ for _ in ()).throw(_make_http_error(404)),
    )
    monkeypatch.setattr("lyricvideo.youtube_playlists.time.sleep", lambda s: None)

    from lyricvideo.youtube_playlists import _add_video_to_playlist_with_retry
    try:
        _add_video_to_playlist_with_retry("client", "PL1", "vid123")
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

    from lyricvideo.youtube_playlists import _add_video_to_playlist_with_retry
    try:
        _add_video_to_playlist_with_retry("client", "PL1", "vid123")
        assert False, "expected an HttpError"
    except Exception as e:
        assert e.status_code == 429

    assert len(calls) == 1  # only tried once -- no retry for a non-404
