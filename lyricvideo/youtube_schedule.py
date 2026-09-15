"""Upload spacing/scheduling: uploads a finished video immediately, letting
YouTube's own publishAt scheduling do the actual publishing for a Public
target -- see docs/superpowers/specs/2026-09-10-youtube-upload-design.md for
why this replaced an earlier local-queue design (verified against YouTube's
real videos.insert docs: publishAt requires privacyStatus="private" at
upload time, and YouTube auto-publishes at that moment, even immediately if
publishAt is already in the past).

Spacing itself is computed from the channel's own real, live schedule
(youtube.reserved_publish_dates), not a local running counter -- see that
function's docstring for the 2026-09-13 incident that replaced a local
youtube_next_slot.json file with this. compute_next_publish_slot below
also fills gaps in that real schedule (e.g. the owner manually publishing
an already-scheduled video early) rather than only ever pushing new
uploads further into the future."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .models import load_song
from .pipeline import slugify
from .youtube import reserved_publish_dates, upload_video
from .youtube_metadata import generate_video_metadata
from .youtube_state import YoutubeState, save_youtube_state

log = logging.getLogger("playalongvideoproduction")


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
    now: datetime, claimed_dates: set[date], min_days_between: int, preferred_hour: int,
) -> datetime:
    """The next publish time to reserve for a newly-scheduled video --
    fills gaps in the channel's real, live schedule rather than only ever
    pushing new uploads further into the future. Walks forward day by day
    from `now`'s own date, and picks the first date that's at least
    `min_days_between` days from every date already claimed by an
    existing video (scheduled OR already-published; see
    youtube.reserved_publish_dates) -- so if the owner manually publishes
    an already-scheduled video early, or a batch run's mid-stream settings
    change once inflated the schedule by extra days (real incident,
    2026-09-13: a stale local counter compounded a 14-day gap onto every
    later upload), the very next new upload lands back in that opened-up
    gap instead of stacking further out past it. With `claimed_dates`
    empty (the very first video ever), today's date has no conflict and is
    returned right away, snapped to `preferred_hour` LOCAL time -- unless
    that time has already passed today, in which case the walk rolls over
    to tomorrow instead (real incident, night of 2026-09-14 into
    2026-09-15: two videos uploaded late in the evening, after that day's
    preferred_hour had already gone by, landed on an unclaimed "today" and
    got a publishAt already in the past -- YouTube auto-published both
    within a few hours instead of scheduling them into the future like
    every other video that same night; an unclaimed date is no longer
    enough on its own, the candidate must also still be ahead of `now`).
    `min_days_between=1` (the common case) means simply "any date with no
    existing video on it, in either direction" -- checked against BOTH
    neighbors, so filling a gap can never land a new video too close to
    what's already scheduled on either side of it."""
    local_now = now.astimezone() if now.tzinfo is not None else now
    candidate_date = local_now.date()
    while True:
        conflicts = any(abs((candidate_date - claimed).days) < min_days_between for claimed in claimed_dates)
        candidate = datetime.combine(candidate_date, time(hour=preferred_hour), tzinfo=local_now.tzinfo)
        if not conflicts and candidate > local_now:
            return candidate
        candidate_date += timedelta(days=1)


def schedule_upload(
    youtube_client,
    anthropic_client,
    work_dir: Path,
    settings,
    now: datetime | None = None,
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
    support_text = getattr(settings, "support_description_text", "").strip()
    if support_text:
        description = f"{description}\n\n{support_text}"
    video_path = work_dir / f"{slugify(song.title)}.mp4"

    if settings.youtube_privacy == "public":
        slot = compute_next_publish_slot(
            now, reserved_publish_dates(youtube_client),
            settings.youtube_min_days_between_uploads, settings.youtube_preferred_upload_hour,
        )
        video_id = upload_video(
            youtube_client, video_path, title, description, tags,
            privacy="private", publish_at=slot,
            category_id=settings.youtube_category_id, made_for_kids=settings.youtube_made_for_kids,
        )
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
