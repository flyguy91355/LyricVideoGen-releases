"""Upload spacing/scheduling: uploads a finished video immediately, letting
YouTube's own publishAt scheduling do the actual publishing for a Public
target -- see docs/superpowers/specs/2026-09-10-youtube-upload-design.md for
why this replaced an earlier local-queue design (verified against YouTube's
real videos.insert docs: publishAt requires privacyStatus="private" at
upload time, and YouTube auto-publishes at that moment, even immediately if
publishAt is already in the past).

Spacing itself is computed from the channel's own real, live schedule
(youtube.reserved_publish_datetimes), not a local running counter -- see
that function's docstring for the 2026-09-13 incident that replaced a
local youtube_next_slot.json file with this. compute_next_publish_slot
below also fills gaps in that real schedule (e.g. the owner manually
publishing an already-scheduled video early) rather than only ever
pushing new uploads further into the future.

2026-09-17: replaced the old "N days between uploads, one per day at a
single preferred hour" model with owner-configurable multiple-times-a-day
scheduling (`Settings.youtube_upload_times`, a comma-separated list of
real HH:MM local times -- its own length IS the uploads-per-day count,
no separate number to keep in sync). `Settings.youtube_uploads_per_day`
drives nothing IN HERE; it only feeds the Settings panel's auto-generated
default times (evenly_spaced_upload_times below). `Settings.
youtube_max_uploads_per_day` (2026-09-18, a deliberately SEPARATE field)
is a real cap on raw upload calls per day, enforced entirely by the
gui.py callers before they ever reach schedule_upload() -- independent
of the publish-time pacing this module does, so raising the upload cap
doesn't touch youtube_upload_times, and vice versa."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta
from pathlib import Path

from .chord_theory import load_easy_chord_capo_marker
from .models import load_song
from .pipeline import slugify
from .youtube import reserved_publish_datetimes, upload_video
from .youtube_metadata import build_easy_chord_title, generate_video_metadata
from .youtube_state import YoutubeState, save_youtube_state

log = logging.getLogger("playalongvideoproduction")

_DEFAULT_UPLOAD_TIME = time(15, 0)
_DAY_WINDOW_START_MINUTES = 9 * 60   # 9:00 AM
_DAY_WINDOW_END_MINUTES = 21 * 60    # 9:00 PM
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_upload_times(text: str) -> list[time]:
    """Turns Settings.youtube_upload_times ("9:30,14:00,19:00") into sorted,
    deduplicated time objects. Tolerant of a stray malformed entry (skipped,
    not fatal -- a typo in the settings box must never break scheduling);
    falls back to a single sane default if every entry is unusable, or the
    box is empty, so schedule_upload() always has at least one slot to work
    with."""
    seen: set[time] = set()
    for part in text.split(","):
        match = _TIME_RE.match(part)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            seen.add(time(hour, minute))
    return sorted(seen) if seen else [_DEFAULT_UPLOAD_TIME]


def format_upload_times(times: list[time]) -> str:
    return ",".join(f"{t.hour:02d}:{t.minute:02d}" for t in sorted(times))


def evenly_spaced_upload_times(count: int) -> list[time]:
    """Generates `count` sensible default upload times spread across a
    single daytime window (9 AM-9 PM), for the Settings panel to fill
    Settings.youtube_upload_times with when the owner moves the "Uploads
    per day" slider (owner request, 2026-09-17: "put in time defaults
    depending on the number per day" ... "during the day"). Whatever it
    generates is a starting point, not a lock -- the owner can hand-edit
    any individual time afterward (e.g. nudge one to 9:30); that edit
    sticks until the slider moves again. count=1 keeps the app's original
    single-upload default (3 PM); count>=2 spaces every slot evenly across
    the window, both endpoints included, e.g. count=3 -> 9:00/15:00/21:00,
    count=5 -> 9:00/12:00/15:00/18:00/21:00."""
    count = max(1, count)
    if count == 1:
        return [_DEFAULT_UPLOAD_TIME]
    span = _DAY_WINDOW_END_MINUTES - _DAY_WINDOW_START_MINUTES
    times = []
    for i in range(count):
        minute_of_day = round(_DAY_WINDOW_START_MINUTES + i * span / (count - 1))
        times.append(time(minute_of_day // 60, minute_of_day % 60))
    return times


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


def _has_happened(moment: datetime, reference: datetime) -> bool:
    """moment <= reference, treating a naive/aware mismatch as "not yet"."""
    try:
        return moment <= reference
    except TypeError:
        return False


def compute_next_publish_slot(
    now: datetime, claimed_datetimes: set[datetime], upload_times: list[time],
) -> datetime:
    """The next publish time to reserve for a newly-scheduled video --
    fills gaps in the channel's real, live schedule rather than only ever
    pushing new uploads further into the future. `upload_times` is the
    owner's configured list of daily slots (Settings.youtube_upload_times,
    via parse_upload_times); a day only counts as full once it already
    holds as many claimed videos (scheduled OR already-published; see
    youtube.reserved_publish_datetimes) as there are configured times --
    a claim at some OTHER hour entirely (a manual upload, or a leftover
    from before the owner last changed their configured times) still
    counts against that day's capacity even though it won't exact-match
    any configured time. Within a day that still has room, each configured
    time is tried in order and the first one that isn't itself already
    claimed wins -- so if the owner manually publishes an already-
    scheduled video early, freeing that exact slot, the very next new
    upload lands back in it instead of stacking past a still-claimed later
    slot (real incident, 2026-09-13, that motivated reading the channel's
    live state instead of a local counter in the first place). A slot must
    still be ahead of `now` to be used (real incident, night of 2026-09-14
    into 2026-09-15: a slot time that had already passed today got a
    publishAt already in the past, and YouTube auto-published immediately
    instead of scheduling into the future) -- once today has no usable
    slot left (full, or every remaining time already past), the walk rolls
    over to tomorrow."""
    local_now = now.astimezone() if now.tzinfo is not None else now
    sorted_times = sorted(upload_times)
    slot_minutes = {(t.hour, t.minute) for t in sorted_times}
    claimed_by_date: dict = {}
    for d in claimed_datetimes:
        # A video the owner published by hand ahead of its slot (2026-09-19)
        # already happened at an off-slot moment in the past; it must not
        # also use up one of that day's slots, or making a scheduled video
        # public would never open a slot for the next upload.
        if (d.hour, d.minute) not in slot_minutes and _has_happened(d, local_now):
            continue
        claimed_by_date.setdefault(d.date(), []).append((d.hour, d.minute))
    candidate_date = local_now.date()
    while True:
        day_claims = claimed_by_date.get(candidate_date, [])
        # A day is only "full" once it holds as many claims as there are
        # configured slots -- a claim at some other hour entirely (a
        # manual upload, or a leftover from before the owner changed their
        # configured times) still counts against that day's capacity,
        # even though it won't exact-match any configured time below.
        if len(day_claims) < len(sorted_times):
            for t in sorted_times:
                if (t.hour, t.minute) in day_claims:
                    continue  # exact slot already taken -- try the next one
                candidate = datetime.combine(candidate_date, t, tzinfo=local_now.tzinfo)
                if candidate > local_now:
                    return candidate
        candidate_date += timedelta(days=1)


# A "sentence end" for placing the support block: . ! or ? (plus any closing quote/bracket) followed by whitespace.
_SENTENCE_END_RE = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s)")
# A period after one of these (or after a lone capital, like "J. Cole") is not the end of a sentence.
_NOT_A_SENTENCE_END = {"dr", "mr", "mrs", "ms", "jr", "sr", "st", "vs", "feat", "ft", "vol", "no", "etc", "inc", "co"}


def _first_sentence_end(text: str) -> int | None:
    """Index just past the first sentence's closing punctuation, or None when there is no second sentence
    (the description is one sentence, or has no sentence break at all)."""
    for match in _SENTENCE_END_RE.finditer(text):
        word = re.search(r"([A-Za-z]+)$", text[:match.start()])
        token = word.group(1) if word else ""
        if match.group().startswith(".") and (
            token.lower() in _NOT_A_SENTENCE_END or (len(token) == 1 and token.isupper())
        ):
            continue
        return match.end() if text[match.end():].strip() else None
    return None


def place_support_text(description: str, support_text: str) -> str:
    """Puts the owner's support block (Settings.support_description_text, one or several lines) right after the
    description's FIRST sentence, so it shows without clicking "...more" -- owner, 2026-09-26, after comparing how
    real play-along channels word and place their ask (lines 1-3, never the bottom). A one-sentence description
    just gets the block after it. Blank support text changes nothing; a description that already contains the
    block is returned untouched, so re-running is harmless."""
    block = support_text.strip()
    body = description.strip()
    if not block:
        return description
    if not body:
        return block
    if block in body:
        return description
    end = _first_sentence_end(body)
    if end is None:
        return f"{body}\n\n{block}"
    return f"{body[:end].rstrip()}\n\n{block}\n\n{body[end:].strip()}"


def move_support_text(description: str, old_support_text: str, new_support_text: str) -> str:
    """For videos already on YouTube: removes the old support line (wherever it sits -- it was appended at the
    bottom) and places the new block after the first sentence. Running it twice gives the same result."""
    cleaned = description
    if old_support_text.strip():
        cleaned = cleaned.replace(old_support_text.strip(), "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return place_support_text(cleaned, new_support_text)


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
    # An EASY CHORD (capo) variant (owner, 2026-09-23: "should maybe have that in the upload file too")
    # gets its own deterministic title and a fixed description sentence, keeping the two versions of a
    # song clearly separate everywhere, not just in the filename. Claude is given the CLEAN original
    # title (never the "EasyChords"-suffixed one) so its description prompt never mentions the suffix.
    capo_info = load_easy_chord_capo_marker(work_dir)
    metadata_title = capo_info["original_title"] if capo_info else song.title
    title, description, tags = generate_video_metadata(anthropic_client, metadata_title, artist, full_lyrics)
    if capo_info is not None:
        title = build_easy_chord_title(capo_info["original_title"], artist, capo_info["capo_fret"])
        description = (
            f"EASY CHORDS version -- Capo {capo_info['capo_fret']}, play it in {capo_info['shape_key']} shapes "
            f"(original key: {capo_info['original_key']}).\n\n{description}"
        )
    support_text = getattr(settings, "support_description_text", "").strip()
    if support_text:
        description = place_support_text(description, support_text)
    video_path = work_dir / f"{slugify(song.title)}.mp4"

    if settings.youtube_privacy == "public":
        slot = compute_next_publish_slot(
            now, reserved_publish_datetimes(youtube_client),
            parse_upload_times(settings.youtube_upload_times),
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
