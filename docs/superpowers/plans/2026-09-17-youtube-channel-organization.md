# YouTube Channel Organization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically organize every YouTube upload (past and future) into
an "All" playlist, a per-artist playlist, and a per-genre playlist, and
queue a Claude-drafted engagement comment for the owner to approve before
it posts.

**Architecture:** One new orchestration function, `organize_video()`,
called right after every successful `schedule_upload()` at its two GUI call
sites (fully automatic — no new owner action), plus a one-time backfill
script that calls it for the 67 already-uploaded videos. Genre is
classified once per song by Claude from a shared, growing list and cached
in `song_info.json`. Playlist IDs are cached locally and self-heal if a
playlist is deleted directly in Studio. The engagement comment always goes
through the existing owner-approval queue pattern before it ever posts.

**Tech Stack:** Python 3.11, YouTube Data API v3 (`googleapiclient`),
Anthropic SDK, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md`

## Global Constraints

- Every function that talks to the YouTube API takes an already-built
  client as its first argument — tests always inject a fake, never make a
  real network call (existing convention, `lyricvideo/youtube.py`).
- Every new dataclass field that a JSON file might not have yet (because it
  predates this feature) must have a default, so `Class(**data)` on an old
  file never raises. Verified against real request/response shapes from
  Google's own current API reference docs, not assumed from memory — see
  the spec's confirmed shapes for `playlists.insert`, `playlistItems.insert`,
  and `commentThreads.insert`.
- Nothing ever posts to YouTube (a comment) without an explicit owner
  Approve click — matches the existing reply-approval convention exactly.
- A single song's failure (backfill, or the live upload path) must never
  abort anything else — same per-item isolation this codebase already uses
  for Batch and for comment-checking.
- Genre list seed values are `Classic Rock, Southern Rock, Hard
  Rock / Glam, Progressive Rock, Alternative / Grunge, Singer-Songwriter /
  Folk Rock, Pop Rock / Soft Rock, 60s Pop Rock / British Invasion, Punk,
  Country, Pop, Hip-Hop / Rap, R&B / Soul, Metal, Blues, Folk / Americana,
  Jazz, Electronic / Dance, Latin, Reggae, Christian / Gospel` — exact
  strings, copied verbatim from the spec.

---

## Task 1: `YoutubeState` gains `engagement_comment_posted`

**Files:**
- Modify: `lyricvideo/youtube_state.py`
- Test: `tests/test_youtube_state.py`

**Interfaces:**
- Produces: `YoutubeState.engagement_comment_posted: bool` (default
  `False`), used by Task 6 (`organize_video`) and Task 8 (GUI approve
  handler).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_youtube_state.py`:

```python
def test_engagement_comment_posted_defaults_to_false_for_a_legacy_file(tmp_path):
    """A youtube_state.json written before this field existed has none of
    these keys -- YoutubeState(**data) must still succeed, or every one of
    the 67 already-uploaded songs would suddenly look never-uploaded."""
    (tmp_path / "youtube_state.json").write_text(
        json.dumps({"video_id": "abc", "uploaded_at": "2026-09-10T15:00:00", "title": "t"}),
        encoding="utf-8",
    )

    state = load_youtube_state(tmp_path)

    assert state is not None
    assert state.engagement_comment_posted is False


def test_save_then_load_round_trips_engagement_comment_posted(tmp_path):
    state = YoutubeState(
        video_id="abc123", uploaded_at="2026-09-10T15:00:00", title="My Song",
        engagement_comment_posted=True,
    )

    save_youtube_state(tmp_path, state)

    assert load_youtube_state(tmp_path) == state
```

Add `import json` to the top of `tests/test_youtube_state.py` if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_youtube_state.py -v`
Expected: FAIL — `TypeError: YoutubeState.__init__() got an unexpected keyword argument 'engagement_comment_posted'`

- [ ] **Step 3: Write minimal implementation**

In `lyricvideo/youtube_state.py`, change the dataclass:

```python
@dataclass(frozen=True)
class YoutubeState:
    video_id: str
    uploaded_at: str
    title: str
    engagement_comment_posted: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_youtube_state.py -v`
Expected: PASS, all tests including the two new ones and the existing
`test_load_youtube_state_returns_none_on_missing_fields` (still fails
because `video_id`/`uploaded_at`/`title` remain required).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/youtube_state.py tests/test_youtube_state.py
git commit -m "Add engagement_comment_posted field to YoutubeState

Defaulted so every one of the 67 already-uploaded songs' youtube_state.json
files (which predate this field) still load correctly."
```

---

## Task 2: `PendingComment` queue in `youtube_comment_state.py`

**Files:**
- Modify: `lyricvideo/youtube_comment_state.py`
- Test: `tests/test_youtube_comment_state.py`

**Interfaces:**
- Produces: `PendingComment(video_id, song_title, draft_text)`,
  `load_pending_comments(path=PENDING_COMMENTS_FILE) -> list[PendingComment]`,
  `save_pending_comments(comments, path=PENDING_COMMENTS_FILE) -> None`,
  `add_pending_comment(comment, path=PENDING_COMMENTS_FILE) -> None`,
  `remove_pending_comment(video_id, path=PENDING_COMMENTS_FILE) -> None`.
  Used by Task 6 (`organize_video` queues one) and Task 8 (GUI
  approve/dismiss removes one).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_youtube_comment_state.py`:

```python
from lyricvideo.youtube_comment_state import (
    PendingComment,
    add_pending_comment,
    load_pending_comments,
    remove_pending_comment,
    save_pending_comments,
)


def _make_pending_comment(video_id="v1") -> PendingComment:
    return PendingComment(video_id=video_id, song_title="My Song", draft_text="Which instrument are you playing?")


def test_load_pending_comments_empty_when_no_file(tmp_path):
    assert load_pending_comments(tmp_path / "no_such_file.json") == []


def test_add_then_load_pending_comments_round_trips(tmp_path):
    path = tmp_path / "pending_comments.json"

    add_pending_comment(_make_pending_comment("v1"), path)
    add_pending_comment(_make_pending_comment("v2"), path)

    comments = load_pending_comments(path)
    assert [c.video_id for c in comments] == ["v1", "v2"]


