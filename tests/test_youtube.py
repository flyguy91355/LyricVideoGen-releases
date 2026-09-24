from datetime import datetime, timezone

from types import SimpleNamespace

from lyricvideo.youtube import (
    add_video_to_playlist, create_playlist, find_playlist_by_id, get_video_snippet, is_quota_exceeded_error,
    is_video_in_playlist, is_video_public, list_new_comments, post_reply, post_top_level_comment,
    remove_video_from_playlist, reserved_publish_datetimes, update_video_description, upload_video, video_exists,
)


class _FakeUploadRequest:
    def __init__(self, video_id: str):
        self._video_id = video_id

    def next_chunk(self):
        return None, {"id": self._video_id}


class _FakeListRequest:
    def __init__(self, items: list):
        self._items = items

    def execute(self):
        return {"items": self._items}


class _FakeUpdateRequest:
    def execute(self):
        return {}


class _FakeVideosResource:
    def __init__(self, video_id: str):
        self._video_id = video_id
        self.insert_kwargs = None
        self.update_kwargs = None
        self.existing_video_ids: set[str] = set()
        self.snippets: dict[str, dict] = {}
        self.statuses: dict[str, dict] = {}

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeUploadRequest(self._video_id)

    def list(self, part, id):
        if id not in self.existing_video_ids:
            return _FakeListRequest([])
        item = {"id": id}
        if id in self.snippets:
            item["snippet"] = self.snippets[id]
        if id in self.statuses:
            item["status"] = self.statuses[id]
        return _FakeListRequest([item])

    def update(self, **kwargs):
        self.update_kwargs = kwargs
        self.snippets[kwargs["body"]["id"]] = kwargs["body"]["snippet"]
        return _FakeUpdateRequest()


class _FakeYoutubeClient:
    def __init__(self, video_id: str = "abc123"):
        self._videos = _FakeVideosResource(video_id)

    def videos(self):
        return self._videos


def test_upload_video_returns_the_new_video_id(tmp_path):
    video_path = tmp_path / "song.mp4"
    video_path.write_bytes(b"fake video bytes")
    client = _FakeYoutubeClient(video_id="abc123")

    video_id = upload_video(
        client, video_path, "My Title", "My description", ["tag1", "tag2"],
        privacy="unlisted", publish_at=None, category_id="26", made_for_kids=False,
    )

    assert video_id == "abc123"
    body = client._videos.insert_kwargs["body"]
    assert body["snippet"]["title"] == "My Title"
    assert body["snippet"]["description"] == "My description"
    assert body["snippet"]["tags"] == ["tag1", "tag2"]
    assert body["snippet"]["categoryId"] == "26"
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert "publishAt" not in body["status"]


def test_upload_video_sets_publish_at_as_utc_when_given(tmp_path):
    video_path = tmp_path / "song.mp4"
    video_path.write_bytes(b"fake video bytes")
    client = _FakeYoutubeClient()
    when = datetime(2026, 9, 15, 15, 0, 0, tzinfo=timezone.utc)

    upload_video(
        client, video_path, "T", "D", [], privacy="private", publish_at=when,
        category_id="26", made_for_kids=False,
    )

    assert client._videos.insert_kwargs["body"]["status"]["publishAt"] == "2026-09-15T15:00:00.0Z"
    assert client._videos.insert_kwargs["body"]["status"]["privacyStatus"] == "private"


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeCommentThreadsResource:
    def __init__(self, items):
        self._items = items
        self.insert_kwargs = None

    def list(self, **kwargs):
        return self

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeExecutable({"id": "thread-new"})

    def execute(self):
        return {"items": self._items}


class _FakeCommentsInsertRequest:
    def __init__(self, sink):
        self._sink = sink

    def execute(self):
        self._sink.append(None)
        return {}


class _FakeCommentsResource:
    def __init__(self):
        self.insert_kwargs = None
        self.executed = []

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeCommentsInsertRequest(self.executed)


class _FakeCommentYoutubeClient:
    def __init__(self, items=None):
        self._comment_threads = _FakeCommentThreadsResource(items or [])
        self._comments = _FakeCommentsResource()
        self._channels_resource = SimpleNamespace(list=lambda part, mine: _FakeExecutable(
            {"items": [{"id": "UCmychannel"}]}
        ))

    def commentThreads(self):
        return self._comment_threads

    def comments(self):
        return self._comments

    def channels(self):
        return self._channels_resource


def _comment_thread_item(comment_id, author, text, published_at="2026-09-10T00:00:00Z"):
    return {
        "snippet": {
            "topLevelComment": {
                "id": comment_id,
                "snippet": {"authorDisplayName": author, "textDisplay": text, "publishedAt": published_at},
            }
        }
    }


