# YouTube Upload + Channel Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let PlayAlongVideoProduction optionally upload finished videos straight to the owner's YouTube channel, with Claude-written metadata, owner-approved comment replies, and upload spacing/scheduling handled by YouTube itself.

**Architecture:** Six new small, single-responsibility modules (`youtube_state.py`, `youtube.py`, `youtube_metadata.py`, `youtube_schedule.py`, `youtube_auth.py`, `youtube_comment_state.py`) hold all real logic, each testable with a fake API client injected the same way `imagery.py` already fakes `anthropic_client`/`http_client`. `settings.py`/`settings_panel.py` gain YouTube fields. `gui.py` wires triggers into the existing Generate/Redo/Batch worker threads plus a new comment-review panel — all GUI wiring stays manually/visually verified, matching this project's existing testing-constraint precedent.

**Tech Stack:** `google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib` (new), `anthropic` (existing), CustomTkinter (existing).

**Spec:** `docs/superpowers/specs/2026-09-10-youtube-upload-design.md`

## Global Constraints

- Nothing ever posts to YouTube (upload OR comment reply) without either the
  owner's explicit approve click (comment replies) or an upload trigger the
  owner explicitly enabled (the `youtube_auto_upload` checkbox, or the
  manual "Upload to YouTube" button).
- A Public-bound video uploads immediately as YouTube-Private with a
  computed future `publishAt`; YouTube's own servers do the actual
  publishing. Unlisted/Private uploads have no such scheduling and go out
  with that literal status immediately.