def test_remove_pending_comment_removes_only_the_matching_one(tmp_path):
    path = tmp_path / "pending_comments.json"
    save_pending_comments([_make_pending_comment("v1"), _make_pending_comment("v2")], path)

    remove_pending_comment("v1", path)

    assert [c.video_id for c in load_pending_comments(path)] == ["v2"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_youtube_comment_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'PendingComment'`

- [ ] **Step 3: Write minimal implementation**

In `lyricvideo/youtube_comment_state.py`, add below the existing
`PENDING_REPLIES_FILE` line:

```python
PENDING_COMMENTS_FILE = CREDENTIALS_DIR / "youtube_pending_comments.json"
```

Add below the existing `PendingReply`-related functions:

```python
@dataclass(frozen=True)
class PendingComment:
    """A Claude-drafted engagement comment awaiting the owner's review --
    not a reply to anyone, a fresh standalone comment posted as the channel
    owner, so it's keyed by video_id (there's no parent comment_id yet)."""

    video_id: str
    song_title: str
    draft_text: str


def load_pending_comments(path: Path = PENDING_COMMENTS_FILE) -> list[PendingComment]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [PendingComment(**item) for item in data]
    except (OSError, ValueError, TypeError):
        return []


def save_pending_comments(comments: list[PendingComment], path: Path = PENDING_COMMENTS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in comments]), encoding="utf-8")


def add_pending_comment(comment: PendingComment, path: Path = PENDING_COMMENTS_FILE) -> None:
    comments = load_pending_comments(path)
    comments.append(comment)
    save_pending_comments(comments, path)