def test_list_new_comments_returns_only_unseen_comments():
    items = [
        _comment_thread_item("c1", "Alice", "Great video!"),
        _comment_thread_item("c2", "Bob", "Chord at 1:30 looks wrong"),
    ]
    client = _FakeCommentYoutubeClient(items)

    comments = list_new_comments(client, "vid123", seen_comment_ids={"c1"})

    assert len(comments) == 1
    assert comments[0].comment_id == "c2"
    assert comments[0].author == "Bob"
    assert comments[0].video_id == "vid123"
    assert comments[0].text == "Chord at 1:30 looks wrong"


def test_list_new_comments_returns_empty_when_all_seen():
    items = [_comment_thread_item("c1", "Alice", "Great video!")]
    client = _FakeCommentYoutubeClient(items)

    assert list_new_comments(client, "vid123", seen_comment_ids={"c1"}) == []


def test_post_reply_sends_the_correct_parent_and_text():
    client = _FakeCommentYoutubeClient()

    post_reply(client, "c2", "Thanks for catching that!")

    assert client._comments.insert_kwargs["body"]["snippet"]["parentId"] == "c2"
    assert client._comments.insert_kwargs["body"]["snippet"]["textOriginal"] == "Thanks for catching that!"
    assert client._comments.executed == [None]


def test_post_top_level_comment_sends_video_channel_and_text():
    client = _FakeCommentYoutubeClient()

    comment_id = post_top_level_comment(client, "vid1", "Which instrument are you playing along with?")

    body = client._comment_threads.insert_kwargs["body"]["snippet"]
    assert body["videoId"] == "vid1"
    assert body["channelId"] == "UCmychannel"
    assert body["topLevelComment"]["snippet"]["textOriginal"] == "Which instrument are you playing along with?"
    assert comment_id == "thread-new"


class _FakePlaylistsResource:
    def __init__(self):
        self.insert_kwargs = None
        self.existing_playlist_ids: set[str] = set()
        self.next_id = "PLnew"

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeExecutable({"id": self.next_id})

    def list(self, part, id):
        requested = set(id.split(","))
        items = [{"id": pid} for pid in requested if pid in self.existing_playlist_ids]
        return _FakeExecutable({"items": items})


class _FakePlaylistItemsResource:
    def __init__(self):
        self.insert_kwargs = None
        self.deleted_item_ids: list[str] = []
        self.members: dict[str, set[str]] = {}
        self._item_ids: dict[tuple[str, str], str] = {}
        self._next_item_id = 0
        self.gone_video_ids: set[str] = set()   # deleted directly on YouTube -- list() 404s, not an empty result

    def list(self, part, playlistId, videoId):
        if videoId in self.gone_video_ids:
            from googleapiclient.errors import HttpError

            resp = SimpleNamespace(status=404, reason="Not Found")
            content = (
                b'{"error": {"code": 404, "errors": [{"message": "Video not found.", '
                b'"domain": "youtube.playlistItem", "reason": "videoNotFound"}]}}'
            )
            raise HttpError(resp, content)
        if videoId not in self.members.get(playlistId, set()):
            return _FakeExecutable({"items": []})
        item_id = self._item_ids.get((playlistId, videoId), "item1")  # a member set up directly in a test fixture
        return _FakeExecutable({"items": [{"id": item_id}]})

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        snippet = kwargs["body"]["snippet"]
        playlist_id, video_id = snippet["playlistId"], snippet["resourceId"]["videoId"]
        self.members.setdefault(playlist_id, set()).add(video_id)
        self._next_item_id += 1
        item_id = f"item{self._next_item_id}"
        self._item_ids[(playlist_id, video_id)] = item_id
        return _FakeExecutable({"id": item_id})

    def delete(self, id):
        self.deleted_item_ids.append(id)
        for (playlist_id, video_id), item_id in list(self._item_ids.items()):
            if item_id == id:
                self.members[playlist_id].discard(video_id)
                del self._item_ids[(playlist_id, video_id)]
        return _FakeExecutable({})


class _FakePlaylistYoutubeClient:
    def __init__(self):
        self._playlists = _FakePlaylistsResource()
        self._playlist_items = _FakePlaylistItemsResource()

    def playlists(self):
        return self._playlists

    def playlistItems(self):
        return self._playlist_items


def test_create_playlist_returns_the_new_playlist_id_and_sends_title_and_description():
    client = _FakePlaylistYoutubeClient()

    playlist_id = create_playlist(client, "Pink Floyd - Play Along Videos", "Every Pink Floyd video.")

    assert playlist_id == "PLnew"
    body = client._playlists.insert_kwargs["body"]
    assert body["snippet"]["title"] == "Pink Floyd - Play Along Videos"
    assert body["snippet"]["description"] == "Every Pink Floyd video."


def test_find_playlist_by_id_true_when_it_still_exists():
    client = _FakePlaylistYoutubeClient()
    client._playlists.existing_playlist_ids = {"PL1"}

    assert find_playlist_by_id(client, "PL1") is True


