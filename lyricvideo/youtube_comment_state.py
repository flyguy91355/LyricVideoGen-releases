"""Pure persistence for YouTube comment monitoring: which comment ids have
already been seen, and which drafted replies are still awaiting the owner's
review. See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .youtube_state import atomic_write_text, read_json_state

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
SEEN_COMMENTS_FILE = CREDENTIALS_DIR / "youtube_seen_comments.json"
PENDING_REPLIES_FILE = CREDENTIALS_DIR / "youtube_pending_replies.json"
PENDING_COMMENTS_FILE = CREDENTIALS_DIR / "youtube_pending_comments.json"
DRAFT_FAILURES_FILE = CREDENTIALS_DIR / "youtube_comment_draft_failures.json"
# After this many failed reply drafts for one comment, the scan should stop paying for more tries -- e.g. queue it with a
# blank draft for the owner to answer by hand, and mark it seen (issue #7 review, F083/F084).
MAX_DRAFT_FAILURES = 3

# Every load-modify-save below holds this lock (issue #7 review, F143): the comment scan thread, Approve worker threads
# and the GUI thread's Dismiss all change these files, and an unlocked read-modify-write let one of them drop another's
# change (an Approve racing the scan's add lost a viewer's comment for good). Writes are atomic (temp file + replace);
# an unreadable file is moved aside to *.corrupt-<time> rather than silently overwritten.
_LOCK = threading.RLock()


def load_seen_comment_ids(path: Path = SEEN_COMMENTS_FILE) -> set[str]:
    with _LOCK:
        data = read_json_state(path, [], list)
    return {str(item) for item in data}


def mark_comments_seen(comment_ids: list[str], path: Path = SEEN_COMMENTS_FILE) -> None:
    with _LOCK:
        seen = load_seen_comment_ids(path)
        before = len(seen)
        seen.update(comment_ids)
        if len(seen) != before or not path.exists():
            atomic_write_text(path, json.dumps(sorted(seen)))


def mark_comment_seen(comment_id: str, path: Path = SEEN_COMMENTS_FILE) -> None:
    """One comment at a time -- for the comment scan to call right after each draft is saved, so a failure later in the
    scan never loses the progress already made (issue #7 review, F083/F084)."""
    mark_comments_seen([comment_id], path)


def record_draft_failure(comment_id: str, path: Path = DRAFT_FAILURES_FILE) -> int:
    """Counts one more failed reply draft for this comment; returns the count so far (compare with MAX_DRAFT_FAILURES)."""
    with _LOCK:
        counts = read_json_state(path, {}, dict)
        count = int(counts.get(comment_id, 0) or 0) + 1
        counts[comment_id] = count
        atomic_write_text(path, json.dumps(counts))
        return count


def clear_draft_failures(comment_id: str, path: Path = DRAFT_FAILURES_FILE) -> None:
    """Forgets a comment's failed drafts (once it was drafted, or given up on)."""
    with _LOCK:
        counts = read_json_state(path, {}, dict)
        if counts.pop(comment_id, None) is not None:
            atomic_write_text(path, json.dumps(counts))


@dataclass(frozen=True)
class PendingReply:
    comment_id: str
    video_id: str
    author: str
    comment_text: str
    draft_reply: str
    is_error_report: bool
    video_title: str = ""  # owner, 2026-10-03: "i dont know what song or video the comments are comming from" --
    # the uploaded video's own title (YoutubeState.title), so a reply drafted before this field existed just
    # shows blank rather than failing to load (_load_items' cls(**item) needs every field optional-or-present)


def _load_items(path: Path, cls):
    """Every well-formed entry of a list-of-dicts state file; a malformed entry is skipped rather than costing the rest."""
    items = []
    for item in read_json_state(path, [], list):
        try:
            items.append(cls(**item))
        except TypeError:
            continue
    return items


def load_pending_replies(path: Path = PENDING_REPLIES_FILE) -> list[PendingReply]:
    with _LOCK:
        return _load_items(path, PendingReply)


def save_pending_replies(replies: list[PendingReply], path: Path = PENDING_REPLIES_FILE) -> None:
    with _LOCK:
        atomic_write_text(path, json.dumps([asdict(r) for r in replies]))


def add_pending_reply(reply: PendingReply, path: Path = PENDING_REPLIES_FILE) -> bool:
    """Queues a drafted reply; a no-op (returns False) when that comment already has one pending -- a re-scan must
    never queue a second copy of the same viewer's comment (issue #7 review, F083)."""
    with _LOCK:
        replies = load_pending_replies(path)
        if any(r.comment_id == reply.comment_id for r in replies):
            return False
        replies.append(reply)
        save_pending_replies(replies, path)
        return True


def remove_pending_reply(comment_id: str, path: Path = PENDING_REPLIES_FILE) -> None:
    with _LOCK:
        replies = load_pending_replies(path)
        kept = [r for r in replies if r.comment_id != comment_id]
        if len(kept) != len(replies):
            save_pending_replies(kept, path)


@dataclass(frozen=True)
class PendingComment:
    """A Claude-drafted engagement comment awaiting the owner's review --
    not a reply to anyone, a fresh standalone comment posted as the channel
    owner, so it's keyed by video_id (there's no parent comment_id yet)."""

    video_id: str
    song_title: str
    draft_text: str


def load_pending_comments(path: Path = PENDING_COMMENTS_FILE) -> list[PendingComment]:
    with _LOCK:
        return _load_items(path, PendingComment)


def save_pending_comments(comments: list[PendingComment], path: Path = PENDING_COMMENTS_FILE) -> None:
    with _LOCK:
        atomic_write_text(path, json.dumps([asdict(c) for c in comments]))


def add_pending_comment(comment: PendingComment, path: Path = PENDING_COMMENTS_FILE) -> bool:
    """Queues an engagement-comment draft; a no-op (returns False) when that video already has one pending."""
    with _LOCK:
        comments = load_pending_comments(path)
        if any(c.video_id == comment.video_id for c in comments):
            return False
        comments.append(comment)
        save_pending_comments(comments, path)
        return True


def remove_pending_comment(video_id: str, path: Path = PENDING_COMMENTS_FILE) -> None:
    with _LOCK:
        comments = load_pending_comments(path)
        kept = [c for c in comments if c.video_id != video_id]
        if len(kept) != len(comments):
            save_pending_comments(kept, path)