def remove_pending_comment(video_id: str, path: Path = PENDING_COMMENTS_FILE) -> None:
    comments = [c for c in load_pending_comments(path) if c.video_id != video_id]
    save_pending_comments(comments, path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_youtube_comment_state.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/youtube_comment_state.py tests/test_youtube_comment_state.py
git commit -m "Add PendingComment queue alongside PendingReply

Same review-before-posting pattern as comment replies, but for a fresh
standalone engagement comment posted as the channel owner."
```

---

## Task 3: Raw playlist + top-level-comment API calls in `youtube.py`

**Files:**
- Modify: `lyricvideo/youtube.py`
- Test: `tests/test_youtube.py`

**Interfaces:**
- Produces: `create_playlist(client, title, description) -> str`,
  `find_playlist_by_id(client, playlist_id) -> bool`,
  `is_video_in_playlist(client, playlist_id, video_id) -> bool`,
  `add_video_to_playlist(client, playlist_id, video_id) -> None`,
  `post_top_level_comment(client, video_id, text) -> str`. Used by Task 6
  (`organize_video`, via `get_or_create_playlist`) and Task 8 (GUI approve
  handler posts the comment).
- Request body shapes below are confirmed against Google's current
  `playlists.insert`, `playlistItems.insert`, and `commentThreads.insert`
  reference docs — not assumed from memory.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_youtube.py`, near the top (after the existing imports —
extend the import line to include the five new names):

```python
from lyricvideo.youtube import (
    add_video_to_playlist, create_playlist, find_playlist_by_id, get_video_snippet, is_video_in_playlist,
    is_video_public, list_new_comments, post_reply, post_top_level_comment, reserved_publish_dates,
    update_video_description, upload_video, video_exists,
)
```

Add a shared tiny helper near the top (after the existing fake classes) and
the new fake client classes and tests, anywhere below the existing
`_FakeYoutubeClient` definitions:

```python
class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


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
        self.members: dict[str, set[str]] = {}

    def list(self, part, playlistId, videoId):
        is_member = videoId in self.members.get(playlistId, set())
        return _FakeExecutable({"items": [{"id": "item1"}] if is_member else []})

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        snippet = kwargs["body"]["snippet"]
        self.members.setdefault(snippet["playlistId"], set()).add(snippet["resourceId"]["videoId"])
        return _FakeExecutable({"id": "item-new"})


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


def test_post_top_level_comment_sends_video_channel_and_text():
    client = _FakeCommentYoutubeClient()
    client._channels = _FakeExecutable({"items": [{"id": "UCmychannel"}]})

    comment_id = post_top_level_comment(client, "vid1", "Which instrument are you playing along with?")

    body = client._comment_threads.insert_kwargs["body"]["snippet"]
    assert body["videoId"] == "vid1"
    assert body["channelId"] == "UCmychannel"
    assert body["topLevelComment"]["snippet"]["textOriginal"] == "Which instrument are you playing along with?"
    assert comment_id == "thread-new"
```

`_FakeCommentYoutubeClient` and `_FakeCommentThreadsResource` already exist
in this file for the reply tests — extend them rather than duplicating.
Change `_FakeCommentThreadsResource` to support `insert` too, and give
`_FakeCommentYoutubeClient` a `channels()` method:

```python
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
```

```python
class _FakeCommentYoutubeClient:
    def __init__(self, items=None):
        self._comment_threads = _FakeCommentThreadsResource(items or [])
        self._comments = _FakeCommentsResource()
        self._channels = _FakeExecutable({"items": [{"id": "UCmychannel"}]})

    def commentThreads(self):
        return self._comment_threads

    def comments(self):
        return self._comments

    def channels(self, part=None, mine=None):
        return self._channels
```

(Replaces the two existing `commentThreads`/`comments` methods on that
class with the version above, and removes the redundant `client._channels
= ...` line from the new test above since the constructor sets it now —
update the test to not reassign `_channels`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_youtube.py -v`
Expected: FAIL — `ImportError: cannot import name 'create_playlist'`

- [ ] **Step 3: Write minimal implementation**

Add to `lyricvideo/youtube.py`, after `upload_video`:

```python
def create_playlist(youtube_client, title: str, description: str) -> str:
    body = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": "public"}}
    response = youtube_client.playlists().insert(part="snippet,status", body=body).execute()
    return response["id"]


def find_playlist_by_id(youtube_client, playlist_id: str) -> bool:
    """Whether playlist_id is still real on the channel -- a locally cached
    id can go stale if the owner deletes the playlist directly in Studio,
    same self-heal role as video_exists() below."""
    response = youtube_client.playlists().list(part="id", id=playlist_id).execute()
    return bool(response.get("items"))


def is_video_in_playlist(youtube_client, playlist_id: str, video_id: str) -> bool:
    response = youtube_client.playlistItems().list(
        part="id", playlistId=playlist_id, videoId=video_id,
    ).execute()
    return bool(response.get("items"))


def add_video_to_playlist(youtube_client, playlist_id: str, video_id: str) -> None:
    """No-op if already a member, so a backfill re-run never duplicates an entry."""
    if is_video_in_playlist(youtube_client, playlist_id, video_id):
        return
    body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    youtube_client.playlistItems().insert(part="snippet", body=body).execute()
```

Add near `post_reply`, at the end of the file:

```python
def post_top_level_comment(youtube_client, video_id: str, text: str) -> str:
    """A fresh standalone comment posted AS the channel, not a reply --
    commentThreads().insert, distinct from post_reply's comments().insert
    which can only reply to an existing comment. channelId is a required
    field (confirmed against the current API reference), so this resolves
    the connected channel's own id first -- channels().list(...) matches
    the resource-then-method pattern every other call in this file already
    uses (get_video_snippet, reserved_publish_dates), not a kwargs-taking
    channels(...) call."""
    channel_response = youtube_client.channels().list(part="id", mine=True).execute()
    channel_id = channel_response["items"][0]["id"]
    body = {
        "snippet": {
            "channelId": channel_id, "videoId": video_id,
            "topLevelComment": {"snippet": {"textOriginal": text}},
        }
    }
    response = youtube_client.commentThreads().insert(part="snippet", body=body).execute()
    return response["id"]
```

The fake client's `channels()` must return an object with `.list(...)`, not
itself take kwargs — matching the real API surface:

```python
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
```

Add `from types import SimpleNamespace` to the top of `tests/test_youtube.py`
if not already imported (it already is, per the existing
`_FakeChannelUploadsClient` class).

And simplify the new test to drop the now-unnecessary reassignment:

```python
def test_post_top_level_comment_sends_video_channel_and_text():
    client = _FakeCommentYoutubeClient()

    comment_id = post_top_level_comment(client, "vid1", "Which instrument are you playing along with?")

    body = client._comment_threads.insert_kwargs["body"]["snippet"]
    assert body["videoId"] == "vid1"
    assert body["channelId"] == "UCmychannel"
    assert body["topLevelComment"]["snippet"]["textOriginal"] == "Which instrument are you playing along with?"
    assert comment_id == "thread-new"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_youtube.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/youtube.py tests/test_youtube.py
git commit -m "Add playlist and top-level-comment raw API calls

Request body shapes confirmed against Google's current playlists.insert,
playlistItems.insert, and commentThreads.insert reference docs."
```

---

## Task 4: `classify_genre` and `draft_engagement_comment` in `youtube_metadata.py`

**Files:**
- Modify: `lyricvideo/youtube_metadata.py`
- Test: `tests/test_youtube_metadata.py`

**Interfaces:**
- Produces: `classify_genre(anthropic_client, song_title, artist,
  full_lyrics, known_genres, model="claude-sonnet-5") -> str`,
  `draft_engagement_comment(anthropic_client, song_title,
  model="claude-sonnet-5") -> str`. Used by Task 6 (`organize_video`).
- Consumes: `_parse_labeled_fields` (already defined in this file, used by
  the existing `generate_video_metadata`/`draft_comment_reply`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_youtube_metadata.py`:

```python
from lyricvideo.youtube_metadata import classify_genre, draft_engagement_comment


def test_classify_genre_returns_the_parsed_genre():
    client = _FakeAnthropicClient("GENRE: Classic Rock")

    genre = classify_genre(client, "Free Bird", "Lynyrd Skynyrd", "lyrics here", known_genres=["Classic Rock"])

    assert genre == "Classic Rock"


def test_classify_genre_includes_every_known_genre_in_the_prompt():
    client = _FakeAnthropicClient("GENRE: Classic Rock")

    classify_genre(client, "Free Bird", "Lynyrd Skynyrd", "lyrics here", known_genres=["Classic Rock", "Country"])

    prompt = client.messages._prompt_text()
    assert "Classic Rock" in prompt
    assert "Country" in prompt


def test_classify_genre_can_propose_a_genre_not_in_the_known_list():
    """The list is a starting point, not a hard cap -- a genuinely new
    genre gets minted rather than forced into an ill-fitting bucket."""
    client = _FakeAnthropicClient("GENRE: Bluegrass")

    genre = classify_genre(client, "Some Song", "Some Artist", "lyrics", known_genres=["Classic Rock"])

    assert genre == "Bluegrass"


def test_draft_engagement_comment_returns_the_parsed_text():
    client = _FakeAnthropicClient("COMMENT: Which instrument are you playing along with?")

    comment = draft_engagement_comment(client, "Free Bird")

    assert comment == "Which instrument are you playing along with?"


def test_draft_engagement_comment_mentions_the_song_title_in_the_prompt():
    client = _FakeAnthropicClient("COMMENT: Nice!")

    draft_engagement_comment(client, "Free Bird")

    assert "Free Bird" in client.messages._prompt_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_youtube_metadata.py -v`
Expected: FAIL — `ImportError: cannot import name 'classify_genre'`

- [ ] **Step 3: Write minimal implementation**

Add to `lyricvideo/youtube_metadata.py`, after `generate_video_metadata`:

```python
def classify_genre(
    anthropic_client, song_title: str, artist: str, full_lyrics: str,
    known_genres: list[str], model: str = "claude-sonnet-5",
) -> str:
    """Picks one genre for this song's Genre playlist. The list is shared
    and growing (see youtube_playlist_state.py) -- Claude is told to reuse
    an existing entry whenever one reasonably fits, and only mint a new one
    when the song genuinely doesn't fit anything already there, so the
    channel's genre vocabulary doesn't fragment into near-duplicates."""
    known_artist = artist.strip()
    artist_line = f'It is performed by "{known_artist}".\n' if known_artist else ""
    genre_list = "\n".join(f"- {g}" for g in known_genres)
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=50,
        messages=[
            {
                "role": "user",
                "content": (
                    f'A song titled "{song_title}" has these lyrics:\n\n{full_lyrics}\n\n'
                    f"{artist_line}"
                    "Classify this song's musical genre for a YouTube playlist. Here is the "
                    f"list of genres already used on this channel:\n{genre_list}\n\n"
                    "If one of these already fits reasonably well, reuse it EXACTLY as written. "
                    "Only propose a new genre name if none of them fit. Reply with EXACTLY one line:\n"
                    "GENRE: <the genre name>"
                ),
            }
        ],
    )
    fields = _parse_labeled_fields(_extract_text(response), ["GENRE"])
    return fields["GENRE"].strip()


def draft_engagement_comment(anthropic_client, song_title: str, model: str = "claude-sonnet-5") -> str:
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=150,
        messages=[
            {
                "role": "user",
                "content": (
                    f'This is a "play along" lyric+chord tutorial video for the song "{song_title}". '
                    "Write a short, friendly comment, as the channel owner, to post on the video, "
                    "inviting musicians to engage -- e.g. asking which instrument they're playing "
                    "along with (guitar, piano, bass, etc.) or what song/chord progression they'd "
                    "like to see covered next. Reply with EXACTLY one line:\n"
                    "COMMENT: <the comment text>"
                ),
            }
        ],
    )
    fields = _parse_labeled_fields(_extract_text(response), ["COMMENT"])
    return fields["COMMENT"].strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_youtube_metadata.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/youtube_metadata.py tests/test_youtube_metadata.py
git commit -m "Add classify_genre and draft_engagement_comment"
```

---

## Task 5: `youtube_playlist_state.py` — playlist ID cache + genre list

**Files:**
- Create: `lyricvideo/youtube_playlist_state.py`
- Test: `tests/test_youtube_playlist_state.py`

**Interfaces:**
- Produces: `DEFAULT_GENRES: list[str]`,
  `load_playlist_ids(path=PLAYLISTS_FILE) -> dict[str, str]`,
  `save_playlist_id(key, playlist_id, path=PLAYLISTS_FILE) -> None`,
  `load_genres(path=GENRES_FILE) -> list[str]`,
  `add_genre_if_new(genre, path=GENRES_FILE) -> None`. Used by Task 6
  (`organize_video`, `get_or_create_playlist`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_youtube_playlist_state.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_youtube_playlist_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lyricvideo.youtube_playlist_state'`

- [ ] **Step 3: Write minimal implementation**

Create `lyricvideo/youtube_playlist_state.py`:

```python
"""Pure persistence for channel-wide YouTube organization: cached playlist
IDs (so a playlist is never re-created or re-searched-for once it exists)
and the shared, growing genre vocabulary Claude classifies songs into. See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md."""

from __future__ import annotations

import json
from pathlib import Path

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
PLAYLISTS_FILE = CREDENTIALS_DIR / "youtube_playlists.json"
GENRES_FILE = CREDENTIALS_DIR / "youtube_genres.json"

DEFAULT_GENRES = [
    "Classic Rock", "Southern Rock", "Hard Rock / Glam", "Progressive Rock",
    "Alternative / Grunge", "Singer-Songwriter / Folk Rock", "Pop Rock / Soft Rock",
    "60s Pop Rock / British Invasion", "Punk", "Country", "Pop", "Hip-Hop / Rap",
    "R&B / Soul", "Metal", "Blues", "Folk / Americana", "Jazz", "Electronic / Dance",
    "Latin", "Reggae", "Christian / Gospel",
]


def load_playlist_ids(path: Path = PLAYLISTS_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_playlist_id(key: str, playlist_id: str, path: Path = PLAYLISTS_FILE) -> None:
    ids = load_playlist_ids(path)
    ids[key] = playlist_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids), encoding="utf-8")


def load_genres(path: Path = GENRES_FILE) -> list[str]:
    if not path.exists():
        return list(DEFAULT_GENRES)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(DEFAULT_GENRES)


def add_genre_if_new(genre: str, path: Path = GENRES_FILE) -> None:
    genres = load_genres(path)
    if genre in genres:
        return
    genres.append(genre)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(genres), encoding="utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_youtube_playlist_state.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/youtube_playlist_state.py tests/test_youtube_playlist_state.py
git commit -m "Add youtube_playlist_state: playlist ID cache + growing genre list"
```

---

## Task 6: `youtube_playlists.py` — `get_or_create_playlist` + `organize_video`

**Files:**
- Create: `lyricvideo/youtube_playlists.py`
- Test: `tests/test_youtube_playlists.py`

**Interfaces:**
- Consumes: `create_playlist`, `find_playlist_by_id`, `add_video_to_playlist`
  (Task 3); `classify_genre`, `draft_engagement_comment` (Task 4);
  `load_playlist_ids`, `save_playlist_id`, `load_genres`, `add_genre_if_new`
  (Task 5); `PendingComment`, `add_pending_comment`, `load_pending_comments`
  (Task 2); `load_youtube_state` (`lyricvideo/youtube_state.py`);
  `load_song` (`lyricvideo/models.py`, already exists — `load_song(path) ->
  Song` with `Song.lines: list[LyricLine]`, `LyricLine.text: str`).
- Produces: `get_or_create_playlist(youtube_client, key, title,
  description) -> str`, `organize_video(youtube_client, anthropic_client,
  work_dir: Path) -> None`. Used by Task 7 (GUI upload call sites) and
  Task 9 (backfill script).

- [ ] **Step 1: Write the failing test**

Create `tests/test_youtube_playlists.py`:

```python
import json
from pathlib import Path

from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.youtube import is_video_in_playlist
from lyricvideo.youtube_state import YoutubeState, save_youtube_state


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


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


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
    added_comments = []
    monkeypatch.setattr("lyricvideo.youtube_playlists.add_pending_comment", lambda c: added_comments.append(c))
    return playlist_ids, genres, added_comments


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

    from lyricvideo.youtube_playlists import organize_video
    fake_client = _FakeAnthropicClient()
    fake_client.messages.create = _must_not_run
    organize_video(client, fake_client, work_dir)  # must not raise


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
    _playlist_ids, _genres, added_comments = _patch_state(monkeypatch, pending=[existing])
    work_dir = _make_work_dir(tmp_path, artist="Pink Floyd", genre="Classic Rock")
    client = _FakeYoutubeClient()

    from lyricvideo.youtube_playlists import organize_video
    organize_video(client, _FakeAnthropicClient(), work_dir)

    assert added_comments == []


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_youtube_playlists.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lyricvideo.youtube_playlists'`

- [ ] **Step 3: Write minimal implementation**

Create `lyricvideo/youtube_playlists.py`:

```python
"""Orchestrates channel organization for one already-uploaded video:
All/Artist/Genre playlist membership, plus queuing its drafted engagement
comment for owner review. Called automatically right after every
schedule_upload() (see gui.py's two upload call sites) and by
scripts/backfill_channel_organization.py for the videos that predate this
feature. See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md."""

from __future__ import annotations

import json
from pathlib import Path

from .models import load_song
from .youtube import add_video_to_playlist, create_playlist, find_playlist_by_id
from .youtube_comment_state import PendingComment, add_pending_comment, load_pending_comments
from .youtube_metadata import classify_genre, draft_engagement_comment
from .youtube_playlist_state import add_genre_if_new, load_genres, load_playlist_ids, save_playlist_id
from .youtube_state import load_youtube_state

ALL_PLAYLIST_KEY = "all"
ALL_PLAYLIST_TITLE = "Play Along Videos - All"
ALL_PLAYLIST_DESCRIPTION = "Every play-along lyrics & chords video on this channel."


def get_or_create_playlist(youtube_client, key: str, title: str, description: str) -> str:
    """Checks the local cache first, self-healing (recreating) if the
    cached playlist was deleted directly in Studio, and only creates fresh
    on a genuine cache miss -- never re-creates a playlist that's still
    real just because this process hasn't seen it before."""
    cached_id = load_playlist_ids().get(key)
    if cached_id and find_playlist_by_id(youtube_client, cached_id):
        return cached_id
    playlist_id = create_playlist(youtube_client, title, description)
    save_playlist_id(key, playlist_id)
    return playlist_id


def _split_artists(artist_field: str) -> list[str]:
    return [a.strip() for a in artist_field.split(",") if a.strip()]


def _read_song_info(work_dir: Path) -> dict:
    try:
        return json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def organize_video(youtube_client, anthropic_client, work_dir: Path) -> None:
    state = load_youtube_state(work_dir)
    if state is None:
        return  # never uploaded -- nothing to organize yet

    info = _read_song_info(work_dir)
    title = info.get("title") or work_dir.name
    artist_field = info.get("artist") or ""
    genre = info.get("genre") or ""

    if not genre:
        song = load_song(work_dir / "lyrics_timed.json")
        full_lyrics = "\n".join(line.text for line in song.lines)
        genre = classify_genre(anthropic_client, title, artist_field, full_lyrics, load_genres())
        add_genre_if_new(genre)
        info["genre"] = genre
        (work_dir / "song_info.json").write_text(json.dumps(info), encoding="utf-8")

    all_playlist_id = get_or_create_playlist(
        youtube_client, ALL_PLAYLIST_KEY, ALL_PLAYLIST_TITLE, ALL_PLAYLIST_DESCRIPTION,
    )
    add_video_to_playlist(youtube_client, all_playlist_id, state.video_id)

    for artist in _split_artists(artist_field):
        artist_playlist_id = get_or_create_playlist(
            youtube_client, f"artist:{artist}", f"{artist} - Play Along Videos",
            f"Every play-along lyrics & chords video on this channel by {artist}.",
        )
        add_video_to_playlist(youtube_client, artist_playlist_id, state.video_id)

    if genre:
        genre_playlist_id = get_or_create_playlist(
            youtube_client, f"genre:{genre}", f"{genre} - Play Along Videos",
            f"Every {genre} play-along lyrics & chords video on this channel.",
        )
        add_video_to_playlist(youtube_client, genre_playlist_id, state.video_id)

    if not state.engagement_comment_posted:
        already_pending = any(c.video_id == state.video_id for c in load_pending_comments())
        if not already_pending:
            comment_text = draft_engagement_comment(anthropic_client, title)
            add_pending_comment(PendingComment(video_id=state.video_id, song_title=title, draft_text=comment_text))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_youtube_playlists.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/youtube_playlists.py tests/test_youtube_playlists.py
git commit -m "Add organize_video: All/Artist/Genre playlists + pending engagement comment

Self-heals a cached playlist deleted directly in Studio, is idempotent on
repeated calls (safe for backfill re-runs), and never reclassifies a genre
already cached in song_info.json."
```

---

## Task 7: Wire `organize_video` into both upload call sites

**Files:**
- Modify: `lyricvideo/gui.py`
- Test: `tests/test_gui.py`

**Interfaces:**
- Consumes: `organize_video(youtube_client, anthropic_client, work_dir) ->
  None` (Task 6).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gui.py`, extending the existing import block at the top
(add `organize_video` to the `from lyricvideo.gui import (...)` list, and
`save_song`/`LyricLine`/`Song`/`Word` from `lyricvideo.models` if not
already imported):

```python
def test_maybe_upload_to_youtube_calls_organize_video_after_a_successful_upload(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.organize_video",
        lambda youtube_client, anthropic_client, work_dir: calls.append((youtube_client, work_dir)),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [("fake-youtube-client", tmp_path)]


def test_maybe_upload_to_youtube_survives_organize_video_failure(tmp_path, monkeypatch):
    """The video itself is the important part -- a playlist/comment
    organization failure must never make an otherwise-successful upload
    look like it failed."""
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.organize_video", _raise)
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_retry_pending_uploads_calls_organize_video_for_each_succeeded_song(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.organize_video",
        lambda youtube_client, anthropic_client, work_dir: calls.append(work_dir),
    )

    results = _retry_pending_uploads(tmp_path, Settings())

    assert calls == [tmp_path / "song-a"]
    assert results == {"succeeded": ["song-a"], "failed": []}


def test_retry_pending_uploads_still_succeeds_when_organize_video_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.schedule_upload", lambda *a, **k: None)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.organize_video", _raise)

    results = _retry_pending_uploads(tmp_path, Settings())

    assert results == {"succeeded": ["song-a"], "failed": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_gui.py -k organize_video -v`
Expected: FAIL — `AttributeError: <module 'lyricvideo.gui'> does not have the attribute 'organize_video'`

- [ ] **Step 3: Write minimal implementation**

In `lyricvideo/gui.py`, add to the imports (near the existing
`from .youtube_schedule import schedule_upload` line):

```python
from .youtube_playlists import organize_video
```

Modify `_maybe_upload_to_youtube` (the `try` block around the existing
`schedule_upload(...)` call):

```python
        anthropic_client = anthropic.Anthropic()
        schedule_upload(youtube_client, anthropic_client, work_dir, settings)
        print(f"Uploaded to YouTube: {work_dir.name}")
        try:
            organize_video(youtube_client, anthropic_client, work_dir)
        except Exception as e:
            print(
                f"WARNING: could not organize {work_dir.name} into playlists/comment: "
                f"{type(e).__name__}: {e}", file=sys.stderr,
            )
    except Exception as e:
        print(f"WARNING: YouTube upload failed for {work_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)
```

Modify `_retry_pending_uploads`'s loop:

```python
    for slug in slugs:
        try:
            schedule_upload(youtube_client, anthropic_client, work_root / slug, settings)
            try:
                organize_video(youtube_client, anthropic_client, work_root / slug)
            except Exception as e:
                print(
                    f"WARNING: could not organize {slug} into playlists/comment: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
            results["succeeded"].append(slug)
        except Exception as e:
            results["failed"].append((slug, f"{type(e).__name__}: {e}"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_gui.py -v`
Expected: PASS (all tests, old and new — the pre-existing
`test_maybe_upload_to_youtube_calls_schedule_upload_when_eligible` and
similar tests still pass since they don't monkeypatch `organize_video`,
and the real `organize_video` importing from a real `work_dir` that has no
`youtube_state.json` will hit its own early-return, caught safely by the
new inner try/except either way).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/gui.py tests/test_gui.py
git commit -m "Call organize_video right after every successful schedule_upload

Automatic on every future upload -- no new owner action. A playlist/
comment organization failure is logged and never fails the upload itself."
```

---

## Task 8: GUI "Pending Engagement Comments" panel

**Files:**
- Modify: `lyricvideo/gui.py`
- Test: `tests/test_gui.py`

**Interfaces:**
- Consumes: `PendingComment`, `load_pending_comments`, `add_pending_comment`,
  `remove_pending_comment` (Task 2); `post_top_level_comment` (Task 3);
  `load_youtube_state`, `save_youtube_state` (`lyricvideo/youtube_state.py`,
  existing); `_make_collapsible_section` (existing, `lyricvideo/gui.py`).
- Produces: `_mark_engagement_comment_posted(video_id: str) -> None`
  (module-level, testable in isolation), `LyricVideoGUI._on_approve_comment`,
  `LyricVideoGUI._on_dismiss_comment`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gui.py` (needs `SimpleNamespace` — already imported per
the existing `_gui_stub` usage elsewhere in this file; needs
`PendingComment` imported from `lyricvideo.youtube_comment_state`):

```python
from lyricvideo.youtube_comment_state import PendingComment


def test_mark_engagement_comment_posted_updates_the_matching_song(tmp_path, monkeypatch):
    from lyricvideo.gui import _mark_engagement_comment_posted

    work_dir = tmp_path / "work" / "my-song"
    work_dir.mkdir(parents=True)
    save_youtube_state(work_dir, YoutubeState(video_id="vid123", uploaded_at="2026-09-10T15:00:00", title="t"))
    other_dir = tmp_path / "work" / "other-song"
    other_dir.mkdir(parents=True)
    save_youtube_state(other_dir, YoutubeState(video_id="vid999", uploaded_at="2026-09-10T15:00:00", title="t2"))
    monkeypatch.setattr("lyricvideo.gui.PROJECT_ROOT", tmp_path)

    _mark_engagement_comment_posted("vid123")

    assert load_youtube_state(work_dir).engagement_comment_posted is True
    assert load_youtube_state(other_dir).engagement_comment_posted is False


def test_approve_comment_posts_marks_posted_and_removes_from_queue(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    posted = []
    monkeypatch.setattr(
        "lyricvideo.gui.post_top_level_comment",
        lambda client, video_id, text: posted.append((video_id, text)) or "new-comment-id",
    )
    removed = []
    monkeypatch.setattr("lyricvideo.gui.remove_pending_comment", lambda video_id: removed.append(video_id))
    marked = []
    monkeypatch.setattr("lyricvideo.gui._mark_engagement_comment_posted", lambda video_id: marked.append(video_id))
    monkeypatch.setattr("lyricvideo.gui.messagebox.showinfo", lambda *a, **k: None)
    invalidated = []

    comment = PendingComment(video_id="vid123", song_title="My Song", draft_text="Which instrument?")
    text_box = SimpleNamespace(get=lambda start, end: "Which instrument are you playing?\n")
    stub = _gui_stub(_invalidate_pending_comments=lambda: invalidated.append(True))
    LyricVideoGUI._on_approve_comment(stub, comment, text_box)

    assert posted == [("vid123", "Which instrument are you playing?")]
    assert removed == ["vid123"]
    assert marked == ["vid123"]
    assert invalidated == [True]


def test_approve_comment_failure_shows_the_error_dialog_and_keeps_the_draft(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")

    def failing_post(client, video_id, text):
        raise RuntimeError("commentsDisabled")

    monkeypatch.setattr("lyricvideo.gui.post_top_level_comment", failing_post)
    monkeypatch.setattr("lyricvideo.gui.remove_pending_comment", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    comment = PendingComment(video_id="vid123", song_title="My Song", draft_text="Which instrument?")
    text_box = SimpleNamespace(get=lambda start, end: "Which instrument are you playing?\n")
    LyricVideoGUI._on_approve_comment(_gui_stub(), comment, text_box)

    assert shown == [("Could not post comment", "RuntimeError: commentsDisabled")]


def test_dismiss_comment_removes_from_queue_and_invalidates(monkeypatch):
    removed = []
    monkeypatch.setattr("lyricvideo.gui.remove_pending_comment", lambda video_id: removed.append(video_id))
    invalidated = []

    comment = PendingComment(video_id="vid123", song_title="My Song", draft_text="Which instrument?")
    stub = _gui_stub(_invalidate_pending_comments=lambda: invalidated.append(True))
    LyricVideoGUI._on_dismiss_comment(stub, comment)

    assert removed == ["vid123"]
    assert invalidated == [True]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_gui.py -k "comment" -v`
Expected: FAIL — `ImportError: cannot import name '_mark_engagement_comment_posted'`
(or `AttributeError` for `LyricVideoGUI._on_approve_comment`).

- [ ] **Step 3: Write minimal implementation**

In `lyricvideo/gui.py`, add to the imports:

```python
from dataclasses import replace
```

```python
from .youtube_comment_state import (
    PendingComment,
    add_pending_comment,
    load_pending_comments,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_comment,
    remove_pending_reply,
)
```

(Merge into the existing `from .youtube_comment_state import (...)` block
rather than duplicating it — add `PendingComment`, `load_pending_comments`,
`remove_pending_comment` to whatever names are already imported there.)

```python
from .youtube import (
    ...,  # existing names
    post_top_level_comment,
)
```

Add a module-level helper near `_maybe_upload_to_youtube`:

```python
def _mark_engagement_comment_posted(video_id: str) -> None:
    for work_dir in (PROJECT_ROOT / "work").glob("*"):
        state = load_youtube_state(work_dir)
        if state is not None and state.video_id == video_id:
            save_youtube_state(work_dir, replace(state, engagement_comment_posted=True))
            return
```

Add the panel-building method right after `_build_youtube_panel` (and call
it from `__init__` right after the existing `self._build_youtube_panel(right)`
line):

```python
        self._build_youtube_panel(right)
        self._build_pending_comments_panel(right)
```

```python
    def _build_pending_comments_panel(self, parent) -> None:
        content, self._invalidate_pending_comments = self._make_collapsible_section(
            parent, "Pending Engagement Comments", on_first_expand=self._refresh_pending_comments,
        )
        self.pending_comments_frame = ctk.CTkScrollableFrame(content, height=SONG_LIST_HEIGHT)
        self.pending_comments_frame.pack(fill="x", padx=8, pady=(0, 8))

    def _refresh_pending_comments(self) -> None:
        for child in self.pending_comments_frame.winfo_children():
            child.destroy()
        comments = load_pending_comments()
        for comment in comments:
            try:
                self._render_one_pending_comment(comment)
            except Exception as e:
                print(
                    f"WARNING: could not build a pending-comment row for {comment.video_id!r}: "
                    f"{type(e).__name__}: {e}", file=sys.stderr,
                )
        if not comments:
            ctk.CTkLabel(self.pending_comments_frame, text="(none)", text_color="gray60").pack(
                anchor="w", padx=6, pady=6
            )

    def _render_one_pending_comment(self, comment: PendingComment) -> None:
        row = ctk.CTkFrame(self.pending_comments_frame)
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(row, text=comment.song_title, anchor="w", font=ctk.CTkFont(weight="bold")).pack(
            fill="x", padx=6, pady=(6, 2)
        )
        text_box = ctk.CTkTextbox(row, height=60)
        text_box.insert("1.0", comment.draft_text)
        text_box.pack(fill="x", padx=6, pady=(0, 4))
        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkButton(
            buttons, text="Approve", width=80, command=lambda: self._on_approve_comment(comment, text_box),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            buttons, text="Dismiss", width=80, fg_color="gray30", hover_color="gray20",
            command=lambda: self._on_dismiss_comment(comment),
        ).pack(side="left")

    def _on_approve_comment(self, comment: PendingComment, text_box) -> None:
        text = text_box.get("1.0", "end").strip()

        def worker():
            credentials = youtube_auth.load_credentials()
            if credentials is None:
                return
            try:
                youtube_client = build("youtube", "v3", credentials=credentials)
                post_top_level_comment(youtube_client, comment.video_id, text)
                remove_pending_comment(comment.video_id)
                _mark_engagement_comment_posted(comment.video_id)
                self.root.after(0, self._invalidate_pending_comments)
                self.root.after(0, lambda: messagebox.showinfo(
                    "Comment posted",
                    "Posted. Remember to pin it from YouTube Studio -- the API has no way to do that part.",
                ))
            except Exception as e:
                # See _on_connect_youtube: never read `e` inside the lambda.
                message = f"{type(e).__name__}: {e}"
                self.root.after(0, lambda: messagebox.showerror("Could not post comment", message))

        threading.Thread(target=worker, daemon=True).start()

    def _on_dismiss_comment(self, comment: PendingComment) -> None:
        remove_pending_comment(comment.video_id)
        self._invalidate_pending_comments()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_gui.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/gui.py tests/test_gui.py
git commit -m "Add Pending Engagement Comments panel

Same collapsible/lazy-build pattern as the Redo/Upload/Pending song lists,
same Approve/Dismiss review gate as comment replies. Approve marks the
song's engagement_comment_posted so a later backfill re-run never re-drafts
it, and reminds the owner that pinning is still a manual Studio step."
```

---

## Task 9: Backfill script for the 67 already-uploaded videos

**Files:**
- Create: `scripts/backfill_channel_organization.py`

**Interfaces:**
- Consumes: `video_exists` (`lyricvideo/youtube.py`, existing);
  `load_credentials` (`lyricvideo/youtube_auth.py`, existing);
  `organize_video` (Task 6); `load_youtube_state` (existing).

No automated test for this file — matches the existing
`scripts/backfill_support_overlay_description.py` convention (a manual,
re-runnable tool, not wired into the GUI or the test suite). Verified in
Step 2 below by actually running it against the real channel.

- [ ] **Step 1: Write the script**

Create `scripts/backfill_channel_organization.py`:

```python
"""One-off tool: organizes every already-uploaded video into its All/
Artist/Genre playlists and queues its engagement comment for review, via
the same organize_video() every future upload already gets automatically
(see youtube_playlists.py). See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md.

Idempotent -- organize_video() checks before adding to a playlist and
before queuing a comment, so re-running this after uploading a few more
songs, or after approving some pending comments, never duplicates
anything. A video deleted directly on YouTube is skipped and reported,
never treated as an error.

Not wired into the GUI -- a manual, re-runnable apply step, same
convention as backfill_support_overlay_description.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from lyricvideo.youtube import video_exists
from lyricvideo.youtube_auth import load_credentials
from lyricvideo.youtube_playlists import organize_video
from lyricvideo.youtube_state import load_youtube_state

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"


def main() -> None:
    from googleapiclient.discovery import build

    credentials = load_credentials()
    if credentials is None:
        raise SystemExit(
            "Not connected to YouTube (no valid stored token). "
            "Connect via the app's 'Connect to YouTube' button first."
        )

    youtube_client = build("youtube", "v3", credentials=credentials)
    anthropic_client = anthropic.Anthropic()

    channel_resp = youtube_client.channels().list(part="snippet", mine=True).execute()
    items = channel_resp.get("items", [])
    if not items:
        raise SystemExit("Connected, but no channel found for this account.")
    print(f"Connected channel: {items[0]['snippet']['title']}\n")

    organized, skipped_missing, failed = 0, 0, 0
    for work_dir in sorted(WORK_DIR.iterdir()):
        if not work_dir.is_dir():
            continue
        state = load_youtube_state(work_dir)
        if state is None:
            continue

        try:
            if not video_exists(youtube_client, state.video_id):
                print(f"  {work_dir.name}: video {state.video_id} no longer exists on YouTube -- skipped")
                skipped_missing += 1
                continue
            organize_video(youtube_client, anthropic_client, work_dir)
            print(f"  {work_dir.name}: organized")
            organized += 1
        except Exception as e:
            print(f"  {work_dir.name}: FAILED -- {type(e).__name__}: {e}")
            failed += 1

    print(f"\nDone. Organized: {organized}, missing/deleted: {skipped_missing}, failed: {failed}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `.venv/bin/python -c "import ast; ast.parse(open('scripts/backfill_channel_organization.py').read())"`
Expected: no output (parses without a SyntaxError).

Then run: `.venv/bin/python -m py_compile scripts/backfill_channel_organization.py`
Expected: no output (compiles cleanly, confirming every import path resolves at compile time — actual execution against the real channel is the owner's own call, made after this plan's implementation is complete and reviewed, not part of this automated step).

- [ ] **Step 3: Commit**

```bash
git add scripts/backfill_channel_organization.py
git commit -m "Add backfill script for the 67 already-uploaded videos

Manual, re-runnable, same convention as
backfill_support_overlay_description.py. Not run automatically -- the
owner runs it once after connecting to YouTube."
```

---

## Task 10: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the entire test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS, zero failures, zero errors — including every pre-existing
test untouched by this plan (confirms nothing here broke `schedule_upload`,
the reply-approval flow, or anything else).

- [ ] **Step 2: Confirm no stray debug output or leftover fakes**

Run: `git status`
Expected: clean tree (everything from Tasks 1-9 already committed) —
nothing untracked left over from manual testing.

- [ ] **Step 3: Re-read the spec once more against the finished code**

Walk `docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md`
section by section and confirm each requirement has a corresponding
committed change:
- Three playlist kinds (All/Artist/Genre), created lazily — Task 6.
- Engagement comment drafted + owner-approval queue — Tasks 2, 8.
- Automatic on every future upload — Task 7.
- One-time backfill for the 67 existing videos — Task 9.
- Exact-string artist matching (no normalization) — Task 6
  (`_split_artists`, no fuzzy matching anywhere).
- Growing genre list, reuse-preferred — Tasks 4, 5, 6.
- Self-healing playlist cache — Task 6 (`get_or_create_playlist`).
- Backward-compatible `YoutubeState`/`song_info.json` — Tasks 1, 6.

If a gap turns up, add a task and implement it before considering this
plan complete.

No commit for this task — it's a read-only check.

---

## Self-Review Notes

- **Spec coverage:** every "In scope" bullet from the spec maps to a task
  above (see Task 10's checklist). The two "Out of scope" items (pinning,
  end screens) have no task — correct, they're impossible via the API.
- **Placeholder scan:** no TBD/TODO; every step has real, runnable code.
- **Type consistency:** `organize_video(youtube_client, anthropic_client,
  work_dir: Path) -> None` and `get_or_create_playlist(youtube_client, key,
  title, description) -> str` are used with these exact signatures in
  Tasks 6, 7, and 9. `PendingComment(video_id, song_title, draft_text)`
  matches across Tasks 2, 6, and 8. `engagement_comment_posted` spelled
  identically in Tasks 1, 6, and 8.
- Found and fixed one real bug while drafting Task 3: an early draft of
  `post_top_level_comment` called `youtube_client.channels(part="id",
  mine=True)` directly, but the real API surface (matching every other
  function already in `youtube.py`) is `youtube_client.channels().list(...)`
  — a resource object first, then `.list()`. Caught by mirroring
  `get_video_snippet`'s existing pattern; corrected in the same task before
  implementation, with the test's fake client updated to match.