def test_find_playlist_by_id_false_when_deleted_directly_on_youtube():
    client = _FakePlaylistYoutubeClient()

    assert find_playlist_by_id(client, "PL1") is False


def test_is_video_in_playlist_false_when_not_a_member():
    client = _FakePlaylistYoutubeClient()

    assert is_video_in_playlist(client, "PL1", "vid1") is False


def test_is_video_in_playlist_true_when_a_member():
    client = _FakePlaylistYoutubeClient()
    client._playlist_items.members["PL1"] = {"vid1"}

    assert is_video_in_playlist(client, "PL1", "vid1") is True


def test_add_video_to_playlist_adds_a_new_member():
    client = _FakePlaylistYoutubeClient()

    add_video_to_playlist(client, "PL1", "vid1")

    assert is_video_in_playlist(client, "PL1", "vid1") is True


def test_add_video_to_playlist_is_a_noop_when_already_a_member():
    """Backfill re-runs must never duplicate a playlist entry."""
    client = _FakePlaylistYoutubeClient()
    client._playlist_items.members["PL1"] = {"vid1"}

    add_video_to_playlist(client, "PL1", "vid1")

    assert client._playlist_items.insert_kwargs is None


def test_remove_video_from_playlist_removes_a_real_member():
    """Owner, 2026-09-23: cleaning up songs that were added to EASY CHORD under the old (wider) is_easy_key
    rule, now that F/B no longer count as easy."""
    client = _FakePlaylistYoutubeClient()
    add_video_to_playlist(client, "PL1", "vid1")

    removed = remove_video_from_playlist(client, "PL1", "vid1")

    assert removed is True
    assert is_video_in_playlist(client, "PL1", "vid1") is False


def test_remove_video_from_playlist_is_a_noop_when_not_a_member():
    client = _FakePlaylistYoutubeClient()

    removed = remove_video_from_playlist(client, "PL1", "vid1")

    assert removed is False
    assert client._playlist_items.deleted_item_ids == []


def test_remove_video_from_playlist_is_a_noop_when_the_video_was_deleted_on_youtube():
    """Real incident, 2026-09-23, running scripts/fix_playlist_data_20260923.py live: a video listed in a
    song's own youtube_state.json no longer existed on YouTube at all, and playlistItems().list() 404s as
    videoNotFound for a gone video -- a different failure mode than "not a member" (which 200s with an empty
    items list). A video that's gone is obviously not a member of anything either -- nothing to remove,
    not an error."""
    client = _FakePlaylistYoutubeClient()
    client._playlist_items.gone_video_ids.add("vid1")

    removed = remove_video_from_playlist(client, "PL1", "vid1")

    assert removed is False
    assert client._playlist_items.deleted_item_ids == []


def test_video_exists_true_when_the_video_id_is_still_live():
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = {"abc123"}

    assert video_exists(client, "abc123") is True


def test_video_exists_false_when_the_video_was_deleted():
    """Real incident 2026-09-10: the owner deleted a video directly on
    YouTube; nothing in the app ever noticed its saved video_id had gone
    stale until this check was added."""
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = set()

    assert video_exists(client, "abc123") is False


def test_is_video_public_true_for_a_public_video():
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = {"abc123"}
    client._videos.statuses["abc123"] = {"privacyStatus": "public"}

    assert is_video_public(client, "abc123") is True


def test_is_video_public_false_for_a_still_scheduled_private_video():
    """Real incident, 2026-09-13: the comment checker was hitting a
    commentsDisabled 403 on every video that's still private/scheduled --
    a still-scheduled video can't accept comment reads at all, so callers
    should check this first instead of catching a call known to fail."""
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = {"abc123"}
    client._videos.statuses["abc123"] = {"privacyStatus": "private"}

    assert is_video_public(client, "abc123") is False


def test_is_video_public_false_for_a_deleted_video():
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = set()

    assert is_video_public(client, "abc123") is False


def test_get_video_snippet_returns_the_real_snippet():
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = {"abc123"}
    client._videos.snippets["abc123"] = {
        "title": "T", "description": "D", "tags": ["a"], "categoryId": "10",
    }

    snippet = get_video_snippet(client, "abc123")

    assert snippet == {"title": "T", "description": "D", "tags": ["a"], "categoryId": "10"}


def test_get_video_snippet_returns_none_when_video_is_gone():
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = set()

    assert get_video_snippet(client, "abc123") is None


