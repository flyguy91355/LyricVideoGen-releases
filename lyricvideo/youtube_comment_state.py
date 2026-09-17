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
PENDING_COMMENTS_FILE = CREDENTIALS_DIR / "youtube_pending_comments.json"


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
