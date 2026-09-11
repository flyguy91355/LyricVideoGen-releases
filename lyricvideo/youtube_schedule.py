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


def _load_artist(work_dir: Path) -> str:
    """The real artist identify.py already resolved, straight from
    song_info.json next to this song's lyrics_timed.json -- so the upload
    title-writing prompt states a known fact instead of guessing. Missing
    file, corrupt JSON, or a genuinely unresolved artist (identify.py can
    legitimately return "") all fail soft to "" -- an upload must never be
    blocked by this, and "" tells generate_video_metadata to simply not
    mention an artist rather than fabricate one."""
    try:
        data = json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
        return str(data.get("artist") or "")
    except (OSError, ValueError):
        return ""


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
    artist = _load_artist(work_dir)
    title, description, tags = generate_video_metadata(anthropic_client, song.title, artist, full_lyrics)
    support_text = getattr(settings, "support_overlay_text", "").strip()
    if support_text:
        description = f"{description}\n\n{support_text}"
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