def test_update_video_description_preserves_every_other_snippet_field():
    """videos.update(part='snippet') REPLACES THE WHOLE snippet -- blindly
    sending {"description": ...} alone would wipe the title/tags/category,
    the same real gotcha this project already hit with channels.update."""
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = {"abc123"}
    client._videos.snippets["abc123"] = {
        "title": "T", "description": "old", "tags": ["a", "b"], "categoryId": "10",
    }

    update_video_description(client, "abc123", "new description")

    body = client._videos.update_kwargs["body"]
    assert body["snippet"]["description"] == "new description"
    assert body["snippet"]["title"] == "T"
    assert body["snippet"]["tags"] == ["a", "b"]
    assert body["snippet"]["categoryId"] == "10"


def test_update_video_description_is_a_noop_when_video_no_longer_exists():
    client = _FakeYoutubeClient(video_id="abc123")
    client._videos.existing_video_ids = set()

    update_video_description(client, "abc123", "new description")

    assert client._videos.update_kwargs is None


class _FakeChannelUploadsClient:
    """A channel with a fixed uploads-playlist history, for
    reserved_publish_datetimes() -- distinct from _FakeYoutubeClient above
    since that one's videos().list() takes a single id, not a comma-
    joined batch."""

    def __init__(self, videos: list[dict]):
        self._videos = videos

    def channels(self):
        return SimpleNamespace(list=lambda part, mine: SimpleNamespace(execute=lambda: {
            "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "uploads-playlist"}}}]
        }))

    def playlistItems(self):
        videos = self._videos

        def list_(part, playlistId, maxResults, pageToken):
            return SimpleNamespace(execute=lambda: {
                "items": [{"contentDetails": {"videoId": v["id"]}} for v in videos],
            })

        return SimpleNamespace(list=list_)

    def videos(self):
        videos = self._videos

        def list_(part, id):
            requested = set(id.split(","))
            items = [v for v in videos if v["id"] in requested]
            return SimpleNamespace(execute=lambda: {"items": items})

        return SimpleNamespace(list=list_)


def test_reserved_publish_datetimes_includes_both_scheduled_and_published_videos():
    client = _FakeChannelUploadsClient([
        {"id": "old-public", "status": {}, "snippet": {"publishedAt": "2026-09-01T18:00:00Z"}},
        {"id": "still-scheduled", "status": {"publishAt": "2026-09-20T18:00:00Z"}, "snippet": {}},
    ])

    result = reserved_publish_datetimes(client)

    assert result == {
        datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc).astimezone(),
        datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc).astimezone(),
    }


def test_reserved_publish_datetimes_returns_empty_set_for_an_empty_channel():
    assert reserved_publish_datetimes(_FakeChannelUploadsClient([])) == set()


def test_reserved_publish_datetimes_ignores_videos_with_neither_field():
    client = _FakeChannelUploadsClient([
        {"id": "processing", "status": {}, "snippet": {}},
        {"id": "real", "status": {}, "snippet": {"publishedAt": "2026-09-05T12:00:00Z"}},
    ])

    result = reserved_publish_datetimes(client)

    assert result == {datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc).astimezone()}


def _make_http_error(status: int):
    from googleapiclient.errors import HttpError

    resp = SimpleNamespace(status=status, reason="")
    return HttpError(resp, b'{"error": {"message": "boom"}}')


def test_is_quota_exceeded_error_true_for_a_429():
    assert is_quota_exceeded_error(_make_http_error(429)) is True


def test_is_quota_exceeded_error_false_for_a_different_http_status():
    assert is_quota_exceeded_error(_make_http_error(404)) is False


def test_is_quota_exceeded_error_false_for_a_non_http_error():
    assert is_quota_exceeded_error(RuntimeError("boom")) is False


def _make_upload_limit_error():
    """The exact error a real upload got on 2026-09-19: a ResumableUploadError
    (HttpError subclass) with HTTP 400 + reason uploadLimitExceeded, not a 429."""
    from googleapiclient.errors import ResumableUploadError

    resp = SimpleNamespace(status=400, reason="Bad Request")
    content = (
        b'{"error": {"code": 400, "message": "The user has exceeded the number of videos they may upload.", '
        b'"errors": [{"message": "The user has exceeded the number of videos they may upload.", '
        b'"domain": "youtube.video", "reason": "uploadLimitExceeded"}]}}'
    )
    return ResumableUploadError(resp, content)


def test_is_quota_exceeded_error_true_for_a_400_upload_limit_exceeded():
    # Real incident, 2026-09-19: every song in a Batch kept retrying the upload
    # because this 400 (not a 429) was never recognized, so no cooldown was set.
    assert is_quota_exceeded_error(_make_upload_limit_error()) is True


def test_is_quota_exceeded_error_false_for_a_400_with_a_different_reason():
    from googleapiclient.errors import HttpError

    resp = SimpleNamespace(status=400, reason="Bad Request")
    content = b'{"error": {"code": 400, "errors": [{"domain": "youtube.video", "reason": "invalidTitle"}]}}'
    assert is_quota_exceeded_error(HttpError(resp, content)) is False
