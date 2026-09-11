from datetime import datetime, timezone
from pathlib import Path

from lyricvideo.youtube import (
    get_video_snippet, list_new_comments, post_reply, update_video_description, upload_video, video_exists,
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

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeUploadRequest(self._video_id)

    def list(self, part, id):
        if id not in self.existing_video_ids:
            return _FakeListRequest([])
        item = {"id": id}
        if id in self.snippets:
            item["snippet"] = self.snippets[id]
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


class _FakeCommentThreadsResource:
    def __init__(self, items):
        self._items = items

    def list(self, **kwargs):
        return self

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

    def commentThreads(self):
        return self._comment_threads

    def comments(self):
        return self._comments


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
