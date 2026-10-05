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
import threading
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .assemble import rendered_stream_seconds
from .chord_theory import load_easy_chord_capo_marker
from .key_decision import KeyNotConfirmed, confirmed_key_for_upload
from .key_note import split_key_note, strip_song_key_line
from .models import load_song
from .pipeline import slugify
from .youtube import reserved_publish_datetimes, set_thumbnail, upload_video
from .youtube_metadata import (
    build_easy_chord_title, build_play_along_title, generate_video_metadata, is_valid_youtube_title,
)
from .youtube_state import STATE_FILENAME, YoutubeState, save_youtube_state

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


def _slot_datetime(day: date, slot: time, aware: bool, tz) -> datetime:
    """`slot` on `day` as a real local moment. Localized for THAT day (issue #7 review, F023): borrowing today's UTC
    offset put every slot past a daylight-saving change an hour off (9:00 became 8:00 CST after the November change,
    and 10:00 CDT after the March one), and those off-slot claims then stopped holding their slots."""
    if not aware:
        return datetime.combine(day, slot)
    if tz is not None:
        return datetime.combine(day, slot, tzinfo=tz)
    return datetime.combine(day, slot).astimezone()     # the system zone's own offset for that date


def compute_next_publish_slot(
    now: datetime, claimed_datetimes: Iterable[datetime], upload_times: list[time], tz=None,
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
    over to tomorrow.

    `claimed_datetimes` holds one entry per video (a list; several videos at
    the same moment each count). `tz` is the zone slots are meant in -- None
    (the app's normal case) is this machine's own zone; tests pass a
    ZoneInfo so daylight-saving behavior is checked on any machine."""
    aware = now.tzinfo is not None
    local_now = now.astimezone(tz) if aware else now
    sorted_times = sorted(upload_times)
    slot_minutes = {(t.hour, t.minute) for t in sorted_times}
    claimed_by_date: dict = {}
    for d in claimed_datetimes:
        if d.tzinfo is not None:
            d = d.astimezone(tz)                            # read every claim on the local wall clock of its own date
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
                candidate = _slot_datetime(candidate_date, t, aware, tz)
                if candidate > local_now:
                    return candidate
        candidate_date += timedelta(days=1)


DESCRIPTION_PLACEHOLDER = "{description}"


def _template_parts(template: str) -> tuple[str, str]:
    """(top, bottom) of a support template: the text before and after the {description} marker. A template with
    no marker is all "bottom" -- the block simply goes below the description, as it always used to."""
    template = template.strip()
    if DESCRIPTION_PLACEHOLDER in template:
        top, bottom = template.split(DESCRIPTION_PLACEHOLDER, 1)
        return top.strip(), bottom.strip()
    return "", template


def render_description(template: str, description: str) -> str:
    """Builds the final YouTube description from Settings.support_description_text -- a small template: what goes
    ABOVE the song description, the marker {description}, and what goes BELOW, e.g. a tip link and a thank-you.
    Blank template: the description unchanged. Owner, 2026-09-29 (reversing a 2026-09-26 decision that put the ask
    on top): the song's own description reads FIRST -- long enough on its own to clear YouTube's "...more" cutoff
    -- with the tip/thank-you block below it; see CLAUDE_HISTORY for both dates' reasoning."""
    if not template.strip():
        return description
    top, bottom = _template_parts(template)
    return "\n\n".join(part for part in (top, description.strip(), bottom) if part)


def description_body(description: str, old_support_text: str | Sequence[str], template: str) -> str:
    """The song's own description text with the old support text (one string, or several -- e.g. the very old
    bottom "Support:" line AND the previous sign-off sentence) and any part of the current template taken out --
    what scripts/update_support_description.py re-renders, so a video already in the new layout comes out
    unchanged and an old-style one is converted."""
    olds = [old_support_text] if isinstance(old_support_text, str) else list(old_support_text)
    text = description
    for piece in (*(old.strip() for old in olds), *_template_parts(template)):
        if piece:
            text = text.replace(piece, "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def assemble_description(template: str, body: str, note: str = "") -> str:
    """The final text: the template's top block, the song description, a key-correction note (if any, right after
    the description -- owner, 2026-09-29), then the template's bottom block. Shared by rerender_description and
    scripts/update_support_description.py, which may substitute a freshly regenerated `body` for a too-short one
    before calling this."""
    top, bottom = _template_parts(template)
    return "\n\n".join(part for part in (top, body.strip(), note, bottom) if part)


def rerender_description(description: str, template: str, old_support_text: str | Sequence[str]) -> str:
    """An already-uploaded video's description in the current support-template layout, with its key note (a "📌 Song
    key: ..." correction from scripts/add_key_note.py) placed right after the song's own description and before the
    template's bottom block (owner, 2026-09-29, reversing a 2026-09-26 decision that kept it as the very first
    paragraph: the song description reads first now, not the key). Any OLD unconditional "🎸 Song key: X" line
    (every upload used to open with one) is stripped outright, never repositioned -- it carries no information
    the video's own badge doesn't already show. What scripts/update_support_description.py writes. A description
    already in this layout comes back unchanged; a note found anywhere else (e.g. pushed below the tip line by an
    older re-render) is moved back to this one spot, never duplicated (issue #7 review, F054's guarantee still
    holds, just at the new position)."""
    note, rest = split_key_note(description)
    rest = strip_song_key_line(rest)
    body = description_body(rest, old_support_text, template)
    return assemble_description(template, body, note)


class EasyChordVersionStale(KeyNotConfirmed):
    """An EASY CHORD video that no longer matches its song (made for another key, or from chords a Redo has since
    changed) -- it must be made again before it may upload. A KeyNotConfirmed, so every existing handler still applies."""


def _easy_chord_problem(work_dir: Path, capo_info: dict, settled_key: str, song) -> str:
    """Why this EASY CHORD version may not upload as it is ("" when it is fine). Cheap and deterministic -- run BEFORE
    any paid Claude call (issue #7 review, F086: a stale EASY folder paid for its description every 20 minutes, then
    was refused). Keys are compared as keys, not strings ("D# major" is "Eb major"); a nested `<song>/easychords`
    version's chords must still be exactly the song's own chords shifted by its capo (chord_theory.capo_track_matches)."""
    from .chord_theory import capo_track_matches
    from .key_estimate import parse_key

    if not isinstance(capo_info, dict):
        return "its EASY CHORD marker (easy_chord_capo.json) is unreadable -- make the EASY CHORD version again."
    original_key = str(capo_info.get("original_key") or "")
    capo_fret = capo_info.get("capo_fret")
    if not isinstance(capo_fret, int) or isinstance(capo_fret, bool) or not capo_info.get("original_title"):
        return "its EASY CHORD marker (easy_chord_capo.json) is incomplete -- make the EASY CHORD version again."
    marker_key, song_key = parse_key(original_key), parse_key(settled_key)
    if marker_key is None or marker_key != song_key:
        return (
            f"this EASY CHORD video was made for the key {original_key or '(none)'} but the song's key is now "
            f"{settled_key} -- make the EASY CHORD version again before uploading it."
        )
    work_dir = Path(work_dir)
    if work_dir.name == "easychords":
        parent_timed = work_dir.parent / "lyrics_timed.json"
        try:
            parent = load_song(parent_timed)
        except Exception as e:
            return f"its song's own chords could not be read to check it ({type(e).__name__}) -- nothing was uploaded."
        if not capo_track_matches(parent.chord_track, song.chord_track, capo_fret):
            return (
                f"its chords are no longer the song's own chords shifted by capo {capo_fret} (the song was redone "
                "since) -- make the EASY CHORD version again before uploading it."
            )
    return ""


def easy_chord_upload_problem(work_dir: Path) -> str:
    """Why `work_dir`'s EASY CHORD version must be rebuilt before it can upload; "" for an ordinary song, for an EASY
    version that is still right, and while the song's key is not settled (that hold is key_needs_attention's). For the
    pending-upload / Flagged lists, so a stale EASY folder is shown as needing a rebuild instead of being retried."""
    capo_info = load_easy_chord_capo_marker(work_dir)
    if capo_info is None:
        return ""
    try:
        settled_key = confirmed_key_for_upload(work_dir)
    except KeyNotConfirmed:
        return ""
    try:
        song = load_song(Path(work_dir) / "lyrics_timed.json")
    except Exception as e:
        return f"its lyrics_timed.json could not be read ({type(e).__name__})."
    return _easy_chord_problem(work_dir, capo_info, settled_key, song)


# One upload at a time, in this process (issue #7 review; the GUI's uploads are serialized on it). Two uploads at once
# could both read the channel's schedule before either had claimed a slot, and a manual Upload Selected overlapping the
# 20-minute auto-retry could send the same pending song twice.
_UPLOAD_LOCK = threading.Lock()


def upload_in_progress() -> bool:
    """True while schedule_upload() is uploading something (e.g. for the GUI's close confirmation)."""
    return _UPLOAD_LOCK.locked()


class AlreadyUploaded(RuntimeError):
    """schedule_upload(..., only_if_not_uploaded=True) found the song already uploaded (by a run that finished while
    this one waited its turn) -- nothing was sent."""


# How much shorter than its audio a video's picture may be and still upload: the same 1 s a render itself allows
# (assemble._check_rendered_video). A finished render's picture and audio are the same length by construction.
MAX_PICTURE_SHORTFALL_SECONDS = 1.0


class IncompleteVideo(RuntimeError):
    """The song's video must be made again before it can upload -- it is cut short (its picture stops well before its
    audio: issue #7, EASY CHORD videos with ~80 s of picture over 5-6 min of audio, rendered before renders became
    atomic), has no audio track, is missing, or could not be checked at all. Raised before the paid Claude description
    call and before any YouTube call, so nothing was spent and nothing was sent. `cut_short` is True only when the file
    was read and really is cut short (not for a missing or unreadable file, which may be a passing problem)."""

    def __init__(self, message: str, video_path: Path | None = None, cut_short: bool = False):
        super().__init__(message)
        self.video_path = video_path
        self.cut_short = cut_short


def is_cut_short(picture_seconds: float, audio_seconds: float | None) -> bool:
    """True when a video's picture ends more than MAX_PICTURE_SHORTFALL_SECONDS before its audio (the two numbers
    assemble.rendered_stream_seconds reads). Shared with scripts/find_truncated_videos.py."""
    return audio_seconds is not None and picture_seconds < audio_seconds - MAX_PICTURE_SHORTFALL_SECONDS


def check_video_complete(video_path: Path, label: str = "") -> tuple[float, float]:
    """(picture s, audio s) of a video that is whole; raises IncompleteVideo otherwise. Fails CLOSED: a file that is
    missing or can't be read back (damaged, or ffmpeg unavailable) is refused, never waved through. A stream-copy read
    (no decoding, a second or two, no network, nothing paid). The container's own Duration can't be trusted for this:
    a cut-short render reports the full audio length there."""
    video_path = Path(video_path)
    prefix = f"{label}: " if label else ""
    if not video_path.is_file():
        raise IncompleteVideo(f"{prefix}its video {video_path.name} does not exist -- nothing was uploaded.",
                              video_path)
    try:
        picture, audio = rendered_stream_seconds(video_path)
    except Exception as e:
        detail = " ".join(str(e).split())[:200]
        raise IncompleteVideo(
            f"{prefix}its video {video_path.name} could not be checked ({type(e).__name__}: {detail}) -- nothing was "
            "uploaded.", video_path,
        ) from e
    if audio is None:
        raise IncompleteVideo(
            f"{prefix}its video {video_path.name} has no readable audio track -- make the video again; nothing was "
            "uploaded.", video_path,
        )
    if is_cut_short(picture, audio):
        raise IncompleteVideo(
            f"{prefix}its video {video_path.name} is cut short: {picture:.0f} s of picture for {audio:.0f} s of audio. "
            "Make the video again (Redo, or rebuild the EASY CHORD version) -- nothing was uploaded.",
            video_path, cut_short=True,
        )
    return picture, audio


def schedule_upload(
    youtube_client,
    anthropic_client,
    work_dir: Path,
    settings,
    now: datetime | None = None,
    *,
    only_if_not_uploaded: bool = False,
) -> str:
    """The single upload code path used by every trigger (auto-upload AND
    the manual button) -- there is no separate "immediate" vs "queued"
    upload function. Re-derives everything needed from the song's own
    lyrics_timed.json rather than trusting anything passed in ahead of
    time, so it's always working from the actual finished video.

    Uploads one at a time (_UPLOAD_LOCK; a second caller waits its turn).
    `only_if_not_uploaded=True` (a pending-list retry, never the owner's
    deliberate re-upload) raises AlreadyUploaded instead of sending a song
    that another run finished uploading while this one waited. Every cheap
    check -- the settled key, a stale EASY CHORD version, a title YouTube
    would refuse, a missing or cut-short video (IncompleteVideo) -- runs
    before the paid Claude description call and before any YouTube call."""
    work_dir = Path(work_dir)
    with _UPLOAD_LOCK:
        # The FILE decides, not whether it parses: an empty or cut-off youtube_state.json still means the song went out
        # (issue #7 review, F106) -- a retry must never send it a second time.
        if only_if_not_uploaded and (work_dir / STATE_FILENAME).exists():
            raise AlreadyUploaded(f"{work_dir.name} was already uploaded -- not sent again.")
        return _schedule_upload_locked(youtube_client, anthropic_client, work_dir, settings, now)


def _schedule_upload_locked(youtube_client, anthropic_client, work_dir: Path, settings, now: datetime | None) -> str:
    from .banned import SongBanned, is_banned
    if is_banned(work_dir):
        raise SongBanned(f"{Path(work_dir).name} is tagged BANNED ON YOUTUBE, so it is never uploaded.")
    now = now or datetime.now().astimezone()
    # Owner, 2026-09-26: nothing goes out without the song's real key. Raises KeyNotConfirmed for a song whose key was never
    # checked or is waiting for the owner; the same settled key is what the description states.
    settled_key = confirmed_key_for_upload(work_dir)
    song = load_song(work_dir / "lyrics_timed.json")
    full_lyrics = "\n".join(line.text for line in song.lines)
    artist = _load_artist(work_dir)
    # An EASY CHORD (capo) variant (owner, 2026-09-23: "should maybe have that in the upload file too")
    # gets its own deterministic title and a fixed description sentence, keeping the two versions of a
    # song clearly separate everywhere, not just in the filename. Claude is given the CLEAN original
    # title (never the "EasyChords"-suffixed one) so its description prompt never mentions the suffix.
    capo_info = load_easy_chord_capo_marker(work_dir)
    if capo_info is not None:
        problem = _easy_chord_problem(work_dir, capo_info, settled_key, song)
        if problem:
            label = work_dir.parent.name if work_dir.name == "easychords" else work_dir.name
            raise EasyChordVersionStale(f"{label} (EASY CHORD version): {problem}")
        if work_dir.name == "easychords":
            # The lists' own check (pipeline.easy_variant_problem) adds what the key/chord check above lacks: lyrics or
            # timing a Redo of the song has changed since this EASY video was made. Cheap and cached; before any paid call.
            from .pipeline import easy_variant_problem

            problem = easy_variant_problem(work_dir)
            if problem:
                raise EasyChordVersionStale(f"{work_dir.parent.name} (EASY CHORD version): {problem}")
        planned_title = build_easy_chord_title(capo_info["original_title"], artist, capo_info["capo_fret"])
    else:
        planned_title = build_play_along_title(song.title, artist)
    if not is_valid_youtube_title(planned_title):
        raise ValueError(f"{work_dir.name}: YouTube would refuse the title {planned_title!r} -- nothing was uploaded.")
    video_path = work_dir / f"{slugify(song.title)}.mp4"
    # Issue #7: a cut-short video (picture far shorter than its audio) must never go out -- refused here, before the
    # paid description call and before any YouTube call (IncompleteVideo).
    song_label = f"{work_dir.parent.name}/easychords" if work_dir.name == "easychords" else work_dir.name
    check_video_complete(video_path, song_label)
    metadata_title = capo_info["original_title"] if capo_info else song.title
    title, description, tags = generate_video_metadata(anthropic_client, metadata_title, artist, full_lyrics)
    if capo_info is not None:
        title = planned_title
        # The settled key's own spelling (an older marker may say "D# major" for the same key as "Eb major"). Owner,
        # 2026-09-29: the description reads song-info-first; this capo/shape line -- real info the badge alone
        # doesn't convey -- comes right after it, never before.
        description = (
            f"{description}\n\nEASY CHORDS version -- Capo {capo_info['capo_fret']}, play it in "
            f"{capo_info['shape_key']} shapes (original key: {settled_key})."
        )
    # A plain video's own Key/BPM badge is already guaranteed correct (settled before any upload, same key the
    # render used) -- restating it in text would be redundant on every single video. Owner, 2026-09-29: "the key
    # if you need one, not all do if there correct." No song_key_line() here; a real correction (the badge was
    # wrong on an OLDER upload) is a separate, later fix via key_note.apply_key_note/rerender_description.
    support_text = getattr(settings, "support_description_text", "").strip()
    if support_text:
        description = render_description(support_text, description)

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

    try:
        save_youtube_state(work_dir, YoutubeState(video_id=video_id, uploaded_at=now.isoformat(), title=title))
    except OSError as e:
        # The video IS on YouTube: say so with its id, or the owner (and every retry) would think nothing went out.
        raise RuntimeError(
            f"{work_dir.name} was uploaded as {video_id} but could not be recorded in {work_dir / STATE_FILENAME} "
            f"({type(e).__name__}: {e}) -- do not upload it again; fix the folder and record that id."
        ) from e
    _set_thumbnail_soft(youtube_client, anthropic_client, work_dir, video_id, settings)
    return video_id


def _set_thumbnail_soft(youtube_client, anthropic_client, work_dir: Path, video_id: str, settings) -> None:
    """Makes the folder's thumbnail if it has none (thumbnail_job.py) and sets it on the video just uploaded (owner,
    2026-10-04). NEVER raises: the video is already on YouTube, so a failed thumbnail only warns -- scripts/
    backfill_thumbnails.py sets it later (a folder without thumbnail_set.json is retried there)."""
    import os
    from .thumbnail import THUMBNAIL_SET_FILE
    from .thumbnail_job import ensure_thumbnail

    if not getattr(settings, "generate_thumbnails", True):
        return
    try:
        path = ensure_thumbnail(
            work_dir, anthropic_client, os.environ.get("REPLICATE_API_TOKEN", ""),
            font_path=getattr(settings, "font_path", None) or None,
            show_chords=getattr(settings, "thumbnail_show_chords", True),
            use_song_images=getattr(settings, "thumbnail_use_song_images", True),
        )
        if path is None:
            log.warning("%s has no thumbnail; YouTube will pick one. Run scripts/backfill_thumbnails.py later.", work_dir.name)
            return
        set_thumbnail(youtube_client, video_id, path)
        (Path(work_dir) / THUMBNAIL_SET_FILE).write_text(json.dumps({"video_id": video_id}), encoding="utf-8")
    except Exception as e:
        log.warning("Could not set the thumbnail of %s (%s): %s: %s", work_dir.name, video_id, type(e).__name__, e)
