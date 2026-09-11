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


def get_video_snippet(youtube_client, video_id: str) -> dict | None:
    """The video's current real snippet (title/description/tags/categoryId),
    straight from the API -- videos.update REPLACES the whole snippet part,
    so any edit must start from this, never a guessed/partial body. Returns
    None if the video no longer exists (deleted directly on YouTube)."""
    response = youtube_client.videos().list(part="snippet", id=video_id).execute()
    items = response.get("items", [])
    return items[0]["snippet"] if items else None


def update_video_description(youtube_client, video_id: str, description: str) -> None:
    """Changes ONLY the description, preserving every other snippet field
    (title, tags, categoryId, etc.) exactly as it is on YouTube right now --
    videos.update(part='snippet') replaces the ENTIRE snippet, the same real
    gotcha this project already hit with channels.update's brandingSettings.
    A no-op if the video no longer exists."""
    snippet = get_video_snippet(youtube_client, video_id)
    if snippet is None:
        return
    snippet["description"] = description
    youtube_client.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()


def video_exists(youtube_client, video_id: str) -> bool:
    """Whether video_id is still a real, live video on YouTube -- a locally
    saved video_id (youtube_state.json) can go stale if the owner deletes
    the video from YouTube Studio directly, and nothing else in this app
    would ever notice."""
    response = youtube_client.videos().list(part="id", id=video_id).execute()
    return bool(response.get("items"))


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