- `youtube_min_days_between_uploads` defaults to `2`, `youtube_preferred_upload_hour` defaults to `15` (3 PM local time).
- `youtube_category_id` defaults to `"26"` (Howto & Style). `youtube_made_for_kids` defaults to `False` (a required COPPA declaration on every upload, not just a preference).
- Comment monitoring is scoped ONLY to videos this app uploaded (tracked via each song's own `youtube_state.json`), and only runs while the GUI process is open — no always-on/background-service mode.
- No automated "corrected video" re-upload/relinking mechanism of any kind — a correction is always the owner's own manual call (using the same manual "Upload to YouTube" button as any other upload).
- Any upload or comment-check failure is caught and logged as a warning — it must never make an otherwise-successful video generation look like it failed, and never crashes the GUI.
- All new persisted files live under `Path.home() / ".playalongvideoproduction"` (same convention as `settings.py`'s `CONFIG_DIR`), except per-song `youtube_state.json` which lives in that song's own work dir alongside `lyrics_timed.json`.

---

## Task 1: Per-song upload tracking (`youtube_state.py`)

**Files:**
- Create: `lyricvideo/youtube_state.py`
- Test: `tests/test_youtube_state.py`

**Interfaces:**
- Consumes: nothing new (stdlib only).
- Produces: `YoutubeState` frozen dataclass (`video_id: str`, `uploaded_at: str`, `title: str`); `load_youtube_state(work_dir: Path) -> YoutubeState | None`; `save_youtube_state(work_dir: Path, state: YoutubeState) -> None`. Used by Task 4 (`youtube_schedule.py`) and Task 8 (`gui.py`'s `_maybe_upload_to_youtube`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_youtube_state.py
from pathlib import Path

from lyricvideo.youtube_state import YoutubeState, load_youtube_state, save_youtube_state


def test_load_youtube_state_returns_none_when_no_file(tmp_path):
    assert load_youtube_state(tmp_path) is None


def test_save_then_load_round_trips(tmp_path):
    state = YoutubeState(video_id="abc123", uploaded_at="2026-09-10T15:00:00", title="My Song")

    save_youtube_state(tmp_path, state)

    assert load_youtube_state(tmp_path) == state


def test_load_youtube_state_returns_none_on_corrupt_file(tmp_path):
    (tmp_path / "youtube_state.json").write_text("not json", encoding="utf-8")

    assert load_youtube_state(tmp_path) is None


def test_load_youtube_state_returns_none_on_missing_fields(tmp_path):
    (tmp_path / "youtube_state.json").write_text('{"video_id": "abc"}', encoding="utf-8")

    assert load_youtube_state(tmp_path) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.youtube_state'`

- [ ] **Step 3: Write the implementation**

```python
# lyricvideo/youtube_state.py
"""Per-song YouTube upload tracking -- lets the app know which of its own
uploads exist, so Redo can skip auto-re-uploading a song it already posted.
See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("playalongvideoproduction")

STATE_FILENAME = "youtube_state.json"


@dataclass(frozen=True)
class YoutubeState:
    video_id: str
    uploaded_at: str
    title: str


def load_youtube_state(work_dir: Path) -> YoutubeState | None:
    path = work_dir / STATE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return YoutubeState(**data)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Could not read YouTube state at %s: %s", path, exc)
        return None


def save_youtube_state(work_dir: Path, state: YoutubeState) -> None:
    path = work_dir / STATE_FILENAME
    path.write_text(json.dumps(asdict(state)), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_state.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Update CLAUDE.md and commit**

Add a short note to `CLAUDE.md`'s feature list describing `youtube_state.json` (see Task 9 for the fuller integration note — a one-line mention here is enough, since the pre-commit hook requires `CLAUDE.md` staged alongside code).

```bash
git add lyricvideo/youtube_state.py tests/test_youtube_state.py CLAUDE.md
git commit -m "Add per-song YouTube upload state tracking"
```

---

## Task 2: Raw YouTube API calls (`youtube.py`)

**Files:**
- Modify: `requirements.txt`
- Create: `lyricvideo/youtube.py`
- Test: `tests/test_youtube.py`

**Interfaces:**
- Consumes: `google-api-python-client` (new dependency).
- Produces: `Comment` frozen dataclass (`comment_id`, `video_id`, `author`, `text`, `published_at`, all `str`); `upload_video(youtube_client, video_path: Path, title: str, description: str, tags: list[str], privacy: str, publish_at: datetime | None, category_id: str, made_for_kids: bool) -> str`; `list_new_comments(youtube_client, video_id: str, seen_comment_ids: set[str]) -> list[Comment]`; `post_reply(youtube_client, comment_id: str, text: str) -> None`. Used by Task 4 (`youtube_schedule.py`) and Task 11 (`gui.py`'s comment panel).

- [ ] **Step 1: Add the new dependencies**

Add to `requirements.txt`, right after the existing `anthropic>=0.34` line:

```
google-api-python-client>=2.100
google-auth-httplib2>=0.2
google-auth-oauthlib>=1.2
```

- [ ] **Step 2: Install the new dependencies**

Run: `.venv/bin/python3 -m pip install -r requirements.txt`
Expected: the three new packages install successfully (needed before Step 4's tests can import `googleapiclient`).

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_youtube.py
from datetime import datetime, timezone
from pathlib import Path

from lyricvideo.youtube import list_new_comments, post_reply, upload_video


class _FakeUploadRequest:
    def __init__(self, video_id: str):
        self._video_id = video_id

    def next_chunk(self):
        return None, {"id": self._video_id}


class _FakeVideosResource:
    def __init__(self, video_id: str):
        self._video_id = video_id
        self.insert_kwargs = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeUploadRequest(self._video_id)


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
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_youtube.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.youtube'`

- [ ] **Step 5: Write the implementation**

```python
# lyricvideo/youtube.py
"""Raw YouTube Data API v3 calls -- upload, comment listing, replying. Every
function takes an already-built API client as its first argument (same
"inject the client, fake it in tests" pattern imagery.py already uses for
anthropic_client/http_client), so nothing here ever makes a real network
call in tests. See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Comment:
    comment_id: str
    video_id: str
    author: str
    text: str
    published_at: str


def _publish_at_string(when: datetime) -> str:
    """YouTube's publishAt must be UTC ISO 8601. `when` may be naive (assumed
    already local) or timezone-aware; either way this converts to a real UTC
    instant before formatting, so the scheduled time is correct regardless
    of the machine's configured timezone."""
    aware = when if when.tzinfo is not None else when.astimezone()
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0Z")


def upload_video(
    youtube_client,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
    publish_at: datetime | None,
    category_id: str,
    made_for_kids: bool,
) -> str:
    from googleapiclient.http import MediaFileUpload

    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids}
    if publish_at is not None:
        status["publishAt"] = _publish_at_string(publish_at)
    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": category_id},
        "status": status,
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube_client.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return response["id"]


def list_new_comments(youtube_client, video_id: str, seen_comment_ids: set[str]) -> list[Comment]:
    response = youtube_client.commentThreads().list(
        part="snippet", videoId=video_id, textFormat="plainText", maxResults=100,
    ).execute()
    comments: list[Comment] = []
    for item in response.get("items", []):
        top_level = item["snippet"]["topLevelComment"]
        comment_id = top_level["id"]
        if comment_id in seen_comment_ids:
            continue
        snippet = top_level["snippet"]
        comments.append(Comment(
            comment_id=comment_id,
            video_id=video_id,
            author=snippet.get("authorDisplayName", ""),
            text=snippet.get("textDisplay", ""),
            published_at=snippet.get("publishedAt", ""),
        ))
    return comments


def post_reply(youtube_client, comment_id: str, text: str) -> None:
    youtube_client.comments().insert(
        part="snippet", body={"snippet": {"parentId": comment_id, "textOriginal": text}},
    ).execute()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_youtube.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Update CLAUDE.md and commit**

```bash
git add requirements.txt lyricvideo/youtube.py tests/test_youtube.py CLAUDE.md
git commit -m "Add raw YouTube Data API v3 call wrappers"
```

---

## Task 3: Claude-authored metadata (`youtube_metadata.py`)

**Files:**
- Create: `lyricvideo/youtube_metadata.py`
- Test: `tests/test_youtube_metadata.py`

**Interfaces:**
- Consumes: an injected Anthropic-client-shaped object (`.messages.create(...)`, same as `imagery.py`).
- Produces: `generate_video_metadata(anthropic_client, song_title: str, full_lyrics: str, model: str = "claude-sonnet-5") -> tuple[str, str, list[str]]` (title, description, tags); `draft_comment_reply(anthropic_client, comment_text: str, song_title: str, model: str = "claude-sonnet-5") -> tuple[str, bool]` (reply, is_error_report). Used by Task 4 (`youtube_schedule.py`) and Task 11 (`gui.py`'s comment panel).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_youtube_metadata.py
from lyricvideo.youtube_metadata import draft_comment_reply, generate_video_metadata


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_generate_video_metadata_parses_all_three_labeled_fields():
    client = _FakeAnthropicClient(
        "TITLE: Wish You Were Here - Play Along\n"
        "DESCRIPTION: A wistful song about absence and longing.\n"
        "TAGS: pink floyd, play along, guitar chords, lyrics video"
    )

    title, description, tags = generate_video_metadata(client, "Wish You Were Here", "lyrics here")

    assert title == "Wish You Were Here - Play Along"
    assert "A wistful song about absence and longing." in description
    assert "Spot an error in this video? Let me know in the comments!" in description
    assert tags == ["pink floyd", "play along", "guitar chords", "lyrics video"]


def test_generate_video_metadata_falls_back_to_song_title_if_title_missing():
    client = _FakeAnthropicClient("DESCRIPTION: Some description\nTAGS: tag1")

    title, _description, _tags = generate_video_metadata(client, "Original Title", "lyrics")

    assert title == "Original Title"


def test_generate_video_metadata_tolerates_reordered_labels():
    client = _FakeAnthropicClient("TAGS: a, b\nTITLE: My Title\nDESCRIPTION: My description")

    title, description, tags = generate_video_metadata(client, "fallback", "lyrics")

    assert title == "My Title"
    assert "My description" in description
    assert tags == ["a", "b"]


def test_draft_comment_reply_detects_error_report():
    client = _FakeAnthropicClient(
        "IS_ERROR_REPORT: YES\nREPLY: Thanks for catching that, I'll take a look!"
    )

    reply, is_error_report = draft_comment_reply(client, "the chord at 1:30 looks wrong", "My Song")

    assert is_error_report is True
    assert reply == "Thanks for catching that, I'll take a look!"


def test_draft_comment_reply_detects_non_error_comment():
    client = _FakeAnthropicClient("IS_ERROR_REPORT: NO\nREPLY: Glad you liked it!")

    reply, is_error_report = draft_comment_reply(client, "great video!", "My Song")

    assert is_error_report is False
    assert reply == "Glad you liked it!"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_metadata.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.youtube_metadata'`

- [ ] **Step 3: Write the implementation**

```python
# lyricvideo/youtube_metadata.py
"""Claude-authored YouTube upload metadata and comment-reply drafts. See
docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

_ERROR_INVITE = "Spot an error in this video? Let me know in the comments!"


class MetadataGenError(Exception):
    pass


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise MetadataGenError("Claude response contained no text content")
    return "".join(parts)


def _parse_labeled_fields(text: str, labels: list[str]) -> dict[str, str]:
    fields = {label: "" for label in labels}
    for line in text.splitlines():
        stripped = line.strip()
        for label in labels:
            prefix = f"{label}:"
            if stripped.upper().startswith(prefix):
                fields[label] = stripped[len(prefix):].strip()
    return fields


def generate_video_metadata(
    anthropic_client, song_title: str, full_lyrics: str, model: str = "claude-sonnet-5",
) -> tuple[str, str, list[str]]:
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f'A song titled "{song_title}" has these lyrics:\n\n{full_lyrics}\n\n'
                    "Write YouTube upload metadata for a 'play along' lyric+chord video of "
                    "this song. Reply with EXACTLY three lines, each prefixed with its label "
                    "and nothing else before or after:\n"
                    "TITLE: <a natural YouTube title>\n"
                    "DESCRIPTION: <a 2-4 sentence description of the song>\n"
                    "TAGS: <5-8 relevant search tags, comma-separated>"
                ),
            }
        ],
    )
    fields = _parse_labeled_fields(_extract_text(response), ["TITLE", "DESCRIPTION", "TAGS"])
    title = fields["TITLE"] or song_title
    description = f"{fields['DESCRIPTION']}\n\n{_ERROR_INVITE}".strip()
    tags = [t.strip() for t in fields["TAGS"].split(",") if t.strip()]
    return title, description, tags


def draft_comment_reply(
    anthropic_client, comment_text: str, song_title: str, model: str = "claude-sonnet-5",
) -> tuple[str, bool]:
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f'Someone left this comment on a "{song_title}" play-along video:\n\n'
                    f'"{comment_text}"\n\n'
                    "Reply with EXACTLY two lines, each prefixed with its label:\n"
                    "IS_ERROR_REPORT: <YES or NO -- is this reporting a mistake in the video, "
                    "like wrong chords, sync issues, or wrong lyrics?>\n"
                    "REPLY: <a short, friendly, genuine-sounding reply, written as the channel owner>"
                ),
            }
        ],
    )
    fields = _parse_labeled_fields(_extract_text(response), ["IS_ERROR_REPORT", "REPLY"])
    is_error_report = fields["IS_ERROR_REPORT"].strip().upper().startswith("YES")
    return fields["REPLY"], is_error_report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_metadata.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Update CLAUDE.md and commit**

```bash
git add lyricvideo/youtube_metadata.py tests/test_youtube_metadata.py CLAUDE.md
git commit -m "Add Claude-authored YouTube metadata and comment-reply drafting"
```

---

## Task 4: Upload scheduling (`youtube_schedule.py`)

**Files:**
- Create: `lyricvideo/youtube_schedule.py`
- Test: `tests/test_youtube_schedule.py`

**Interfaces:**
- Consumes: `load_song` (`lyricvideo/models.py`, existing), `slugify` (`lyricvideo/pipeline.py`, existing), `upload_video` (Task 2), `generate_video_metadata` (Task 3), `YoutubeState`/`save_youtube_state` (Task 1). Also duck-types a `settings` object with `.youtube_privacy`, `.youtube_category_id`, `.youtube_made_for_kids`, `.youtube_min_days_between_uploads`, `.youtube_preferred_upload_hour` (matches `Settings`, added in Task 6 — not imported directly here to avoid any import-order coupling).
- Produces: `compute_next_publish_slot(now: datetime, reserved_slot: datetime | None, min_days_between: int, preferred_hour: int) -> datetime`; `load_next_slot(path: Path = NEXT_SLOT_FILE) -> datetime | None`; `save_next_slot(when: datetime, path: Path = NEXT_SLOT_FILE) -> None`; `schedule_upload(youtube_client, anthropic_client, work_dir: Path, settings, now: datetime | None = None, next_slot_path: Path = NEXT_SLOT_FILE) -> str`. Used by Task 8 and Task 9 (`gui.py`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_youtube_schedule.py
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.youtube_schedule import compute_next_publish_slot, schedule_upload


def test_compute_next_publish_slot_returns_now_when_nothing_reserved_yet():
    now = datetime(2026, 9, 10, 10, 0, 0)

    assert compute_next_publish_slot(now, None, min_days_between=2, preferred_hour=15) == now


def test_compute_next_publish_slot_spaces_from_the_reserved_slot_not_now():
    reserved = datetime(2026, 9, 10, 15, 0, 0)
    now = datetime(2026, 9, 10, 10, 5, 0)  # much earlier than the reserved slot

    slot = compute_next_publish_slot(now, reserved, min_days_between=2, preferred_hour=15)

    assert slot == datetime(2026, 9, 12, 15, 0, 0)


def test_compute_next_publish_slot_snaps_to_the_preferred_hour():
    reserved = datetime(2026, 9, 10, 9, 47, 33)

    slot = compute_next_publish_slot(datetime(2026, 9, 10), reserved, min_days_between=1, preferred_hour=15)

    assert slot == datetime(2026, 9, 11, 15, 0, 0)


def test_compute_next_publish_slot_chain_spaces_multiple_batch_items_evenly():
    slot1 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 0), None, min_days_between=2, preferred_hour=15)
    slot2 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 1), slot1, min_days_between=2, preferred_hour=15)
    slot3 = compute_next_publish_slot(datetime(2026, 9, 10, 8, 2), slot2, min_days_between=2, preferred_hour=15)

    assert slot1 == datetime(2026, 9, 10, 8, 0)
    assert slot2 == datetime(2026, 9, 12, 15, 0, 0)
    assert slot3 == datetime(2026, 9, 14, 15, 0, 0)


class _FakeUploadRequest:
    def __init__(self, video_id):
        self._video_id = video_id

    def next_chunk(self):
        return None, {"id": self._video_id}


class _FakeVideosResource:
    def __init__(self, video_id):
        self._video_id = video_id
        self.insert_kwargs = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeUploadRequest(self._video_id)


class _FakeYoutubeClient:
    def __init__(self, video_id="vid123"):
        self._videos = _FakeVideosResource(video_id)

    def videos(self):
        return self._videos


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def create(self, **kwargs):
        return _FakeAnthropicResponse(
            "TITLE: My Song - Play Along\nDESCRIPTION: A great song.\nTAGS: tag1, tag2"
        )


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _make_song_work_dir(tmp_path) -> Path:
    work_dir = tmp_path / "my-song"
    work_dir.mkdir()
    song = Song(
        title="My Song", audio_path="song.mp3",
        lines=[LyricLine(words=[Word(word="hello"), Word(word="there")])],
    )
    save_song(song, work_dir / "lyrics_timed.json")
    (work_dir / "my-song.mp4").write_bytes(b"fake video bytes")
    return work_dir


def test_schedule_upload_public_uses_private_status_with_publish_at(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="public", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid123")
    next_slot_path = tmp_path / "next_slot.json"

    video_id = schedule_upload(
        client, _FakeAnthropicClient(), work_dir, settings,
        now=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc), next_slot_path=next_slot_path,
    )

    assert video_id == "vid123"
    body = client._videos.insert_kwargs["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert "publishAt" in body["status"]
    assert next_slot_path.exists()


def test_schedule_upload_unlisted_skips_publish_at_and_next_slot_file(tmp_path):
    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="unlisted", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid456")
    next_slot_path = tmp_path / "next_slot.json"

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings, next_slot_path=next_slot_path)

    body = client._videos.insert_kwargs["body"]
    assert body["status"]["privacyStatus"] == "unlisted"
    assert "publishAt" not in body["status"]
    assert not next_slot_path.exists()


def test_schedule_upload_saves_youtube_state(tmp_path):
    from lyricvideo.youtube_state import load_youtube_state

    work_dir = _make_song_work_dir(tmp_path)
    settings = SimpleNamespace(
        youtube_privacy="private", youtube_category_id="26", youtube_made_for_kids=False,
        youtube_min_days_between_uploads=2, youtube_preferred_upload_hour=15,
    )
    client = _FakeYoutubeClient(video_id="vid789")

    schedule_upload(client, _FakeAnthropicClient(), work_dir, settings, next_slot_path=tmp_path / "next_slot.json")

    state = load_youtube_state(work_dir)
    assert state is not None
    assert state.video_id == "vid789"
    assert state.title == "My Song - Play Along"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_schedule.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.youtube_schedule'`

- [ ] **Step 3: Write the implementation**

```python
# lyricvideo/youtube_schedule.py
"""Upload spacing/scheduling: uploads a finished video immediately, letting
YouTube's own publishAt scheduling do the actual publishing for a Public
target -- see docs/superpowers/specs/2026-09-10-youtube-upload-design.md for
why this replaced an earlier local-queue design (verified against YouTube's
real videos.insert docs: publishAt requires privacyStatus="private" at
upload time, and YouTube auto-publishes at that moment, even immediately if
publishAt is already in the past)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .models import load_song
from .pipeline import slugify
from .youtube import upload_video
from .youtube_metadata import generate_video_metadata
from .youtube_state import YoutubeState, save_youtube_state

log = logging.getLogger("playalongvideoproduction")

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
NEXT_SLOT_FILE = CREDENTIALS_DIR / "youtube_next_slot.json"


def load_next_slot(path: Path = NEXT_SLOT_FILE) -> datetime | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["next_slot"])
    except (OSError, ValueError, KeyError):
        return None


def save_next_slot(when: datetime, path: Path = NEXT_SLOT_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"next_slot": when.isoformat()}), encoding="utf-8")


def compute_next_publish_slot(
    now: datetime, reserved_slot: datetime | None, min_days_between: int, preferred_hour: int,
) -> datetime:
    """The next publish time to reserve for a newly-scheduled video. The
    very first video ever scheduled publishes immediately (`now`) -- there's
    nothing to space it against yet. Every video after that is spaced
    `min_days_between` days past whichever slot was reserved LAST (never
    past `now`), so scheduling several videos back-to-back (a batch run)
    still lands them one every N days on the channel, in the order they
    were scheduled, landing on `preferred_hour` local time."""
    if reserved_slot is None:
        return now
    candidate = reserved_slot + timedelta(days=min_days_between)
    return candidate.replace(hour=preferred_hour, minute=0, second=0, microsecond=0)


def schedule_upload(
    youtube_client,
    anthropic_client,
    work_dir: Path,
    settings,
    now: datetime | None = None,
    next_slot_path: Path = NEXT_SLOT_FILE,
) -> str:
    """The single upload code path used by every trigger (auto-upload AND
    the manual button) -- there is no separate "immediate" vs "queued"
    upload function. Re-derives everything needed from the song's own
    lyrics_timed.json rather than trusting anything passed in ahead of
    time, so it's always working from the actual finished video."""
    now = now or datetime.now().astimezone()
    song = load_song(work_dir / "lyrics_timed.json")
    full_lyrics = "\n".join(line.text for line in song.lines)
    title, description, tags = generate_video_metadata(anthropic_client, song.title, full_lyrics)
    video_path = work_dir / f"{slugify(song.title)}.mp4"

    if settings.youtube_privacy == "public":
        slot = compute_next_publish_slot(
            now, load_next_slot(next_slot_path),
            settings.youtube_min_days_between_uploads, settings.youtube_preferred_upload_hour,
        )
        video_id = upload_video(
            youtube_client, video_path, title, description, tags,
            privacy="private", publish_at=slot,
            category_id=settings.youtube_category_id, made_for_kids=settings.youtube_made_for_kids,
        )
        save_next_slot(slot, next_slot_path)
    else:
        # Unlisted/Private have no "publish later" concept on YouTube -- upload
        # immediately with that literal status, no scheduling machinery at all.
        video_id = upload_video(
            youtube_client, video_path, title, description, tags,
            privacy=settings.youtube_privacy, publish_at=None,
            category_id=settings.youtube_category_id, made_for_kids=settings.youtube_made_for_kids,
        )

    save_youtube_state(work_dir, YoutubeState(video_id=video_id, uploaded_at=now.isoformat(), title=title))
    return video_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_schedule.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Update CLAUDE.md and commit**

```bash
git add lyricvideo/youtube_schedule.py tests/test_youtube_schedule.py CLAUDE.md
git commit -m "Add YouTube upload scheduling via publishAt"
```

---

## Task 5: OAuth connection (`youtube_auth.py`)

**Files:**
- Create: `lyricvideo/youtube_auth.py`
- Test: `tests/test_youtube_auth.py`

**Interfaces:**
- Consumes: `google-auth-oauthlib`, `google-auth`, `google-api-python-client` (already added in Task 2).
- Produces: `connect(client_secrets_path: Path, token_path: Path = TOKEN_FILE)` (returns real `Credentials`, manually verified); `load_credentials(token_path: Path = TOKEN_FILE)` (returns `Credentials | None`); `get_channel_title(credentials) -> str`. Used by Task 8, 9, and 11 (`gui.py`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_youtube_auth.py
from lyricvideo.youtube_auth import load_credentials


def test_load_credentials_returns_none_when_no_token_file(tmp_path):
    assert load_credentials(tmp_path / "no_such_token.json") is None


def test_load_credentials_returns_none_on_corrupt_token_file(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("not valid json", encoding="utf-8")

    assert load_credentials(token_path) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.youtube_auth'`

- [ ] **Step 3: Write the implementation**

```python
# lyricvideo/youtube_auth.py
"""OAuth connection lifecycle for the owner's own YouTube channel, isolated
from the API-calling code in youtube.py so that module's functions can be
tested against a fake client without ever touching real OAuth machinery.
See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("playalongvideoproduction")

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
TOKEN_FILE = CREDENTIALS_DIR / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def connect(client_secrets_path: Path, token_path: Path = TOKEN_FILE):
    """Opens the owner's browser for one-time OAuth consent, then saves the
    resulting credentials (including refresh token) so future runs never
    need to ask again. Manually/visually verified (real browser interaction)
    -- see the spec's Testing section."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def load_credentials(token_path: Path = TOKEN_FILE):
    """None means "not connected" (never connected, or the stored token is
    unusable) -- callers always treat None this way, never raise."""
    if not token_path.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as exc:
        log.warning("Could not read stored YouTube credentials: %s", exc)
        return None

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not refresh YouTube credentials: %s", exc)
            return None
    return credentials


def get_channel_title(credentials) -> str:
    from googleapiclient.discovery import build

    youtube_client = build("youtube", "v3", credentials=credentials)
    response = youtube_client.channels().list(part="snippet", mine=True).execute()
    items = response.get("items", [])
    return items[0]["snippet"]["title"] if items else "(unknown channel)"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_auth.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Update CLAUDE.md and commit**

```bash
git add lyricvideo/youtube_auth.py tests/test_youtube_auth.py CLAUDE.md
git commit -m "Add YouTube OAuth connection lifecycle"
```

---

## Task 6: Settings fields

**Files:**
- Modify: `lyricvideo/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: seven new `Settings` fields (see below), flowing through the existing `save()`/`load()`/`from_dict()` machinery unchanged. Used by Task 4, 7, 8, 9, 11.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_settings.py`:

```python
def test_youtube_settings_defaults():
    from lyricvideo.settings import Settings

    s = Settings()
    assert s.youtube_auto_upload is False
    assert s.youtube_client_secrets_path == ""
    assert s.youtube_privacy == "public"
    assert s.youtube_category_id == "26"
    assert s.youtube_made_for_kids is False
    assert s.youtube_min_days_between_uploads == 2
    assert s.youtube_preferred_upload_hour == 15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_settings.py::test_youtube_settings_defaults -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'youtube_auto_upload'`

- [ ] **Step 3: Add the fields**

In `lyricvideo/settings.py`, add this new group to the `Settings` dataclass, right after the existing `min_chord_seconds: float = 0.5` field:

```python
    # --- YouTube (youtube_schedule.py / gui.py) --------------------------
    youtube_auto_upload: bool = False
    youtube_client_secrets_path: str = ""
    youtube_privacy: str = "public"          # "public" | "unlisted" | "private"
    youtube_category_id: str = "26"          # YouTube's own category id -- 26 = "Howto & Style"
    youtube_made_for_kids: bool = False      # COPPA declaration, required on every upload
    youtube_min_days_between_uploads: int = 2
    youtube_preferred_upload_hour: int = 15  # 24h local time (0-23); 3 PM matches research
                                              # on peak engagement windows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_settings.py -v`
Expected: PASS (all tests, including the new one)

- [ ] **Step 5: Update CLAUDE.md and commit**

```bash
git add lyricvideo/settings.py tests/test_settings.py CLAUDE.md
git commit -m "Add YouTube upload settings fields"
```

---

## Task 7: Settings panel widgets

**Files:**
- Modify: `lyricvideo/settings_panel.py`
- Test: `tests/test_settings_panel.py`

**Interfaces:**
- Consumes: `Settings` (Task 6).
- Produces: extends `_INT_FIELDS` and `values_to_settings()` (existing, both already unit-tested) to cover the two new int fields and the category label<->id translation; adds a "YouTube" section to `SettingsPanel._build()` (GUI widget construction, manually/visually verified per this project's existing precedent).

- [ ] **Step 1: Write the failing tests**

Modify `tests/test_settings_panel.py`: extend `_raw_defaults()` to include the seven new keys (the category dropdown stores its friendly label, like every other `_option()`-backed field), then add new test functions.

```python
def _raw_defaults() -> dict:
    """..."""
    return {
        "resolution": "1080p (1920x1080)",
        "fps": "24",
        "encoder": "libx264",
        "crf": 20.0,
        "font_path": "",
        "lyric_size": 48.0,
        "chord_now_size": 64.0,
        "chord_next_size": 32.0,
        "accent_color": "#38bdf8",
        "text_color": "#ffffff",
        "dim_text_color": "#94a3b8",
        "panel_color": "#0b1220",
        "panel_alpha": 150.0,
        "show_chord_timeline": True,
        "show_key_bpm": True,
        "timeline_window_sec": 12.0,
        "snap_chords_to_key": True,
        "prefer_flats": True,
        "include_seventh_chords": False,
        "min_chord_seconds": 0.5,
        "show_chord_legend": True,
        "chord_legend_size": 100.0,
        "youtube_auto_upload": False,
        "youtube_client_secrets_path": "",
        "youtube_privacy": "public",
        "youtube_category_id": "Howto & Style",
        "youtube_made_for_kids": False,
        "youtube_min_days_between_uploads": 2.0,
        "youtube_preferred_upload_hour": 15.0,
    }


def test_values_to_settings_translates_category_label_to_id():
    from lyricvideo.settings_panel import values_to_settings

    raw = _raw_defaults()
    raw["youtube_category_id"] = "Education"

    assert values_to_settings(raw).youtube_category_id == "27"


def test_values_to_settings_produces_the_youtube_defaults_from_default_raw_values():
    from lyricvideo.settings import Settings
    from lyricvideo.settings_panel import values_to_settings

    assert values_to_settings(_raw_defaults()) == Settings()
```

(NOTE: if `_raw_defaults()` in the existing file already includes `show_chord_legend`/`chord_legend_size` keys from an earlier feature, keep those as-is — only add the seven new `youtube_*` keys shown above; don't duplicate keys that already exist.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_settings_panel.py -v`
Expected: FAIL — `test_values_to_settings_translates_category_label_to_id` fails because `values_to_settings` doesn't yet translate the label, and `test_values_to_settings_produces_the_youtube_defaults_from_default_raw_values` fails because `Settings.from_dict` silently drops the unrecognized `"Howto & Style"` string mismatch isn't actually an error (it's tolerant) but the resulting `Settings().youtube_category_id` will be `"Howto & Style"` instead of `"26"`, so the equality assertion fails.

- [ ] **Step 3: Write the implementation**

In `lyricvideo/settings_panel.py`, add the category translation table near the top (right after `_INT_FIELDS`):

```python
_INT_FIELDS = {
    "fps", "crf", "lyric_size", "chord_now_size", "chord_next_size", "panel_alpha", "chord_legend_size",
    "youtube_min_days_between_uploads", "youtube_preferred_upload_hour",
}

_YOUTUBE_CATEGORY_IDS = {"Howto & Style": "26", "Education": "27", "Music": "10"}
_YOUTUBE_CATEGORY_LABELS = {v: k for k, v in _YOUTUBE_CATEGORY_IDS.items()}
```

Modify `values_to_settings()`:

```python
def values_to_settings(raw: dict) -> Settings:
    """..."""
    coerced = dict(raw)
    for name in _INT_FIELDS:
        if name in coerced:
            coerced[name] = int(float(coerced[name]))
    if "youtube_category_id" in coerced:
        coerced["youtube_category_id"] = _YOUTUBE_CATEGORY_IDS.get(
            coerced["youtube_category_id"], coerced["youtube_category_id"],
        )
    return Settings.from_dict(coerced)
```

Modify `SettingsPanel.load_from()` to translate the id back to a label when populating the dropdown:

```python
    def load_from(self, settings: Settings) -> None:
        self._suppress_change = True
        try:
            for name, value in asdict(settings).items():
                if name == "youtube_category_id":
                    value = _YOUTUBE_CATEGORY_LABELS.get(value, value)
                if name in self.vars:
                    self.vars[name].set(value)
        finally:
            self._suppress_change = False
        self._changed()
```

Add a browse method and the new section to `SettingsPanel`:

```python
    def _browse_youtube_secrets(self) -> None:
        f = filedialog.askopenfilename(
            parent=self, title="Choose your client_secret_*.json file",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if f:
            self.vars["youtube_client_secrets_path"].set(f)
```

In `_build()`, after the existing `"Chord detection"` section:

```python
        self._section("YouTube")
        self.vars["youtube_client_secrets_path"] = tk.StringVar()
        self.vars["youtube_client_secrets_path"].trace_add("write", lambda *_: self._changed())
        self._add("Client secrets file",
                   ctk.CTkButton(self, text="Browse client secrets file...", command=self._browse_youtube_secrets))
        self._check("youtube_auto_upload", "Auto-upload finished videos to YouTube")
        self._option("youtube_privacy", "Privacy", ["public", "unlisted", "private"])
        self._option("youtube_category_id", "Category", list(_YOUTUBE_CATEGORY_IDS.keys()))
        self._check("youtube_made_for_kids", "Made for kids")
        self._slider("youtube_min_days_between_uploads", "Minimum days between uploads", 1, 14, 13, lambda v: f"{int(v)}d")
        self._slider("youtube_preferred_upload_hour", "Preferred upload hour", 0, 23, 23,
                     lambda v: f"{int(v) % 12 or 12}{'AM' if int(v) < 12 else 'PM'}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_settings_panel.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/python3 -m pytest -q`
Expected: PASS (no regressions in `test_settings_preview.py` or elsewhere — `SettingsPreviewFrame` builds a `Settings` object directly and never goes through the panel's widgets, so it's unaffected)

- [ ] **Step 6: Update CLAUDE.md and commit**

```bash
git add lyricvideo/settings_panel.py tests/test_settings_panel.py CLAUDE.md
git commit -m "Add YouTube section to the Settings panel"
```

---

## Task 8: Auto-upload trigger wiring (`gui.py`)

**Files:**
- Modify: `lyricvideo/gui.py`
- Test: `tests/test_gui.py`

**Interfaces:**
- Consumes: `youtube_auth.load_credentials` (Task 5), `load_youtube_state` (Task 1), `schedule_upload` (Task 4), `Settings` (Task 6).
- Produces: module-level `_maybe_upload_to_youtube(work_dir: Path, settings: Settings) -> None`, called from `_run_worker` (covers both Generate and Redo, which already share this method) and `_run_batch_worker`. Used only within `gui.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_gui.py`:

```python
from lyricvideo.gui import _maybe_upload_to_youtube
from lyricvideo.settings import Settings
from lyricvideo.youtube_state import YoutubeState, save_youtube_state


def test_maybe_upload_to_youtube_skips_when_auto_upload_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.youtube_auth.load_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("should not check credentials when disabled")),
    )
    settings = Settings(youtube_auto_upload=False)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_skips_when_not_connected(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: None)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: calls.append(True) or (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise

    assert calls == []


def test_maybe_upload_to_youtube_skips_when_already_uploaded(tmp_path, monkeypatch):
    save_youtube_state(tmp_path, YoutubeState(video_id="abc", uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr(
        "lyricvideo.gui.youtube_auth.load_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("should not check credentials once already-uploaded is known")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_calls_schedule_upload_when_eligible(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(
            (youtube_client, anthropic_client, work_dir, settings)
        ),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [("fake-youtube-client", "fake-anthropic-client", tmp_path, settings)]


def test_maybe_upload_to_youtube_never_raises_on_upload_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", _raise)
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_gui.py -v`
Expected: FAIL with `ImportError: cannot import name '_maybe_upload_to_youtube' from 'lyricvideo.gui'`

- [ ] **Step 3: Write the implementation**

Add these imports to the top of `lyricvideo/gui.py`, alongside the existing imports:

```python
import anthropic
from googleapiclient.discovery import build

from . import youtube_auth
from .youtube_schedule import schedule_upload
from .youtube_state import load_youtube_state
```

Add this module-level function near `_slugify`/`_split_log_text` (i.e., outside the `LyricVideoGUI` class, so it's importable and testable the exact same way those two already are):

```python
def _maybe_upload_to_youtube(work_dir: Path, settings: Settings) -> None:
    """Uploads work_dir's finished video to YouTube if auto-upload is on,
    YouTube is connected, and this song has never been uploaded before --
    Redo of an already-uploaded song is deliberately skipped here to avoid
    duplicate videos piling up (owner's explicit choice). Any failure is
    caught and logged -- an upload problem must never make an
    otherwise-successful video generation look like it failed."""
    if not settings.youtube_auto_upload:
        return
    if load_youtube_state(work_dir) is not None:
        return
    credentials = youtube_auth.load_credentials()
    if credentials is None:
        return

    try:
        youtube_client = build("youtube", "v3", credentials=credentials)
        anthropic_client = anthropic.Anthropic()
        schedule_upload(youtube_client, anthropic_client, work_dir, settings)
        print(f"Uploaded to YouTube: {work_dir.name}")
    except Exception as e:
        print(f"WARNING: YouTube upload failed for {work_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)
```

Wire it into `_run_worker` — find this existing block:

```python
            out_path = run_pipeline(
                audio_path,
                work_dir,
                title,
                start_stage=start_stage,
                settings=self.settings,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
            )
            self._queue.put(("done", str(out_path)))
```

and change it to:

```python
            out_path = run_pipeline(
                audio_path,
                work_dir,
                title,
                start_stage=start_stage,
                settings=self.settings,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
            )
            _maybe_upload_to_youtube(work_dir, self.settings)
            self._queue.put(("done", str(out_path)))
```

Wire it into `_run_batch_worker` — find both existing `results["succeeded"].append(item.title)` lines (one in the `if item.already_done:` branch, one in the `else:` branch) and add the same call directly after each:

```python
                    results["succeeded"].append(item.title)
                    _maybe_upload_to_youtube(item.work_dir, self.settings)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_gui.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/python3 -m pytest -q`
Expected: PASS, no regressions

- [ ] **Step 6: Update CLAUDE.md and commit**

```bash
git add lyricvideo/gui.py tests/test_gui.py CLAUDE.md
git commit -m "Wire YouTube auto-upload into Generate/Redo/Batch"
```

---

## Task 9: Connect button + manual upload button (`gui.py`)

**Files:**
- Modify: `lyricvideo/gui.py`

**Interfaces:**
- Consumes: `youtube_auth.connect`/`load_credentials`/`get_channel_title` (Task 5), `schedule_upload` (Task 4), `build`/`anthropic` (already imported in Task 8).
- Produces: a "Connect to YouTube" status/button pair and a manual "Upload to YouTube" button, both purely GUI wiring. This task's testing step is manual/visual verification, matching this project's established precedent for GUI code (see `settings_panel.py`'s own header comment).

- [ ] **Step 1: Add instance state in `__init__`**

In `LyricVideoGUI.__init__`, alongside the other instance variables (near `self._batch_results = ...`), add:

```python
        self._last_work_dir: Path | None = None
```

- [ ] **Step 2: Add the Connect-to-YouTube status/button**

In `_build_widgets`, find where `right` is constructed and `self.settings_preview`/`self.settings_panel` are added to it (search for `SettingsPreviewFrame(right,`). Immediately before the line that creates `self.settings_panel`, add:

```python
        youtube_connect_frame = ctk.CTkFrame(right, fg_color="transparent")
        youtube_connect_frame.pack(fill="x", padx=4, pady=(0, 6))
        self.youtube_status_var = tk.StringVar(value="YouTube: not connected")
        ctk.CTkLabel(youtube_connect_frame, textvariable=self.youtube_status_var, anchor="w").pack(side="left")
        ctk.CTkButton(
            youtube_connect_frame, text="Connect to YouTube", command=self._on_connect_youtube, width=160,
        ).pack(side="right")
```

At the end of `_build_widgets` (or right after `self._build_widgets()` is called in `__init__`, before `_check_api_keys()`), add a call to populate the initial status:

```python
        self._refresh_youtube_status()
```

- [ ] **Step 3: Add the manual "Upload to YouTube" button**

In `_build_widgets`, find the existing `status_frame` block (`ctk.CTkLabel(status_frame, text="Status:")...`). Add, right after the existing two labels in that frame:

```python
        self.upload_button = ctk.CTkButton(
            status_frame, text="Upload to YouTube", command=self._on_manual_upload, state="disabled", width=140,
        )
        self.upload_button.pack(side="left", padx=(12, 0))
```

- [ ] **Step 4: Add the handler methods**

Add these methods to `LyricVideoGUI` (near `_on_redo`/`_on_generate` is a natural spot):

```python
    def _refresh_youtube_status(self) -> None:
        credentials = youtube_auth.load_credentials()
        if credentials is None:
            self.youtube_status_var.set("YouTube: not connected")
            return
        try:
            channel = youtube_auth.get_channel_title(credentials)
            self.youtube_status_var.set(f"YouTube: connected as {channel}")
        except Exception:
            self.youtube_status_var.set("YouTube: connected (channel name unavailable)")

    def _on_connect_youtube(self) -> None:
        secrets_path = self.settings.youtube_client_secrets_path
        if not secrets_path:
            messagebox.showerror(
                "No client secrets file", "Choose your client_secret_*.json file in Settings first.",
            )
            return

        def worker():
            try:
                youtube_auth.connect(Path(secrets_path))
                self.root.after(0, self._refresh_youtube_status)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Could not connect to YouTube", f"{type(e).__name__}: {e}",
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_manual_upload(self) -> None:
        if self._last_work_dir is None:
            return
        work_dir = self._last_work_dir
        self.upload_button.configure(state="disabled")

        def worker():
            credentials = youtube_auth.load_credentials()
            if credentials is None:
                self.root.after(0, lambda: messagebox.showerror(
                    "Not connected", "Connect to YouTube in Settings first.",
                ))
                self.root.after(0, lambda: self.upload_button.configure(state="normal"))
                return
            try:
                youtube_client = build("youtube", "v3", credentials=credentials)
                anthropic_client = anthropic.Anthropic()
                schedule_upload(youtube_client, anthropic_client, work_dir, self.settings)
                self.root.after(0, lambda: messagebox.showinfo("Uploaded", "Video uploaded to YouTube."))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Upload failed", f"{type(e).__name__}: {e}"))
            finally:
                self.root.after(0, lambda: self.upload_button.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()
```

- [ ] **Step 5: Track `_last_work_dir` and enable the button on completion**

In `_on_generate`, find where the worker thread is started (`args=(audio_path, song_dir, ...)` — the version WITHOUT `"fetch_lyrics"` as the start stage) and add, on the line right before `thread.start()`:

```python
        self._last_work_dir = song_dir
```

In `_on_redo`, find the equivalent `thread.start()` call (`args=(audio_path, song_dir, title, "fetch_lyrics")`) and add the same line right before it.

In `_poll_queue`, find the `elif kind == "done":` block and add, as its last line:

```python
                self.upload_button.configure(
                    state="normal" if youtube_auth.load_credentials() is not None else "disabled"
                )
```

- [ ] **Step 6: Manually verify**

Run: `./run_playalongvideoproduction.sh`

Verify: the "YouTube: not connected" label and "Connect to YouTube" button appear above Settings; the "Upload to YouTube" button appears next to Status, disabled by default. This step needs a real `client_secret_*.json` (from Google Cloud Console, owner-provided) to verify the Connect flow end-to-end — if not available yet, confirm at minimum that clicking Connect with no secrets file path set shows the expected error dialog, and that the rest of the app (Generate/Redo/Batch) still works unaffected.

- [ ] **Step 7: Update CLAUDE.md and commit**

```bash
git add lyricvideo/gui.py CLAUDE.md
git commit -m "Add YouTube connect status and manual upload button"
```

---

## Task 10: Comment-tracking persistence (`youtube_comment_state.py`)

**Files:**
- Create: `lyricvideo/youtube_comment_state.py`
- Test: `tests/test_youtube_comment_state.py`

**Interfaces:**
- Consumes: nothing new (stdlib only).
- Produces: `PendingReply` frozen dataclass (`comment_id`, `video_id`, `author`, `comment_text`, `draft_reply`: all `str`, `is_error_report: bool`); `load_seen_comment_ids`/`mark_comments_seen`; `load_pending_replies`/`save_pending_replies`/`add_pending_reply`/`remove_pending_reply`. Used by Task 11 (`gui.py`'s comment panel).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_youtube_comment_state.py
from lyricvideo.youtube_comment_state import (
    PendingReply,
    add_pending_reply,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_reply,
    save_pending_replies,
)


def test_load_seen_comment_ids_empty_when_no_file(tmp_path):
    assert load_seen_comment_ids(tmp_path / "no_such_file.json") == set()


def test_mark_comments_seen_then_load_round_trips(tmp_path):
    path = tmp_path / "seen.json"

    mark_comments_seen(["c1", "c2"], path)
    mark_comments_seen(["c2", "c3"], path)

    assert load_seen_comment_ids(path) == {"c1", "c2", "c3"}


def _make_reply(comment_id="c1") -> PendingReply:
    return PendingReply(
        comment_id=comment_id, video_id="v1", author="Alice", comment_text="hi",
        draft_reply="thanks!", is_error_report=False,
    )


def test_load_pending_replies_empty_when_no_file(tmp_path):
    assert load_pending_replies(tmp_path / "no_such_file.json") == []


def test_add_then_load_pending_replies_round_trips(tmp_path):
    path = tmp_path / "pending.json"

    add_pending_reply(_make_reply("c1"), path)
    add_pending_reply(_make_reply("c2"), path)

    replies = load_pending_replies(path)
    assert [r.comment_id for r in replies] == ["c1", "c2"]


def test_remove_pending_reply_removes_only_the_matching_one(tmp_path):
    path = tmp_path / "pending.json"
    save_pending_replies([_make_reply("c1"), _make_reply("c2")], path)

    remove_pending_reply("c1", path)

    assert [r.comment_id for r in load_pending_replies(path)] == ["c2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_comment_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.youtube_comment_state'`

- [ ] **Step 3: Write the implementation**

```python
# lyricvideo/youtube_comment_state.py
"""Pure persistence for YouTube comment monitoring: which comment ids have
already been seen, and which drafted replies are still awaiting the owner's
review. See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
SEEN_COMMENTS_FILE = CREDENTIALS_DIR / "youtube_seen_comments.json"
PENDING_REPLIES_FILE = CREDENTIALS_DIR / "youtube_pending_replies.json"


def load_seen_comment_ids(path: Path = SEEN_COMMENTS_FILE) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def mark_comments_seen(comment_ids: list[str], path: Path = SEEN_COMMENTS_FILE) -> None:
    seen = load_seen_comment_ids(path)
    seen.update(comment_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen)), encoding="utf-8")


@dataclass(frozen=True)
class PendingReply:
    comment_id: str
    video_id: str
    author: str
    comment_text: str
    draft_reply: str
    is_error_report: bool


def load_pending_replies(path: Path = PENDING_REPLIES_FILE) -> list[PendingReply]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [PendingReply(**item) for item in data]
    except (OSError, ValueError, TypeError):
        return []


def save_pending_replies(replies: list[PendingReply], path: Path = PENDING_REPLIES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in replies]), encoding="utf-8")


def add_pending_reply(reply: PendingReply, path: Path = PENDING_REPLIES_FILE) -> None:
    replies = load_pending_replies(path)
    replies.append(reply)
    save_pending_replies(replies, path)


def remove_pending_reply(comment_id: str, path: Path = PENDING_REPLIES_FILE) -> None:
    replies = [r for r in load_pending_replies(path) if r.comment_id != comment_id]
    save_pending_replies(replies, path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_youtube_comment_state.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Update CLAUDE.md and commit**

```bash
git add lyricvideo/youtube_comment_state.py tests/test_youtube_comment_state.py CLAUDE.md
git commit -m "Add YouTube comment-tracking persistence"
```

---

## Task 11: Comment-review panel (`gui.py`)

**Files:**
- Modify: `lyricvideo/gui.py`

**Interfaces:**
- Consumes: `list_new_comments`/`post_reply` (Task 2), `draft_comment_reply` (Task 3), `load_youtube_state` (Task 1, already imported in Task 8), `load_seen_comment_ids`/`mark_comments_seen`/`PendingReply`/`add_pending_reply`/`load_pending_replies`/`remove_pending_reply` (Task 10), `youtube_auth.load_credentials` (already imported in Task 8), `PROJECT_ROOT` (existing module constant in `gui.py`).
- Produces: a "YouTube Comments" panel with Check Now + per-comment Approve/Dismiss, wired to a 20-minute periodic check. GUI wiring, manually/visually verified per this project's established precedent.

- [ ] **Step 1: Add the new imports**

Add to the top of `lyricvideo/gui.py`, alongside the imports added in Task 8:

```python
from .youtube import list_new_comments, post_reply
from .youtube_comment_state import (
    PendingReply,
    add_pending_reply,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_reply,
)
from .youtube_metadata import draft_comment_reply
```

- [ ] **Step 2: Add the panel to the layout**

In `_build_widgets`, right after the block that adds `self.settings_panel` to `right` (or wherever `right`'s children finish being laid out), add:

```python
        self._build_youtube_panel(right)
```

- [ ] **Step 3: Implement the panel and its handlers**

Add these methods to `LyricVideoGUI`:

```python
    def _build_youtube_panel(self, parent) -> None:
        frame = ctk.CTkFrame(parent)
        frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(header, text="YouTube Comments", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="Check Now", command=self._on_check_youtube_comments, width=100).pack(side="right")
        self.youtube_replies_frame = ctk.CTkScrollableFrame(frame, height=200)
        self.youtube_replies_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._render_pending_replies()
        self.root.after(20 * 60 * 1000, self._schedule_youtube_comment_check)

    def _render_pending_replies(self) -> None:
        for child in self.youtube_replies_frame.winfo_children():
            child.destroy()
        for reply in load_pending_replies():
            self._render_one_pending_reply(reply)

    def _render_one_pending_reply(self, reply: PendingReply) -> None:
        row = ctk.CTkFrame(self.youtube_replies_frame)
        row.pack(fill="x", pady=4)
        badge = " ⚠ possible error report" if reply.is_error_report else ""
        ctk.CTkLabel(
            row, text=f"{reply.author}: {reply.comment_text}{badge}", anchor="w", wraplength=400, justify="left",
        ).pack(fill="x", padx=6, pady=(6, 2))
        text_box = ctk.CTkTextbox(row, height=60)
        text_box.insert("1.0", reply.draft_reply)
        text_box.pack(fill="x", padx=6, pady=(0, 4))
        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkButton(
            buttons, text="Approve", width=80, command=lambda: self._on_approve_reply(reply, text_box),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            buttons, text="Dismiss", width=80, fg_color="gray30", hover_color="gray20",
            command=lambda: self._on_dismiss_reply(reply),
        ).pack(side="left")

    def _on_approve_reply(self, reply: PendingReply, text_box) -> None:
        text = text_box.get("1.0", "end").strip()

        def worker():
            credentials = youtube_auth.load_credentials()
            if credentials is None:
                return
            try:
                youtube_client = build("youtube", "v3", credentials=credentials)
                post_reply(youtube_client, reply.comment_id, text)
                remove_pending_reply(reply.comment_id)
                self.root.after(0, self._render_pending_replies)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Could not post reply", f"{type(e).__name__}: {e}",
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_dismiss_reply(self, reply: PendingReply) -> None:
        remove_pending_reply(reply.comment_id)
        self._render_pending_replies()

    def _on_check_youtube_comments(self) -> None:
        threading.Thread(target=self._check_youtube_comments_worker, daemon=True).start()

    def _check_youtube_comments_worker(self) -> None:
        credentials = youtube_auth.load_credentials()
        if credentials is None:
            return
        youtube_client = build("youtube", "v3", credentials=credentials)
        anthropic_client = anthropic.Anthropic()
        seen_ids = load_seen_comment_ids()
        new_ids = []
        for work_dir in sorted((PROJECT_ROOT / "work").glob("*")):
            state = load_youtube_state(work_dir)
            if state is None:
                continue
            for comment in list_new_comments(youtube_client, state.video_id, seen_ids):
                draft, is_error_report = draft_comment_reply(anthropic_client, comment.text, state.title)
                add_pending_reply(PendingReply(
                    comment_id=comment.comment_id, video_id=comment.video_id, author=comment.author,
                    comment_text=comment.text, draft_reply=draft, is_error_report=is_error_report,
                ))
                new_ids.append(comment.comment_id)
        if new_ids:
            mark_comments_seen(new_ids)
        self.root.after(0, self._render_pending_replies)

    def _schedule_youtube_comment_check(self) -> None:
        if youtube_auth.load_credentials() is not None:
            threading.Thread(target=self._check_youtube_comments_worker, daemon=True).start()
        self.root.after(20 * 60 * 1000, self._schedule_youtube_comment_check)
```

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python3 -m pytest -q`
Expected: PASS, no regressions (this task adds no new pytest-covered logic — every function here is either GUI wiring or a thin pass-through to already-tested Task 1/2/3/10 functions)

- [ ] **Step 5: Manually verify**

Run: `./run_playalongvideoproduction.sh`

Verify: the "YouTube Comments" panel appears with a "Check Now" button and an empty scrollable area (no pending replies yet, since nothing has been uploaded/checked). If a real connected channel with an app-uploaded video and at least one real comment is available, click Check Now and confirm a pending-reply row appears with the comment text, a drafted reply in an editable box, and working Approve/Dismiss buttons.

- [ ] **Step 6: Update CLAUDE.md and commit**

Write the full CLAUDE.md narrative for this entire feature now (all 11 tasks): OAuth connection via `youtube_auth.py`, auto-upload triggers, the `publishAt`-based scheduling redesign (and why), Claude-authored metadata, the manual upload button, and the comment-review panel. Given this is a substantial addition, also add a dated entry to `docs/CLAUDE_HISTORY.md` per this project's documentation practice for significant features.

```bash
git add lyricvideo/gui.py CLAUDE.md docs/CLAUDE_HISTORY.md
git commit -m "Add YouTube comment-review panel"
```

---

## Testing Note

Tasks 1-6, 8, and 10 have full automated test coverage (`pytest`). Tasks 7, 9, and 11 add GUI widgets/wiring on top of already-tested logic; per this project's established precedent (see `settings_panel.py`'s own header comment), widget construction itself stays manually/visually verified rather than unit-tested — the underlying logic each button/panel calls (Tasks 1-6, 8, 10) is exactly what already has real test coverage.

After all 11 tasks are complete, run the full suite one final time (`.venv/bin/python3 -m pytest -q`) and confirm the count of passing tests grew by roughly 35-40 over the pre-feature baseline, with zero failures.
