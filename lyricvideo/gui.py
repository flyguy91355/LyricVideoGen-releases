from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import traceback
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from tkinter import font as tkfont
from tkinter import ttk

import customtkinter as ctk
from dotenv import load_dotenv

from .key_decision import KEY_HOLD_PREFIX, key_state, load_decision, load_owner_key, save_owner_key
from .key_estimate import candidate_keys, estimate_key_from_chords
from .venv import venv_python
from .batch import (
    find_audio_files,
    load_last_batch_folder,
    release_memory,
    resolve_batch_items,
    resolve_existing_folder,
    save_last_batch_folder,
)
from .identify import extract_metadata
from .dismissed_songs import dismiss_song, load_dismissed, undismiss_song
from .owner_lyrics import load_editable_lyrics, save_owner_lyrics
from .pipeline import (
    STAGES,
    HeldBeforeVideo,
    run_pipeline,
    build_capo_variant,
    slugify as _slugify,
    list_flagged_songs,
    needs_review,
    list_redoable_songs,
    list_pending_uploads,
    list_rendered_songs,
    list_uploadable_songs,
    list_easy_chord_backfill_candidates,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
    song_video_path,
    whisper_lines_for,
    easy_version_waits_on_key,
)
from .owner_whisper import current_lyric_line_texts, owner_whisper_corrections, save_owner_whisper_line
from .models import load_song
from .owner_verified import mark_verified, upload_label
from .settings import Settings
from .timing_gate import check_saved_song, hidden_note, use_pass_share_from
from .update.apply import (
    copy_updatable_files,
    extract_release_archive,
    is_source_commit_already_applied,
    read_release_source_commit,
    requirements_changed,
)
from .update.release_client import RELEASES_REPO, check_for_update
from .update.version import read_local_version, write_local_version
from . import youtube_auth
from .youtube import (
    is_quota_exceeded_error, is_video_public, list_new_comments, post_reply, post_top_level_comment,
    video_exists,
)
from .youtube_comment_state import (
    PendingComment,
    PendingReply,
    add_pending_reply,
    load_pending_comments,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_comment,
    remove_pending_reply,
)
from .youtube_metadata import build_play_along_title, draft_comment_reply
from .youtube_playlists import organize_video
from .youtube_quota_state import load_quota_blocked_until, save_quota_blocked_until
from .youtube_schedule import schedule_upload
from .youtube_state import load_youtube_state, save_youtube_state
from .youtube_upload_count_state import load_uploads_today, record_upload
# gui-behavior (issue #7 review): one upload run at a time, one comment scan at a time, EASY CHORD folders included.
import json
from .batch import holds_other_recording, stored_song_identity, work_dir_for
from .pipeline import review_concern
from .youtube_comment_state import MAX_DRAFT_FAILURES, clear_draft_failures, mark_comment_seen, record_draft_failure
from .youtube_schedule import AlreadyUploaded, IncompleteVideo, upload_in_progress
from .youtube_state import (
    STATE_FILENAME, atomic_write_text, mark_engagement_comment_posted, read_json_state, uploaded_song_dirs,
)

_CR_LF_RE = re.compile(r"[\r\n]")


class _LazyAnthropic:
    """Stands in for the `anthropic` module until an upload or a comment check really needs a client (issue #7, "long
    time to start": importing the SDK at launch cost ~1.3 s before the window could appear). `anthropic.Anthropic()` works
    exactly as before, and so does a test's monkeypatch of "lyricvideo.gui.anthropic.Anthropic"."""

    def __getattr__(self, name: str):
        import anthropic as _anthropic

        return getattr(_anthropic, name)


anthropic = _LazyAnthropic()


def build(*args, **kwargs):
    """googleapiclient.discovery.build, imported on first use rather than at launch (issue #7). Tests patch this name."""
    from googleapiclient.discovery import build as _build

    return _build(*args, **kwargs)


class _StopPolling(Exception):
    """Raised by _dispatch_queue_message to tell _poll_queue "this tick is done, don't reschedule" -- the same
    thing an early `return` inside _poll_queue itself used to mean, before that logic moved into its own method
    so a message handler that raises for real can be caught without losing track of these intentional exits."""


def _split_log_text(pending: str, text: str) -> tuple[str, str]:
    """Terminal-style \\r/\\n handling for the log widget: \\n commits the
    current line permanently, \\r discards it and starts the line over (this
    is what tqdm-style progress bars send on every update). Returns
    (new_pending_line, text_to_commit) -- text_to_commit is zero or more
    complete newline-terminated lines safe to insert verbatim; new_pending
    is the trailing not-yet-terminated content that should currently be
    showing as the widget's last, still-changeable line.
    """
    if not text:
        return pending, ""
    committed: list[str] = []
    current = pending
    start = 0
    for m in _CR_LF_RE.finditer(text):
        idx = m.start()
        current += text[start:idx]
        if m.group() == "\n":
            committed.append(current + "\n")
        current = ""
        start = idx + 1
    current += text[start:]
    return current, "".join(committed)


def _mark_engagement_comment_posted(video_id: str) -> None:
    """Records that video_id's engagement comment is done with, in whichever folder uploaded it -- an EASY CHORD
    version's nested `<song>/easychords` included (issue #7 review, F043-F045: only top-level folders were searched, so an
    EASY video kept engagement_comment_posted=False and a backfill drafted it another comment)."""
    mark_engagement_comment_posted(PROJECT_ROOT / "work", video_id)


# --- uploads: one run at a time, never the same song twice (issue #7 review, F007/F008/F010/F011/F017/F018/F049) -------
# A manual Upload / Upload Selected / Upload Anyway and the 20-minute auto-retry each walked the pending list on their own
# thread, and youtube_state.json is only written once an upload finishes (minutes on the owner's uplink) -- so two runs
# both uploaded the same songs. Now: _UPLOAD_RUN_LOCK lets only one pending-list run exist (a click while one runs is
# refused; the tick skips); every pending-list upload passes only_if_not_uploaded=True, so schedule_upload itself refuses
# a song another path finished meanwhile; and _UPLOAD_SLOT_LOCK makes "is there room under today's cap -> upload ->
# count it" one step, so two uploads can never both take the last slot.
_UPLOAD_RUN_LOCK = threading.Lock()
_UPLOAD_SLOT_LOCK = threading.Lock()


def _upload_run_active() -> bool:
    """True while a manual upload run or the tick's auto-retry is working through songs."""
    return _UPLOAD_RUN_LOCK.locked()


def _uploads_busy() -> bool:
    """True while any upload is running: a pending-list run, or a single upload (Generate/Redo/Batch auto-upload)."""
    return _upload_run_active() or upload_in_progress()


def _state_file_exists(work_dir: Path) -> bool:
    """Whether work_dir has a youtube_state.json at all -- readable or not. An unreadable (empty, cut-off) one still means
    the song WAS uploaded, so every "never uploaded?" check fails closed on it (issue #7 review, F106)."""
    return (Path(work_dir) / STATE_FILENAME).exists()


def _upload_within_todays_cap(youtube_client, anthropic_client, work_dir: Path, settings: Settings,
                              only_if_not_uploaded: bool) -> str:
    """One upload, counted against today's cap as a single step. "uploaded", "already" (another run uploaded it first --
    nothing sent), or "deferred" (today's cap is used up). Anything else raises, as schedule_upload does."""
    with _UPLOAD_SLOT_LOCK:
        if only_if_not_uploaded and _state_file_exists(work_dir):
            return "already"
        if _uploads_remaining_today(settings) <= 0:
            return "deferred"
        try:
            schedule_upload(youtube_client, anthropic_client, work_dir, settings,
                            only_if_not_uploaded=only_if_not_uploaded)
        except AlreadyUploaded:
            return "already"
        record_upload()
        return "uploaded"


def _set_upload_buttons(gui, state: str) -> None:
    """Upload / Upload Selected / the Flagged pane's Upload Anyway, together: all off while an upload run is going."""
    for name in ("retry_upload_button", "upload_selected_button"):
        button = getattr(gui, name, None)
        if button is not None:
            button.configure(state=state)
    upload_anyway = (getattr(gui, "_flagged_buttons", None) or {}).get("upload_anyway")
    if upload_anyway is not None and state == "disabled":
        upload_anyway.configure(state="disabled")   # the detail pane turns it back on per song (_show_flagged_details)


# Songs the 20-minute auto-retry failed (a stale EASY CHORD version, a title YouTube refuses, Claude failing to write a
# description...): tried again after this long instead of on every tick, which re-paid for the Claude description each
# time (wave-1 youtube request). Kept per GUI, for this session only; manual uploads ignore it.
_AUTO_RETRY_BACKOFF = timedelta(hours=6)


def _auto_retry_due(gui, slugs: list[str], now: datetime) -> list[str]:
    not_before = getattr(gui, "_auto_retry_not_before", None) or {}
    return [slug for slug in slugs if slug not in not_before or not_before[slug] <= now]


def _note_auto_retry_results(gui, results: dict | None, now: datetime) -> None:
    not_before = getattr(gui, "_auto_retry_not_before", None)
    if not_before is None:
        not_before = gui._auto_retry_not_before = {}
    for slug, _reason in (results or {}).get("failed", []):
        not_before[slug] = now + _AUTO_RETRY_BACKOFF
    for slug in (results or {}).get("succeeded", []):
        not_before.pop(slug, None)


# --- Approve: never post the same public reply/comment twice (issue #7 review, F038/F039/F115) -------------------------
# Approve started a new posting thread on every click, the button stayed on, and the draft left the queue only after the
# post returned -- so a second click on a slow link (or a laggy window) posted the same text twice. A draft being posted,
# or already posted this session, is claimed here per GUI; an Approve for a claimed draft does nothing.
_POSTING_LOCK = threading.Lock()
_NOT_CONNECTED_TITLE = "Not connected to YouTube"
_NOT_CONNECTED_MESSAGE = (
    "Your YouTube connection has expired (it lasts 7 days) or was never made. Click 'Connect to YouTube', then Approve "
    "again -- nothing was posted."
)


def _claim_posting(gui, key: str) -> bool:
    """Claims `key` ("reply:<comment id>" / "comment:<video id>") for posting; False when it is already claimed."""
    with _POSTING_LOCK:
        keys = getattr(gui, "_posting_keys", None)
        if keys is None:
            keys = gui._posting_keys = set()
        if key in keys:
            return False
        keys.add(key)
        return True


def _release_posting(gui, key: str) -> None:
    """Lets `key` be approved again -- after a post that failed or never went out (a posted one stays claimed)."""
    with _POSTING_LOCK:
        getattr(gui, "_posting_keys", set()).discard(key)


def _is_posting(gui, key: str) -> bool:
    with _POSTING_LOCK:
        return key in getattr(gui, "_posting_keys", set())


def _mark_draft_busy(gui, view_name: str, item_id: str, busy: bool) -> None:
    """Turns a draft's Approve/Dismiss off (or back on) in its list, if that list exists (Tk thread only)."""
    view = getattr(gui, view_name, None)
    if view is None:
        return
    try:
        view.set_busy(item_id, busy)
    except (tk.TclError, AttributeError):
        pass


def _post_draft_in_background(gui, key: str, view_name: str, item_id: str, post) -> None:
    """Runs `post(youtube_client)` on a worker thread for a draft already claimed with _claim_posting. `post` returns True
    once it really posted (the draft then stays claimed, so it is never posted again) or False when nothing went out (the
    claim is released and Approve works again). Not connected: says so, instead of silently doing nothing (F115)."""
    _mark_draft_busy(gui, view_name, item_id, True)
    worker_ran = []

    def worker():
        worker_ran.append(True)
        posted = False
        try:
            credentials = youtube_auth.load_credentials()
            if credentials is None:
                gui.root.after(0, lambda: messagebox.showerror(_NOT_CONNECTED_TITLE, _NOT_CONNECTED_MESSAGE))
                gui.root.after(0, gui._refresh_youtube_status)
                return
            try:
                youtube_client = build("youtube", "v3", credentials=credentials)
            except Exception as e:
                message = f"{type(e).__name__}: {e}"  # never read `e` inside the lambda (see _on_connect_youtube)
                gui.root.after(0, lambda: messagebox.showerror("Could not reach YouTube", message))
                return
            posted = bool(post(youtube_client))
        finally:
            if not posted:
                _release_posting(gui, key)
            gui.root.after(0, lambda: _mark_draft_busy(gui, view_name, item_id, False))

    try:
        threading.Thread(target=worker, daemon=True).start()
    except BaseException:
        if not worker_ran:
            _release_posting(gui, key)
            _mark_draft_busy(gui, view_name, item_id, False)
        raise


# --- the YouTube comment check (issue #7 review, F016/F043-F046/F116/F118/F119) -----------------------------------------
# Check Now and the 20-minute tick never scan at the same time (each would draft -- and queue -- the same comments), and a
# tick never starts while the previous one is still working through a long upload run.
_COMMENT_CHECK_LOCK = threading.Lock()
_TICK_LOCK = threading.Lock()
# Each public video's comment count at its last COMPLETE check. A video whose count has not changed has nothing new, so it
# costs no call: a check is ~1 quota unit per 50 videos instead of 2 per video every 20 minutes (hundreds of videos ate
# most of the 10,000-unit day, and each upload needs ~1,600). Once a day every public video with comments is read anyway
# (_COMMENT_FULL_CHECK_EVERY), in case YouTube's count moved before the comment itself could be listed.
_COMMENT_COUNTS_FILE = Path.home() / ".playalongvideoproduction" / "youtube_comment_counts.json"
_COMMENT_FULL_CHECK_EVERY = timedelta(hours=24)
_FULL_CHECK_KEY = "_full_check_at"


def _load_comment_counts() -> dict[str, int]:
    data = read_json_state(_COMMENT_COUNTS_FILE, {}, dict)
    return {str(k): v for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool)}


def _last_full_comment_check() -> datetime | None:
    value = read_json_state(_COMMENT_COUNTS_FILE, {}, dict).get(_FULL_CHECK_KEY)
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _save_comment_counts(counts: dict[str, int], full_check_at: datetime | None = None) -> None:
    data: dict = dict(counts)
    if full_check_at is not None:
        data[_FULL_CHECK_KEY] = full_check_at.isoformat()
    try:
        atomic_write_text(_COMMENT_COUNTS_FILE, json.dumps(data, sort_keys=True))
    except OSError as e:
        print(f"WARNING: could not save the comment counts: {e}", file=sys.stderr)


def _video_comment_stats(youtube_client, video_ids: list[str]) -> dict[str, tuple[bool, int | None]]:
    """{video id: (public?, comment count)} for many videos, one videos.list call (1 quota unit) per 50. The count is 0
    when YouTube leaves commentCount out (comments are off for that video) and None when it gave no statistics at all
    (unknown: the video is read). A deleted video is simply missing."""
    stats: dict[str, tuple[bool, int | None]] = {}
    unique = list(dict.fromkeys(v for v in video_ids if v))
    for start in range(0, len(unique), 50):
        chunk = unique[start:start + 50]
        response = youtube_client.videos().list(
            part="status,statistics", id=",".join(chunk), maxResults=50,
        ).execute()
        for item in response.get("items", []):
            public = (item.get("status") or {}).get("privacyStatus") == "public"
            numbers = item.get("statistics")
            count: int | None
            if not isinstance(numbers, dict):
                count = None
            else:
                try:
                    count = int(numbers.get("commentCount", 0))
                except (TypeError, ValueError):
                    count = None
            stats[str(item.get("id", ""))] = (public, count)
    return stats


def _own_channel_id(youtube_client) -> str:
    """The connected channel's id ("" when it cannot be read) -- to leave the channel's own comments alone."""
    try:
        items = youtube_client.channels().list(part="id", mine=True).execute().get("items", [])
        return str(items[0]["id"]) if items else ""
    except Exception:
        return ""


def _scan_youtube_comments(gui) -> None:
    """One comment check (the caller holds _COMMENT_CHECK_LOCK): every video this app uploaded -- EASY CHORD versions in
    `<song>/easychords` included (they were never checked) -- whose comment count changed since its last complete check
    gets its new comments read, a reply drafted and queued for the owner. Each comment is marked seen right after its
    draft is queued, so a failure later in the scan never drafts it again; a comment whose draft keeps failing is queued
    with an empty reply after MAX_DRAFT_FAILURES tries. A quota-exceeded error stops the scan and starts the cooldown."""
    credentials = youtube_auth.load_credentials()
    if credentials is None:
        return
    uploaded = uploaded_song_dirs(PROJECT_ROOT / "work")
    if not uploaded:
        return
    settings = gui.settings
    youtube_client = build("youtube", "v3", credentials=credentials)

    def stop_for_quota(where: str) -> None:
        save_quota_blocked_until(datetime.now().astimezone() + timedelta(hours=settings.youtube_quota_retry_hours))
        print(
            f"WARNING: YouTube quota exceeded checking comments{where}; will retry automatically in "
            f"{settings.youtube_quota_retry_hours}h.", file=sys.stderr,
        )

    try:
        stats = _video_comment_stats(youtube_client, [state.video_id for _slug, _path, state in uploaded])
    except Exception as e:
        if is_quota_exceeded_error(e):
            stop_for_quota("")
        else:
            print(f"WARNING: could not read the videos' comment counts: {type(e).__name__}: {e}", file=sys.stderr)
        return
    counts = _load_comment_counts()
    counts_before = dict(counts)
    now = datetime.now().astimezone()
    last_full = _last_full_comment_check()
    full_check = last_full is None or now - last_full >= _COMMENT_FULL_CHECK_EVERY
    stopped = False
    seen_ids = load_seen_comment_ids()
    anthropic_client = None
    own_channel: str | None = None
    added = False
    for slug, _work_dir, state in uploaded:
        public, comment_count = stats.get(state.video_id, (False, None))
        if not public:
            continue  # still scheduled/private (or deleted): YouTube refuses comment reads for it
        if comment_count == 0:
            continue  # no comments at all, or comments are off for it
        if not full_check and comment_count is not None and counts.get(state.video_id) == comment_count:
            continue  # nothing new since its last complete check -- no call spent
        try:
            comments = list_new_comments(youtube_client, state.video_id, seen_ids)
        except Exception as e:
            # One video's failure (comments disabled, a 404...) never blocks the rest -- except quota, which would fail
            # identically for every remaining video right now.
            if is_quota_exceeded_error(e):
                stop_for_quota(f" for {slug}")
                stopped = True
                break
            print(f"WARNING: could not check comments for {slug} ({state.video_id}): {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        complete = True
        for comment in comments:
            if comment.comment_id in seen_ids:
                continue
            author_channel = str(getattr(comment, "author_channel_id", "") or "")
            if author_channel:
                if own_channel is None:
                    own_channel = _own_channel_id(youtube_client)
                if author_channel == own_channel:
                    mark_comment_seen(comment.comment_id)   # the channel's own comment: never a reply to itself (F116)
                    seen_ids.add(comment.comment_id)
                    continue
            try:
                if anthropic_client is None:
                    anthropic_client = anthropic.Anthropic()
                draft, is_error_report = draft_comment_reply(anthropic_client, comment.text, state.title)
            except Exception as e:
                try:
                    failures = record_draft_failure(comment.comment_id)
                except Exception:
                    failures = 0
                if failures < MAX_DRAFT_FAILURES:
                    print(f"WARNING: could not draft a reply to a comment on {slug} ({type(e).__name__}: {e}); "
                          "it is tried again at the next check.", file=sys.stderr)
                    complete = False
                    continue
                print(f"WARNING: drafting a reply to a comment on {slug} failed {failures} times; it is queued with an "
                      "empty reply for you to write.", file=sys.stderr)
                draft, is_error_report = "", False
            try:
                if add_pending_reply(PendingReply(
                    comment_id=comment.comment_id, video_id=comment.video_id, author=comment.author,
                    comment_text=comment.text, draft_reply=draft, is_error_report=is_error_report,
                    video_title=state.title,
                )):
                    added = True
                mark_comment_seen(comment.comment_id)
                seen_ids.add(comment.comment_id)
                clear_draft_failures(comment.comment_id)
            except Exception as e:
                print(f"WARNING: could not save a drafted reply for {slug}: {type(e).__name__}: {e}", file=sys.stderr)
                complete = False
        if complete and comment_count is not None:
            counts[state.video_id] = comment_count
    full_done = full_check and not stopped
    if counts != counts_before or full_done:
        _save_comment_counts(counts, now if full_done else last_full)
    if added:
        # Only when something new arrived, and only ADDING rows (issue #7 review): this used to rebuild every reply row
        # every 20 minutes, resetting whatever reply the owner was in the middle of editing.
        gui.root.after(0, gui._on_pending_replies_changed)


def _uploads_remaining_today(settings: Settings) -> int:
    """How many more raw upload_video() calls this app may still make today
    under Settings.youtube_max_uploads_per_day -- see
    youtube_upload_count_state.py for the real enforced ceiling this
    tracks (2026-09-18), a SEPARATE field from youtube_uploads_per_day
    (which only sizes the Settings panel's publish-time-slot generator,
    unchanged). Independent of publish-time scheduling on purpose (owner
    decision, 2026-09-18): this caps raw upload *calls* to protect quota,
    while youtube_upload_times separately paces when uploaded videos go
    public -- a big backlog of already-uploaded, still-scheduled videos is
    fine and not something this caps."""
    return max(0, settings.youtube_max_uploads_per_day - load_uploads_today())


def _is_easy_variant(slug: str) -> bool:
    """A "<song>/easychords" slug: an EASY CHORD (capo) version nested in its song's folder (pipeline.build_capo_variant).
    It is remade from its song, never run through the pipeline itself (see _show_flagged_details)."""
    parts = Path(slug).parts
    return len(parts) > 1 and parts[-1] == "easychords"


def _easy_variant_parent(slug: str) -> str:
    """The song an EASY CHORD version is made from ("<song>/easychords" -> "<song>"); a normal slug is its own."""
    return Path(slug).parent.as_posix() if _is_easy_variant(slug) else slug


def _refuse_easy_variant(slug: str, action: str) -> bool:
    """True (after telling the owner why) when `slug` is an EASY CHORD version: `action` runs on the song it is made
    from, and the EASY version is then remade with Rebuild EASY version (issue #7 review: run on the capo folder itself,
    Redo / Render Anyway / Set Key dropped the CAPO badge, labelled shape chords with the original key and even built a
    second, doubly-transposed capo version inside it)."""
    if not _is_easy_variant(slug):
        return False
    messagebox.showinfo(
        "EASY CHORD version",
        f'"{slug}" is the EASY CHORD version of "{_easy_variant_parent(slug)}" -- its lyrics, timing and key come from '
        f"that song. Use {action} on that song, then Rebuild EASY version.",
    )
    return True


def _not_dismissed(list_name: str, songs: list[str]) -> list[str]:
    """`songs` minus the ones the owner removed from that list -- the removed set read once per list, not once per song
    (the old Upload list re-read it for every row)."""
    dismissed = load_dismissed(list_name)
    return [s for s in songs if s not in dismissed]


def _stage_to_resume(song_dir: Path) -> str:
    """Where a held song's video is made from, judged by what its SAVED song holds: no chords saved ("detect_chords" -- a
    song held for its timing; a key_decision.json left from an earlier run proves nothing, since a Redo re-times the song
    with no chords before the timing hold stops it: issue #7 review, Render Anyway made a video with no chords); chords
    saved and a video already made ("render", e.g. after Set Key corrected its key); chords saved and a key decision on
    file ("images" -- no chord detection again, and the images stage only buys what is missing)."""
    try:
        chords_saved = bool(load_song(Path(song_dir) / "lyrics_timed.json").chord_track.events)
    except Exception:
        chords_saved = False
    if not chords_saved:
        return "detect_chords"
    if song_video_path(song_dir) is not None:
        return "render"
    return "images" if load_decision(song_dir) is not None else "detect_chords"


def _maybe_upload_to_youtube(work_dir: Path, settings: Settings) -> None:
    """Uploads work_dir's finished video to YouTube if auto-upload is on,
    YouTube is connected, and this song has never been (verifiably) uploaded
    before -- Redo of an already-uploaded song is deliberately skipped to
    avoid duplicate videos piling up (owner's explicit choice), UNLESS the
    saved video_id no longer exists on YouTube (the owner deleted it there
    directly -- real incident 2026-09-10, "Come As You Are" stayed stuck
    showing "already uploaded" forever after its manual deletion, since
    nothing ever re-checked the saved state against YouTube's real state).
    Any failure is caught and logged -- an upload problem must never make an
    otherwise-successful video generation look like it failed. A quota-
    exceeded failure (2026-09-17) additionally records a cooldown
    (Settings.youtube_quota_retry_hours) so every OTHER song doesn't also
    immediately retry into the same wall -- see _retry_pending_uploads_if_due,
    which auto-resumes once that cooldown passes. Also skips outright once
    today's own upload cap is already used up (2026-09-18,
    _uploads_remaining_today) -- the song stays pending and uploads
    automatically on a later day, same as a quota-exceeded skip. A song
    flagged by check_lyric_accuracy() (2026-09-18, Song.lyrics_accuracy_concern
    non-empty) never auto-uploads either -- it still fully rendered, and
    shows up in the "Flagged for Lyrics Review" panel instead."""
    if not settings.youtube_auto_upload:
        return
    credentials = youtube_auth.load_credentials()
    if credentials is None:
        return
    blocked_until = load_quota_blocked_until()
    if blocked_until is not None and datetime.now().astimezone() < blocked_until:
        return  # still cooling down from a prior quota-exceeded error
    if _uploads_remaining_today(settings) <= 0:
        return  # today's upload cap reached -- picked up automatically on a later day
    try:
        if load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern:
            return  # flagged -- see the "Flagged for Lyrics Review" panel
    except Exception:
        pass  # missing/corrupt file must never block an otherwise-normal upload
    if key_state(work_dir) != "confirmed":
        return  # its key is unchecked or waiting for the owner -- never uploaded (see Set Key in the review panel)
    state = load_youtube_state(work_dir)
    if state is None and _state_file_exists(work_dir):
        # Uploaded, but its record is unreadable (empty or cut off): fail closed, like a failed video_exists check --
        # treating it as "never uploaded" put a second copy on the channel (issue #7 review, F106).
        print(
            f"WARNING: {work_dir / STATE_FILENAME} is unreadable, so this song is not uploaded again (that could make a "
            "duplicate video). Fix or delete that file, then upload it from Upload to YouTube.", file=sys.stderr,
        )
        return

    try:
        youtube_client = build("youtube", "v3", credentials=credentials)
        if state is not None:
            try:
                if video_exists(youtube_client, state.video_id):
                    return
            except Exception:
                return  # can't verify right now -- fail closed, don't risk a duplicate
        anthropic_client = anthropic.Anthropic()
        # A never-uploaded song must still be never-uploaded when its turn comes (a manual run may be uploading it right
        # now); a song whose saved video was deleted on YouTube is deliberately uploaded again.
        outcome = _upload_within_todays_cap(
            youtube_client, anthropic_client, work_dir, settings, only_if_not_uploaded=state is None,
        )
        if outcome == "already":
            print(f"{work_dir.name} was already uploaded by another run -- not sent again.")
            return
        if outcome == "deferred":
            return  # today's upload cap was reached meanwhile -- it stays pending for a later day
        print(f"Uploaded to YouTube: {work_dir.name}")
        try:
            organize_video(youtube_client, anthropic_client, work_dir)
        except Exception as e:
            if is_quota_exceeded_error(e):
                save_quota_blocked_until(
                    datetime.now().astimezone() + timedelta(hours=settings.youtube_quota_retry_hours)
                )
                print(
                    f"WARNING: YouTube quota exceeded organizing {work_dir.name}; will retry "
                    f"automatically in {settings.youtube_quota_retry_hours}h.", file=sys.stderr,
                )
            else:
                print(
                    f"WARNING: could not organize {work_dir.name} into playlists/comment: "
                    f"{type(e).__name__}: {e}", file=sys.stderr,
                )
    except IncompleteVideo as e:
        print(f"WARNING: {work_dir.name} was not uploaded -- the video must be made again: {e}", file=sys.stderr)
    except Exception as e:
        if is_quota_exceeded_error(e):
            save_quota_blocked_until(
                datetime.now().astimezone() + timedelta(hours=settings.youtube_quota_retry_hours)
            )
            print(
                f"WARNING: YouTube quota exceeded uploading {work_dir.name}; will retry "
                f"automatically in {settings.youtube_quota_retry_hours}h.", file=sys.stderr,
            )
        else:
            print(f"WARNING: YouTube upload failed for {work_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)


def _record_owner_verification(work_dir: Path) -> None:
    """Records the owner's approval of this version of the song, with the automatic score as it stands now."""
    report = check_saved_song(work_dir)
    mark_verified(
        work_dir, automatic_share=None if report is None else report.share, needed=None if report is None else report.needed,
    )


def _retry_pending_uploads(
    work_root: Path, settings: Settings, slugs: list[str] | None = None, force: bool = False,
    *, only_if_not_uploaded: bool = True,
) -> dict:
    """Uploads every song named in `slugs` (default: every list_pending_uploads()
    result) via the same schedule_upload() code path as auto-upload and the
    manual Upload button -- backs the GUI's retry-upload controls for a song
    that failed to upload and isn't self._last_work_dir. One song's failure
    is logged and skipped, never aborting the rest -- EXCEPT a quota-exceeded
    failure (2026-09-17), which stops the loop immediately (every remaining
    song would fail identically right now) and records a cooldown
    (Settings.youtube_quota_retry_hours) instead of grinding through the rest
    reporting the same root cause fifty times over. Also stops (without
    marking anything "failed") once today's own upload cap is reached
    (2026-09-18, _uploads_remaining_today) -- untried songs land in
    results["deferred"] and simply stay pending for a later day; this is how
    "Select All" + Upload Selected on a big pending list is safe to click
    without blowing through a day's quota in one run. `force=True` (manual
    retry-upload triggers only, after the owner explicitly confirms past a
    quota-cooldown warning -- see gui.py's _confirm_quota_override_if_blocked)
    skips the upfront cooldown check below; it never bypasses the per-song
    daily upload cap, which has no override by owner request (2026-09-18).

    `only_if_not_uploaded` (default True: every pending-list upload) skips a song that already has a youtube_state.json
    -- readable or not -- when its turn comes, e.g. because another run uploaded it meanwhile; it lands in
    results["already_uploaded"] and is never sent twice (issue #7 review). Only the single-song Upload button, after the
    owner confirmed "upload again and create a duplicate", passes False."""
    credentials = youtube_auth.load_credentials()
    if credentials is None:
        raise RuntimeError("Not connected to YouTube.")
    blocked_until = load_quota_blocked_until()
    if not force and blocked_until is not None and datetime.now().astimezone() < blocked_until:
        raise RuntimeError(
            f"YouTube's daily quota is exceeded; will retry automatically after "
            f"{blocked_until.astimezone():%Y-%m-%d %H:%M}."
        )
    youtube_client = build("youtube", "v3", credentials=credentials)
    anthropic_client = anthropic.Anthropic()

    if slugs is None:
        slugs = list_pending_uploads(work_root)

    results: dict = {"succeeded": [], "failed": [], "deferred": []}
    for slug in slugs:
        try:
            outcome = _upload_within_todays_cap(
                youtube_client, anthropic_client, work_root / slug, settings, only_if_not_uploaded,
            )
        except IncompleteVideo as e:
            # Not an upload failure: the video itself must be made again (cut short, no audio, missing). Nothing was sent
            # or paid for; the auto-retry backs off from it like any failed song.
            results["failed"].append((slug, f"the video must be made again (Redo, or Rebuild EASY version): {e}"))
            continue
        except Exception as e:
            results["failed"].append((slug, f"{type(e).__name__}: {e}"))
            if is_quota_exceeded_error(e):
                save_quota_blocked_until(
                    datetime.now().astimezone() + timedelta(hours=settings.youtube_quota_retry_hours)
                )
                break
            continue
        if outcome == "already":
            results.setdefault("already_uploaded", []).append(slug)
            continue
        if outcome == "deferred":
            results["deferred"].append(slug)
            if needs_review(work_root / slug):
                # Only Upload Anyway can send a held song here, so this click IS the owner's approval: remember it, or the
                # song would sit in Flagged for Lyrics Review and never upload (the app used to promise it would).
                _record_owner_verification(work_root / slug)
                results.setdefault("approved", []).append(slug)
            continue
        results["succeeded"].append(slug)
        try:
            organize_video(youtube_client, anthropic_client, work_root / slug)
        except Exception as e:
            print(
                f"WARNING: could not organize {slug} into playlists/comment: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            if is_quota_exceeded_error(e):
                save_quota_blocked_until(
                    datetime.now().astimezone() + timedelta(hours=settings.youtube_quota_retry_hours)
                )
                break
    return results


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE_PATH = PROJECT_ROOT / "VERSION"

# Height (px) of each scrollable song-list panel (Redo / Upload to YouTube /
# Pending Uploads) -- replaces the old CTkComboBox dropdown (a native OS menu
# that could run off-screen once a song list got long enough; owner request,
# 2026-09-15). First tried wrapping all three lists in an outer
# CTkScrollableFrame so the whole page would scroll -- reverted the same day:
# nesting a CTkScrollableFrame inside another one is unreliable in this
# customtkinter version (its scrollbar/mouse-wheel handling is keyed off a
# single bind_all per instance, and the outer one never actually scrolled for
# the owner, trapping everything below the Redo list). Each section now
# starts CLOSED (see _make_collapsible_section) and is opened on demand, so
# there's no more fight for vertical space -- ~15 rows visible per list,
# scrolling (via that list's own, proven-working scrollbar) for the rest.
SONG_LIST_HEIGHT = 420

_CHECKED, _UNCHECKED = "☑", "☐"
_SONG_LIST_STYLE = "SongList.Treeview"


def _theme_color(widget_name: str, key: str, fallback: str) -> str:
    """One of CustomTkinter's own theme colors for the current appearance mode (the theme stores [light, dark] pairs)."""
    try:
        value = ctk.ThemeManager.theme[widget_name][key]
    except (KeyError, TypeError):
        return fallback
    if isinstance(value, (list, tuple)):
        value = value[1] if ctk.get_appearance_mode() == "Dark" else value[0]
    return value if isinstance(value, str) and value != "transparent" else fallback


def _song_list_font(widget) -> tkfont.Font:
    """Sets up (once per Tk window) the ttk style every song list draws with -- colored to match CustomTkinter's theme --
    and returns its font. Kept on the root window so it lives as long as the lists do (a Tk font is deleted with its
    last Python reference)."""
    root = widget.winfo_toplevel()
    cached = getattr(root, "_song_list_font", None)
    if cached is not None:
        return cached
    style = ttk.Style(widget)
    if style.theme_use() not in ("clam", "alt", "default"):
        style.theme_use("clam")  # Windows' native "vista" theme ignores a Treeview's background colors
    try:
        scaling = ctk.ScalingTracker.get_widget_scaling(widget)
    except Exception:
        scaling = 1.0
    try:
        family = ctk.CTkFont().cget("family")
    except Exception:
        family = "TkDefaultFont"
    font = tkfont.Font(root=widget, family=family, size=-max(10, round(13 * scaling)))
    background = _theme_color("CTkTextbox", "fg_color", "#1D1E1E")
    foreground = _theme_color("CTkLabel", "text_color", "#DCE4EE")
    selected = _theme_color("CTkButton", "fg_color", "#1F6AA5")
    style.configure(
        _SONG_LIST_STYLE, background=background, fieldbackground=background, foreground=foreground, font=font,
        rowheight=font.metrics("linespace") + round(8 * scaling), borderwidth=0, relief="flat",
    )
    style.map(
        _SONG_LIST_STYLE, background=[("selected", selected)], foreground=[("selected", "#FFFFFF")],
    )
    style.layout(_SONG_LIST_STYLE, [("Treeview.treearea", {"sticky": "nswe"})])  # no border/focus ring around the list
    root._song_list_font = font
    return font


class _SongListView:
    """One scrollable song list drawn by a single ttk.Treeview (issue #7). The old lists built a CTkFrame, a radio or check
    box and two buttons PER SONG -- about 11 Tk windows a row, so ~5,000 for the owner's ~466 songs: tens of seconds to
    open a list on his slow box, the same again on every refresh, seconds more to close the window, and on Windows enough
    native windows to hit the 10,000-handle limit and crash. A Treeview draws every row itself: a constant handful of
    widgets whatever the song count, rows added or dropped in milliseconds.

    Single-select lists (Redo, Upload) mirror the highlighted row into a StringVar. A checklist (`checklist=True`: Pending
    YouTube Uploads, EASY CHORD versions) adds a ☑/☐ column -- clicking the box (or Space) ticks it, and every song
    starts ticked by default exactly like the old checkboxes -- while the highlighted row is what the list's own Watch and
    Remove buttons act on. `extra_column` adds a second, narrower text column (the Flagged list's short reason).
    Double-clicking a song runs `on_activate` (Watch)."""

    def __init__(
        self, parent, *, height_px: int = SONG_LIST_HEIGHT, checklist: bool = False, variable: tk.StringVar | None = None,
        on_select=None, on_activate=None, extra_column: bool = False,
    ):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        font = _song_list_font(self.frame)
        row_px = font.metrics("linespace") + 8
        self.checklist = checklist
        self.variable = variable
        self._on_select = on_select
        self._on_activate = on_activate
        self._checked: set[str] = set()
        self._order: list[str] = []
        self._labels: dict[str, str] = {}
        self._extras: dict[str, str] = {}
        self.loaded = False
        self._has_extra = extra_column
        self._count_refresh_queued = False
        self._note = ""
        columns = (("check",) if checklist else ()) + ("name",) + (("extra",) if extra_column else ())
        body = ctk.CTkFrame(self.frame, fg_color="transparent")
        body.pack(fill="x")
        self.tree = ttk.Treeview(
            body, columns=columns, show="", selectmode="browse", style=_SONG_LIST_STYLE,
            height=max(5, height_px // row_px),
        )
        if checklist:
            self.tree.column("check", width=font.measure(_CHECKED) + 18, minwidth=24, stretch=False, anchor="center")
        self.tree.column("name", width=220, minwidth=120, stretch=True, anchor="w")
        if extra_column:
            self.tree.column("extra", width=font.measure("lyrics/timing, on YouTube, EASY") + 12, minwidth=80,
                             stretch=False, anchor="w")
        scrollbar = ctk.CTkScrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="x", expand=True)
        self.status = ctk.CTkLabel(self.frame, text="", anchor="w", text_color="gray60")
        self.status.pack(fill="x", padx=4)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-Button-1>", self._double_clicked)
        if checklist:
            self.tree.bind("<Button-1>", self._clicked, add="+")
            self.tree.bind("<space>", self._space_pressed)
        if variable is not None:
            variable.trace_add("write", lambda *_: self._follow_variable())

    # --- contents -------------------------------------------------------------------------------------------------
    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def slugs(self) -> list[str]:
        return list(self._order)

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def set_songs(self, songs: list[str], labels: dict[str, str] | None = None, extras: dict[str, str] | None = None,
                  default_checked: bool = True, note: str = "") -> None:
        """Shows exactly `songs`, in order. A song already listed keeps its tick and highlight (a refresh after a Batch item
        never undoes the owner's own unticking); a new song starts ticked when `default_checked`."""
        labels = labels or {}
        extras = extras or {}
        previous = set(self._order)
        highlighted = self.selected()
        self._checked = {s for s in self._checked if s in songs} | {s for s in songs if s not in previous and default_checked}
        top = self.tree.yview()[0] if self._order else 0.0
        if songs != self._order:
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            for song in songs:
                self.tree.insert("", "end", iid=song, values=self._row_values(song, labels.get(song, song), extras.get(song, "")))
        else:
            for song in songs:
                self.tree.item(song, values=self._row_values(song, labels.get(song, song), extras.get(song, "")))
        self._order = list(songs)
        self._labels = {s: labels.get(s, s) for s in songs}
        self._extras = {s: extras.get(s, "") for s in songs}
        self.loaded = True
        if highlighted in previous and highlighted in songs:
            self.tree.selection_set(highlighted)
        elif self.variable is not None:
            self._follow_variable()
        if songs:
            self.tree.yview_moveto(top)
        self._note = note
        self._refresh_status()

    def _refresh_status(self) -> None:
        self._count_refresh_queued = False
        count = len(self._order)
        text = f"{count} song{'s' if count != 1 else ''}" if count else "(none)"
        if self.checklist and count:
            text += f", {len(self._checked)} ticked"
        note = getattr(self, "_note", "")
        self.set_status(f"{text}{'   ' + note if note else ''}")

    def _row_values(self, song: str, label: str, extra: str) -> tuple:
        values = (label,) + ((extra,) if self._has_extra else ())
        return ((_CHECKED if song in self._checked else _UNCHECKED),) + values if self.checklist else values

    def remove(self, song: str) -> None:
        """Drops one row (Remove) -- no rescan, no rebuild of the rest."""
        if song not in self._order:
            return
        was_selected = self.selected() == song
        self._order.remove(song)
        self._checked.discard(song)
        self.tree.delete(song)
        if was_selected:
            if self.variable is not None:
                self.variable.set("")
            if self._on_select is not None:
                self._on_select(None)
        self._refresh_status()

    # --- highlight (single selection) -----------------------------------------------------------------------------
    def selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def select(self, song: str) -> None:
        if song in self._order:
            self.tree.selection_set(song)
            self.tree.see(song)

    def _selection_changed(self, _event=None) -> None:
        song = self.selected()
        if self.variable is not None and song is not None and self.variable.get() != song:
            self.variable.set(song)
        if self._on_select is not None:
            self._on_select(song)

    def _follow_variable(self) -> None:
        """One trace per list (not one per row, as the old radio buttons had): a StringVar set elsewhere (e.g. Redo from
        the Flagged panel) highlights that row."""
        wanted = self.variable.get()
        if wanted in self._order:
            if self.selected() != wanted:
                self.select(wanted)
        elif self.selected() is not None:
            self.tree.selection_remove(*self.tree.selection())

    def _double_clicked(self, event) -> str | None:
        song = self.tree.identify_row(event.y)
        if song and self._on_activate is not None and (not self.checklist or self.tree.identify_column(event.x) != "#1"):
            self._on_activate(song)
            return "break"
        return None

    # --- ticks (checklists) ---------------------------------------------------------------------------------------
    def is_checked(self, song: str) -> bool:
        return song in self._checked

    def set_checked(self, song: str, value: bool) -> None:
        if song not in self._order:
            return
        if value:
            self._checked.add(song)
        else:
            self._checked.discard(song)
        self.tree.set(song, "check", _CHECKED if value else _UNCHECKED)
        if not self._count_refresh_queued:  # once per burst: Select All ticks every row in one go
            self._count_refresh_queued = True
            self.tree.after_idle(self._refresh_status)

    def checked(self) -> list[str]:
        return [s for s in self._order if s in self._checked]

    def _clicked(self, event) -> None:
        if self.tree.identify_column(event.x) != "#1":
            return
        song = self.tree.identify_row(event.y)
        if song:
            self.set_checked(song, song not in self._checked)

    def _space_pressed(self, _event=None) -> str:
        song = self.tree.focus() or self.selected()
        if song:
            self.set_checked(song, song not in self._checked)
        return "break"


class _TickFlag:
    """What `_pending_upload_vars` / `_easy_chord_backfill_vars` hold per song now: the same get()/set() a BooleanVar had,
    backed by the list's ☑ column, so Upload Selected / Generate Selected / Select All read and write exactly as before."""

    def __init__(self, view: _SongListView, song: str):
        self._view, self._song = view, song

    def get(self) -> bool:
        return self._view.is_checked(self._song)

    def set(self, value: bool) -> None:
        self._view.set_checked(self._song, bool(value))


def _one_line(text: str, limit: int = 90) -> str:
    """A draft's first words for its list row: whitespace collapsed, cut at `limit` characters."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _draft_box(parent, text: str = "", height: int = 4) -> tk.Text:
    """An editable text box colored like CTkTextbox. A plain tk.Text: a CTkTextbox's own scrollbars force a layout pass over
    the whole window every time one is drawn (~0.1 s each here; issue #7)."""
    box = tk.Text(
        parent, height=height, wrap="word", relief="flat", borderwidth=0, highlightthickness=1,
        background=_theme_color("CTkTextbox", "fg_color", "#1D1E1E"),
        foreground=_theme_color("CTkTextbox", "text_color", "#DCE4EE"),
        insertbackground=_theme_color("CTkTextbox", "text_color", "#DCE4EE"),
        highlightbackground=_theme_color("CTkTextbox", "border_color", "#565B5E"),
        highlightcolor=_theme_color("CTkButton", "fg_color", "#1F6AA5"),
        font=_song_list_font(parent), padx=6, pady=4, undo=True,
    )
    if text:
        box.insert("1.0", text)
    return box


class _DraftQueueView:
    """Drafts waiting for the owner (YouTube comment replies, engagement comments): ONE list of them plus ONE editor with
    Approve / Dismiss for the highlighted draft (issue #7). The old panels built a frame, label, text box and two buttons
    per draft -- at launch, for the replies -- and rebuilt all of them on every refresh, which both cost seconds and reset
    whatever reply the owner was in the middle of editing. Here a refresh only adds or drops list rows, and every edit is
    kept per draft id (switching to another draft and back, a new comment arriving, approving a different one)."""

    def __init__(self, parent, *, on_approve, on_dismiss, height_px: int = 140, empty_text: str = "(none)"):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        font = _song_list_font(self.frame)
        row_px = font.metrics("linespace") + 8
        self._on_approve, self._on_dismiss = on_approve, on_dismiss
        self._empty_text = empty_text
        self._items: dict[str, object] = {}
        self._drafts: dict[str, str] = {}
        self._headings: dict[str, str] = {}
        self._order: list[str] = []
        self._edits: dict[str, str] = {}
        self._shown: str | None = None
        self._busy: set[str] = set()      # drafts being posted: Approve / Dismiss stay off for them (see set_busy)
        body = ctk.CTkFrame(self.frame, fg_color="transparent")
        body.pack(fill="x")
        self.tree = ttk.Treeview(
            body, columns=("who", "text"), show="", selectmode="browse", style=_SONG_LIST_STYLE,
            height=max(3, height_px // row_px),
        )
        self.tree.column("who", width=150, minwidth=80, stretch=False, anchor="w")
        self.tree.column("text", width=260, minwidth=120, stretch=True, anchor="w")
        scrollbar = ctk.CTkScrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="x", expand=True)
        self.status = ctk.CTkLabel(self.frame, text="", anchor="w", text_color="gray60")
        self.status.pack(fill="x", padx=4)
        self.heading = ctk.CTkLabel(self.frame, text="", anchor="w", wraplength=420, justify="left")
        self.heading.pack(fill="x", padx=4, pady=(2, 2))
        self.editor = _draft_box(self.frame)
        self.editor.pack(fill="x", padx=4, pady=(0, 4))
        buttons = ctk.CTkFrame(self.frame, fg_color="transparent")
        buttons.pack(fill="x", padx=4, pady=(0, 4))
        self.approve_button = ctk.CTkButton(buttons, text="Approve", width=80, state="disabled", command=self._approve)
        self.approve_button.pack(side="left", padx=(0, 6))
        self.dismiss_button = ctk.CTkButton(
            buttons, text="Dismiss", width=80, fg_color="gray30", hover_color="gray20", state="disabled",
            command=self._dismiss,
        )
        self.dismiss_button.pack(side="left")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show(self.selected()))
        self._show(None)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def ids(self) -> list[str]:
        return list(self._order)

    def selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def select(self, item_id: str) -> None:
        if item_id in self._items:
            self.tree.selection_set(item_id)
            self.tree.see(item_id)
            self._show(item_id)

    def set_items(self, rows: list[tuple]) -> None:
        """rows: (id, item, who, preview, heading, draft) per waiting draft, in order. Rows already listed keep their place
        in the owner's attention: the highlighted one stays highlighted and every edit made so far is kept."""
        self._remember_edit()
        ids = [row[0] for row in rows]
        highlighted = self.selected()
        if ids != self._order:
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            for item_id, _item, who, preview, _heading, _draft in rows:
                self.tree.insert("", "end", iid=item_id, values=(who, preview))
        self._order = ids
        self._items = {row[0]: row[1] for row in rows}
        self._headings = {row[0]: row[4] for row in rows}
        self._drafts = {row[0]: row[5] for row in rows}
        self._edits = {k: v for k, v in self._edits.items() if k in self._items}
        self._busy &= set(self._items)
        self.status.configure(text=f"{len(ids)} waiting" if ids else self._empty_text)
        if highlighted in self._items:
            if self.selected() != highlighted:
                self.tree.selection_set(highlighted)
            if self._shown != highlighted:
                self._show(highlighted)
        else:
            self._show(None)

    def _remember_edit(self) -> None:
        if self._shown is not None and self._shown in self._items:
            self._edits[self._shown] = self.editor.get("1.0", "end-1c")

    def _show(self, item_id: str | None) -> None:
        if item_id == self._shown and item_id is not None:
            return
        self._remember_edit()
        self._shown = item_id if item_id in self._items else None
        self.editor.delete("1.0", "end")
        if self._shown is None:
            self.heading.configure(text="Click a draft above to read and edit it." if self._order else "")
            state = "disabled"
        else:
            self.heading.configure(text=self._headings.get(self._shown, ""))
            self.editor.insert("1.0", self._edits.get(self._shown, self._drafts.get(self._shown, "")))
            state = "disabled" if self._shown in self._busy else "normal"
        self.approve_button.configure(state=state)
        self.dismiss_button.configure(state=state)

    def set_busy(self, item_id: str, busy: bool) -> None:
        """Marks a draft as being posted (issue #7 review, F038/F039/F115): its Approve and Dismiss are off until the post
        ends, so a second click during a slow post can never send the same public text twice."""
        if busy:
            self._busy.add(item_id)
        else:
            self._busy.discard(item_id)
        if self._shown == item_id:
            state = "disabled" if busy else "normal"
            self.approve_button.configure(state=state)
            self.dismiss_button.configure(state=state)

    def current(self):
        return self._items.get(self._shown) if self._shown is not None else None

    def _approve(self) -> None:
        item = self.current()
        if item is not None:
            self._remember_edit()
            self._on_approve(item, self.editor)

    def _dismiss(self) -> None:
        item = self.current()
        if item is not None:
            self._on_dismiss(item)


def _open_with_default_app(path: Path) -> None:
    """Hands a rendered video off to whatever the OS already uses to play
    mp4s -- no in-app player, just a preview-before-uploading convenience
    (owner request, 2026-09-15)."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _default_work_dir_from_audio(audio_path: str) -> str:
    """Fallback work directory derived straight from the audio file's own
    name, used when Generate is clicked before (or despite) the GUI's own
    background title-identification (_on_audio_selected/_identify_worker)
    finishing -- that lookup can take a moment (it can fall back to a real
    network call when tags are missing) or fail silently by design, and
    neither should ever block Generate: run_pipeline's own identify stage
    re-resolves the real title from scratch regardless of what this GUI-side
    preview did or didn't manage first."""
    return str(PROJECT_ROOT / "work" / _slugify(Path(audio_path).stem))

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class _QueueWriter:
    """File-like object that pushes writes into a queue instead of a real
    stream -- lets stdout/stderr from the worker thread (moviepy's own
    progress output, and Demucs's, which separate._run_demucs relays through
    sys.stdout precisely so it lands here) reach the GUI's log widget
    instead of the terminal."""

    def __init__(self, q: "queue.Queue"):
        self._queue = q

    def write(self, text: str) -> int:
        if text:
            self._queue.put(("log", text))
        return len(text)

    def flush(self) -> None:
        pass


class LyricVideoGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        current_version = read_local_version(str(_VERSION_FILE_PATH)) or "v0.0.0"
        root.title(f"PlayAlongVideoProduction {current_version}")
        root.geometry("1600x1000")     # owner, 2026-09-21: the size he had resized it to (1552x1000), a little wider
        root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        self._queue: "queue.Queue" = queue.Queue()
        self._update_queue: "queue.Queue" = queue.Queue()
        self._current_version = current_version
        self._available_update: dict | None = None
        self._running = False
        self._identified_artist = ""  # set by _apply_identified_title, used for the YouTube title preview
        self._auto_filled_title = ""  # the title identify filled in (not typed) -- a new audio pick may replace it
        self._update_dialog_window = None
        self._update_apply_in_progress = False  # an Apply Update is downloading/installing/copying
        self._posting_keys: set[str] = set()     # drafts being posted / posted (see _claim_posting)
        self._auto_retry_not_before: dict[str, datetime] = {}   # see _note_auto_retry_results
        self._log_pending = ""
        self._log_has_uncommitted_line = False
        self._suppress_settings_save = True  # True while load_from() is populating widgets on launch
        self._closing = False
        # Song-list scans run off the Tk thread (issue #7) -- see _start_list_load.
        self._list_lock = threading.Lock()
        self._list_jobs: deque = deque()
        self._list_worker_running = False
        self._list_worker_thread: threading.Thread | None = None
        self._list_gen: dict[str, int] = {}
        self._list_pending: dict[str, int] = {}
        self._list_results: "queue.Queue" = queue.Queue()
        self._list_poll_scheduled = False
        self._song_views: dict[str, _SongListView] = {}
        # Settings popup: built once, hidden/shown after that (see _open_settings_window).
        self._settings_window_shown = False
        self._preview_after_id = None
        self._pass_mark_at_open: int | None = None

        self.settings = Settings.load()
        self._use_settings_pass_mark()

        self.title_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.work_dir_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.redo_song_var = tk.StringVar()
        self.redo_new_images_var = tk.BooleanVar(value=False)
        self.redo_easy_chord_var = tk.BooleanVar(value=False)
        self.retry_upload_song_var = tk.StringVar()
        self.batch_folder_var = tk.StringVar(value=load_last_batch_folder())
        self._batch_items: list = []  # list[BatchItem] once resolved
        self._batch_index = 0
        self._batch_results = {"succeeded": [], "skipped_already_done": [], "failed": []}
        self._last_work_dir: Path | None = None

        self.title_var.trace_add("write", self._on_title_changed)

        self._build_widgets()
        self._suppress_settings_save = False
        self._check_api_keys()
        self._start_update_check()
        self._refresh_youtube_status()

    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        self.update_banner_var = tk.StringVar()
        self.update_banner = ctk.CTkLabel(
            self.root,
            textvariable=self.update_banner_var,
            text_color="#4da3ff",
            cursor="hand2",
            anchor="w",
        )
        self.update_banner.bind("<Button-1>", self._on_update_banner_clicked)

        # Two-column body: left = single-song form + Redo, right = SettingsPanel.
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, **pad)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)
        # `body` (not `left`) is the update banner's `before=` anchor: pack()'s
        # `before=` requires a widget managed by pack() in the SAME master as
        # the widget being packed. `left` is grid()-managed inside `body`, not
        # pack()-managed inside `self.root` like the banner is -- using it
        # here always raised TclError: window isn't packed, silently (a real
        # bug from the CustomTkinter rebuild, found live 2026-09-09: the
        # banner never once appeared across multiple relaunches with a real
        # update available, because Tkinter callback exceptions print to a
        # log the desktop-launched app's owner never sees, not to any visible
        # UI). `body` itself IS pack()-managed directly under `self.root`,
        # exactly like the banner, so `before=body` inserts the banner right
        # above it as intended.
        self.top_frame = body

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        form = ctk.CTkFrame(left, fg_color="transparent")
        form.pack(fill="x", padx=10, pady=10)
        form.grid_columnconfigure(1, weight=1)

        self._add_row(form, 0, "Song title (auto-filled, editable):", self.title_var)
        self._add_file_row(
            form, 1, "Audio file (mp3/wav):", self.audio_var,
            [("Audio files", "*.mp3 *.wav *.m4a *.flac"), ("All files", "*.*")],
            on_selected=self._on_audio_selected,
        )
        self._add_row(form, 2, "Work directory:", self.work_dir_var)

        note = ctk.CTkLabel(
            form,
            text="Title, artist, and lyrics are identified automatically from the audio\n"
            "file's tags and online lookup -- edit the title above if it's wrong. Chords\n"
            "are detected directly from the audio; no tab or chord sheet is needed.",
            text_color="gray60",
            justify="left",
            anchor="w",
        )
        note.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))

        self.youtube_title_preview_var = tk.StringVar()
        ctk.CTkLabel(
            form, textvariable=self.youtube_title_preview_var, text_color="gray60",
            justify="left", anchor="w",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 8))

        button_row = ctk.CTkFrame(form, fg_color="transparent")
        button_row.grid(row=5, column=0, columnspan=3, pady=8)
        self.generate_button = ctk.CTkButton(button_row, text="Generate Video", command=self._on_generate)
        self.generate_button.pack(side="left", padx=4)
        self.new_song_button = ctk.CTkButton(
            button_row, text="New Song", command=self._on_new_song, fg_color="gray30", hover_color="gray20",
        )
        self.new_song_button.pack(side="left", padx=4)

        status_frame = ctk.CTkFrame(left, fg_color="transparent")
        status_frame.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(status_frame, text="Status:").pack(side="left")
        ctk.CTkLabel(status_frame, textvariable=self.status_var, text_color="#3ecf8e").pack(
            side="left", padx=6
        )

        self.progress_bar = ctk.CTkProgressBar(left)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 10))

        # The song lists (issue #7): each is ONE Treeview (a constant handful of widgets whatever the song count) whose rows
        # are scanned from work/ on a background thread the first time the section is opened -- never at launch -- and
        # re-scanned (again off the Tk thread, old rows staying up meanwhile) when invalidate() says the data changed.
        # One Watch / Remove pair per list acts on the highlighted row (double-click also Watches).
        redo_content, self._invalidate_redo_list = self._make_collapsible_section(
            left, "Redo an Existing Song", on_first_expand=lambda: self._refresh_song_list("redo"),
        )
        self._make_song_view(redo_content, "redo", variable=self.redo_song_var)
        redo_controls = ctk.CTkFrame(redo_content, fg_color="transparent")
        redo_controls.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkCheckBox(
            redo_controls, text="Generate new images", variable=self.redo_new_images_var,
        ).pack(side="left", padx=8)
        ctk.CTkCheckBox(
            redo_controls, text="Easy Chords (capo)", variable=self.redo_easy_chord_var,
        ).pack(side="left", padx=8)
        self.redo_button = ctk.CTkButton(redo_controls, text="Redo", command=self._on_redo, width=80)
        self.redo_button.pack(side="left", padx=8)
        self._add_watch_remove_buttons(redo_controls, "redo")

        upload_content, self._invalidate_upload_list = self._make_collapsible_section(
            left, "Upload to YouTube", on_first_expand=lambda: self._refresh_song_list("upload"),
        )
        self._make_song_view(upload_content, "upload", variable=self.retry_upload_song_var)
        retry_upload_controls = ctk.CTkFrame(upload_content, fg_color="transparent")
        retry_upload_controls.pack(fill="x", padx=8, pady=(0, 8))
        self.retry_upload_button = ctk.CTkButton(
            retry_upload_controls, text="Upload", command=self._on_retry_upload, width=80,
        )
        self.retry_upload_button.pack(side="left", padx=8)
        self._add_watch_remove_buttons(retry_upload_controls, "upload")

        def _add_pending_select_all(header: ctk.CTkFrame) -> None:
            self.pending_select_all_var = tk.BooleanVar(value=True)
            ctk.CTkCheckBox(
                header, text="Select All", variable=self.pending_select_all_var,
                command=self._on_toggle_pending_select_all,
            ).pack(side="right", padx=8)

        self._pending_upload_vars: dict[str, _TickFlag] = {}
        pending_content, self._invalidate_pending_list = self._make_collapsible_section(
            left, "Pending YouTube Uploads", header_extra=_add_pending_select_all,
            on_first_expand=self._refresh_pending_uploads_list,
        )
        self._make_song_view(pending_content, "pending", checklist=True)
        pending_controls = ctk.CTkFrame(pending_content, fg_color="transparent")
        pending_controls.pack(fill="x", padx=8, pady=(0, 8))
        self.upload_selected_button = ctk.CTkButton(
            pending_controls, text="Upload Selected", command=self._on_upload_selected_pending, width=140,
        )
        self.upload_selected_button.pack(side="left", padx=8)
        self._add_watch_remove_buttons(pending_controls, "pending")

        def _add_easy_chord_select_all(header: ctk.CTkFrame) -> None:
            self.easy_chord_select_all_var = tk.BooleanVar(value=True)
            ctk.CTkCheckBox(
                header, text="Select All", variable=self.easy_chord_select_all_var,
                command=self._on_toggle_easy_chord_backfill_select_all,
            ).pack(side="right", padx=8)

        self._easy_chord_backfill_vars: dict[str, _TickFlag] = {}
        easy_chord_content, self._invalidate_easy_chord_backfill_list = self._make_collapsible_section(
            left, "Generate EASY CHORD Versions (existing songs)", header_extra=_add_easy_chord_select_all,
            on_first_expand=self._refresh_easy_chord_backfill_list,
        )
        self._make_song_view(easy_chord_content, "easy_chord_backfill", checklist=True)
        easy_chord_controls = ctk.CTkFrame(easy_chord_content, fg_color="transparent")
        easy_chord_controls.pack(fill="x", padx=8, pady=(0, 8))
        self.generate_easy_chord_backfill_button = ctk.CTkButton(
            easy_chord_controls, text="Generate Selected", command=self._on_generate_selected_easy_chord_backfill,
            width=140,
        )
        self.generate_easy_chord_backfill_button.pack(side="left", padx=8)
        self._add_watch_remove_buttons(easy_chord_controls, "easy_chord_backfill")

        batch_frame = ctk.CTkFrame(left)
        batch_frame.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(batch_frame, text="Batch: Process a Folder", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        batch_controls = ctk.CTkFrame(batch_frame, fg_color="transparent")
        batch_controls.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkEntry(batch_controls, textvariable=self.batch_folder_var, width=340, state="readonly").pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(batch_controls, text="Browse Folder...", command=self._on_browse_batch_folder, width=110).pack(
            side="left", padx=8
        )
        self.batch_button = ctk.CTkButton(batch_controls, text="Start Batch", command=self._on_start_batch, width=90)
        self.batch_button.pack(side="left", padx=8)

        self.log_widget = ctk.CTkTextbox(left, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew")

        # Settings moved into its own popup window (owner feedback,
        # 2026-09-11: the embedded tab felt cramped/"ugly") -- this column is
        # now just the YouTube panel plus the button that opens it. The panel
        # and its live preview are built the first time the window opens and
        # then only hidden/shown again (see _open_settings_window; issue #7).
        self._settings_window: ctk.CTkToplevel | None = None
        settings_button_row = ctk.CTkFrame(right, fg_color="transparent")
        settings_button_row.pack(fill="x", padx=4, pady=(4, 0))
        ctk.CTkButton(settings_button_row, text="⚙ Settings", command=self._open_settings_window).pack(
            side="left"
        )

        youtube_connect_frame = ctk.CTkFrame(right, fg_color="transparent")
        youtube_connect_frame.pack(fill="x", padx=4, pady=(10, 0))
        self.youtube_status_var = tk.StringVar(value="YouTube: not connected")
        ctk.CTkLabel(youtube_connect_frame, textvariable=self.youtube_status_var, anchor="w").pack(side="left")
        ctk.CTkButton(
            youtube_connect_frame, text="Connect to YouTube", command=self._on_connect_youtube, width=160,
        ).pack(side="right")

        self._build_youtube_panel(right)
        self._build_pending_comments_panel(right)
        self._build_flagged_songs_panel(right)

    def _open_settings_window(self) -> None:
        if self._settings_window is not None and self._settings_window_shown:
            # Already open -- bring it to front rather than building a second
            # SettingsPanel bound to the same Settings object (which would
            # double-fire on_change and diverge from the real dirty baseline).
            self._settings_window.lift()
            self._settings_window.focus_force()
            return
        from .settings_panel import SettingsPanel          # only the popup needs these (issue #7: faster launch)
        from .settings_preview import SettingsPreviewFrame

        # Built ONCE, then hidden and shown again (issue #7): its ~500 widgets took a second or more to build on every
        # open here, several on the owner's box. Closing reverts unsaved changes first (see _on_close), so a reopened
        # window always shows exactly the settings in use.
        if self._settings_window is not None:
            self._show_settings_dialog(self._settings_window)
            return
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Settings")
        self._settings_window = dialog
        self._show_settings_dialog(dialog)

        # Suppressed while THIS popup's own SettingsPanel populates itself
        # (SettingsPanel.__init__'s trailing load_from() fires on_change()
        # once before this method's own `self.settings_panel = ...`
        # assignment below has completed) -- the same guard startup already
        # uses for the identical reason, just re-armed here since startup
        # only resets it once. Real bug found live, 2026-09-11: without
        # this, on_change -> self.settings_panel.collect() raised
        # AttributeError (attribute not yet assigned) partway through
        # SettingsPanel's own __init__, silently swallowed by Tkinter with
        # no visible error on a desktop-launched app -- aborting
        # construction before .pack() ever ran, leaving the whole panel
        # invisible below the live preview.
        self._suppress_settings_save = True
        self.settings_preview = SettingsPreviewFrame(dialog, self.settings)
        self.settings_preview.pack(fill="x", padx=6, pady=(6, 0))
        self.settings_panel = SettingsPanel(dialog, self.settings, on_change=self._on_settings_changed)
        self.settings_panel.pack(fill="both", expand=True, padx=6, pady=6)
        self._suppress_settings_save = False

        def _on_close() -> None:
            if self.settings_panel.has_unsaved_changes():
                if not self._ask_over(
                    dialog, "Discard changes", "You have unsaved settings changes. Discard them and close?",
                ):
                    return
                # Reverts the widgets (and, via on_change, self.settings
                # itself) back to what's actually on disk -- calling the
                # panel's own Discard button would pop a SECOND confirmation
                # on top of this one, so this reuses its underlying action
                # directly instead.
                self.settings_panel.load_from(self.settings_panel._baseline)
            self._hide_settings_dialog(dialog)

        dialog.protocol("WM_DELETE_WINDOW", _on_close)

    def _show_settings_dialog(self, dialog) -> None:
        dialog_w, dialog_h = 640, 780
        self.root.update_idletasks()
        # Same transient+grab_set+lift/focus_force+brief-topmost treatment as
        # the Update Available dialog -- a plain Toplevel can open silently
        # behind the main window on some Linux window managers (Cinnamon
        # included; a documented recurring bug class on this project).
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(x, 0)}+{max(y, 0)}")
        dialog.deiconify()
        dialog.transient(self.root)
        self._settings_window_shown = True
        self._grab_settings_dialog(dialog)
        dialog.lift()
        dialog.focus_force()
        dialog.attributes("-topmost", True)
        dialog.after(300, lambda: dialog.winfo_exists() and dialog.attributes("-topmost", False))
        self._pass_mark_at_open = self.settings.timing_pass_percent

    def _grab_settings_dialog(self, dialog, tries_left: int = 20) -> None:
        """Makes the popup modal. On X11 a grab fails ("window not viewable") until the window manager has actually mapped
        it -- which, for a popup being SHOWN AGAIN after withdraw(), is usually a moment after deiconify() returns -- so a
        failed grab is retried briefly instead of leaving a reopened popup non-modal (the song lists rely on it being modal
        to follow a moved pass mark only when it closes)."""
        if not self._settings_window_shown:
            return
        try:
            dialog.grab_set()
        except tk.TclError:
            if tries_left > 0:
                try:
                    dialog.after(50, lambda: self._grab_settings_dialog(dialog, tries_left - 1))
                except tk.TclError:
                    pass  # the window went away meanwhile

    def _hide_settings_dialog(self, dialog) -> None:
        """Closing hides the popup (it is reused next time) and, if the timing pass mark ended up different from when it
        opened, refreshes the lists built from it ONCE -- never per slider step (issue #7: each 1% of a drag used to rescan
        and rebuild every open list, and write holds into song files for values the owner only passed through)."""
        self._settings_window_shown = False   # first: a grab retry still pending (_grab_settings_dialog) must not fire
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        dialog.withdraw()
        if self._pass_mark_at_open is not None and self.settings.timing_pass_percent != self._pass_mark_at_open:
            self._apply_pass_mark_change()
        self._pass_mark_at_open = None

    def _apply_pass_mark_change(self) -> None:
        """What counts as a good video moved: every list built from the timing pass mark is stale (an open one re-scans in
        the background, a closed one on its next open)."""
        self._invalidate_upload_list()
        self._invalidate_pending_list()
        self._invalidate_flagged_list()
        self._invalidate_easy_chord_backfill_list()

    @staticmethod
    def _ask_over(window, title: str, message: str) -> bool:
        """askyesno parented to `window` and raised above it: without a parent, a confirmation asked from a transient +
        grab_set popup can open BEHIND it on some Linux window managers, leaving the popup looking hung (HISTORY 9-10,
        the Apply Update dialog; issue #7 review)."""
        raised = False
        try:
            window.attributes("-topmost", True)
            raised = True
        except (tk.TclError, AttributeError):
            pass
        try:
            return messagebox.askyesno(title, message, parent=window)
        finally:
            if raised:
                try:
                    window.attributes("-topmost", False)
                except tk.TclError:
                    pass

    def _make_collapsible_section(
        self, parent, title: str, header_extra=None, on_first_expand=None,
    ):
        """A section that starts CLOSED (owner request, 2026-09-15, after the
        three song lists being open by default made the window unmanageably
        tall) -- clicking the header toggles a content frame the caller packs
        its own widgets into. `header_extra(header_row)`, if given, adds
        something that stays visible whether the section is open or not (the
        Pending list's Select All checkbox). `on_first_expand`, if given, is
        called once -- only the first time the section is actually opened,
        never during startup -- to build its (possibly expensive) contents
        lazily: real owner complaint, 2026-09-15, "always extremely slow" to
        launch -- eagerly building a CTkRadioButton/CTkCheckBox row (plus a
        Watch and a Remove button each) for every song in a 65-song work/
        folder across all three lists, whether or not anyone ever opens them,
        measured at 28 SECONDS of `LyricVideoGUI.__init__` alone (vs. 0.05s
        for the actual filesystem scan behind them -- CustomTkinter widget
        construction, not I/O, is what's slow here).

        Returns (content_frame, invalidate) -- `invalidate()` is for a caller
        whose underlying data changed (e.g. after an upload) to call INSTEAD
        of rebuilding the list itself: if the section is open it refreshes
        right away (via `on_first_expand` again), but if it's closed it just
        marks the content stale for the next real open. Real owner
        complaint, 2026-09-15: rebuilding a still-CLOSED, never-opened list
        after every single upload was the exact same expensive-widget-
        construction cost as the launch-time bug above, just re-triggered on
        a different event -- freezing the window for a stretch even though
        nobody was even looking at that list. Since issue #7 the song lists'
        `on_first_expand` only STARTS a background scan (_start_list_load),
        so opening or refreshing an open section never blocks the window
        either; a burst of invalidations costs one extra scan, not one each."""
        section = ctk.CTkFrame(parent)
        section.pack(fill="x", padx=10, pady=(0, 10))
        header = ctk.CTkFrame(section, fg_color="transparent")
        header.pack(fill="x")
        content = ctk.CTkFrame(section, fg_color="transparent")
        state = {"expanded": False, "populated": on_first_expand is None}

        def populate_now() -> None:
            if on_first_expand is not None:
                on_first_expand()
            state["populated"] = True

        def toggle() -> None:
            if state["expanded"]:
                content.pack_forget()
                toggle_button.configure(text=f"▶ {title}")
            else:
                if not state["populated"]:
                    populate_now()
                content.pack(fill="x")
                toggle_button.configure(text=f"▼ {title}")
            state["expanded"] = not state["expanded"]

        def invalidate() -> None:
            if state["expanded"]:
                populate_now()
            else:
                state["populated"] = False

        toggle_button = ctk.CTkButton(
            header, text=f"▶ {title}", command=toggle, anchor="w",
            fg_color="transparent", hover_color=("gray80", "gray25"),
            font=ctk.CTkFont(weight="bold"),
        )
        toggle_button.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        if header_extra is not None:
            header_extra(header)
        return content, invalidate

    def _add_row(self, frame: ctk.CTkFrame, row: int, label: str, var: tk.StringVar) -> None:
        ctk.CTkLabel(frame, text=label).grid(row=row, column=0, sticky="w")
        ctk.CTkEntry(frame, textvariable=var, width=360).grid(row=row, column=1, sticky="ew", padx=4)

    def _add_file_row(
        self, frame: ctk.CTkFrame, row: int, label: str, var: tk.StringVar, filetypes: list,
        on_selected=None,
    ) -> None:
        self._add_row(frame, row, label, var)

        def browse() -> None:
            path = filedialog.askopenfilename(filetypes=filetypes)
            if path:
                var.set(path)
                if on_selected is not None:
                    on_selected(path)

        ctk.CTkButton(frame, text="Browse...", command=browse, width=90).grid(row=row, column=2, padx=4)

    def _use_settings_pass_mark(self) -> None:
        """Points the timing gate at the LIVE Settings (a function, not a copy), so a moved slider is used by the very next
        check -- the Upload list, the Pending list, Flagged for Lyrics Review and the next render."""
        use_pass_share_from(lambda: self.settings.timing_pass_percent / 100)

    def _uploadable_songs(self) -> tuple[list[str], str]:
        """The Upload to YouTube list (owner, 2026-09-20: only good videos): songs that pass the timing pass mark, plus the
        note shown under the list saying how many are hidden and below what. Filesystem work only -- it runs on the list
        scanner's background thread (issue #7), never the Tk thread."""
        root = PROJECT_ROOT / "work"
        songs = list_uploadable_songs(root)
        note = hidden_note(len(list_rendered_songs(root)) - len(songs), self.settings.timing_pass_percent)
        return songs, note

    def _on_settings_changed(self) -> None:
        """SettingsPanel's on_change fires on every keystroke/slider-move/color-pick,
        live-updating the preview and the in-memory settings this session's own
        Generate/Redo/Batch will use -- but never the settings FILE on disk.
        SettingsPanel owns persistence entirely itself now (its own "Save Settings"
        button, with an itemized confirm dialog first): a change here becoming
        permanent the instant a slider gets nudged is exactly the real incident
        (2026-09-10) this split was built to prevent. Suppressed while the panel
        is still being populated on launch (Settings.load() itself is already the
        source of truth then).

        Only the cheap part runs per event (issue #7): self.settings is updated at once, the full-resolution preview is
        re-rendered once the changes pause (_schedule_settings_preview), and a moved timing pass mark refreshes the song
        lists once, when the popup closes (_hide_settings_dialog) -- never per slider step."""
        if self._suppress_settings_save:
            return
        self.settings = self.settings_panel.collect()
        self._schedule_settings_preview()

    _PREVIEW_DELAY_MS = 150

    def _schedule_settings_preview(self) -> None:
        """Debounces the live preview: a slider drag fires dozens of changes a second and each render took ~55 ms on the
        Tk thread here, so the preview is drawn once, _PREVIEW_DELAY_MS after the last change."""
        if self._preview_after_id is not None:
            try:
                self.root.after_cancel(self._preview_after_id)
            except (tk.TclError, ValueError, AttributeError):
                pass
        self._preview_after_id = self.root.after(self._PREVIEW_DELAY_MS, self._flush_settings_preview)

    def _flush_settings_preview(self) -> None:
        self._preview_after_id = None
        preview = getattr(self, "settings_preview", None)
        if preview is None:
            return
        try:
            preview.update_preview(self.settings)
        except tk.TclError:
            pass  # the popup went away meanwhile

    def _on_title_changed(self, *_args) -> None:
        if not self._running:
            slug = _slugify(self.title_var.get())
            self.work_dir_var.set(str(PROJECT_ROOT / "work" / slug))
        self._update_youtube_title_preview()

    def _update_youtube_title_preview(self) -> None:
        """Shows the exact title that will be used on YouTube upload
        (build_play_along_title -- see youtube_metadata.py) next to the
        editable Song title field. This is a PREVIEW only: unlike the Song
        title field itself, it never feeds into identify/lyrics-search/the
        video filename -- those all need the bare title, not this combined
        display string (owner request, 2026-09-14, wants to see the real
        upload title without the two getting tangled together)."""
        title = self.title_var.get().strip()
        if not title:
            self.youtube_title_preview_var.set("")
            return
        self.youtube_title_preview_var.set(
            f"Will upload to YouTube as: {build_play_along_title(title, self._identified_artist)}"
        )

    def _on_audio_selected(self, path: str) -> None:
        """Best-effort auto-fill of the title once an audio file is picked --
        never overwrites a title the owner already typed, and any failure
        (offline, unreadable file) is silently ignored: Generate still works
        with an auto-identified title computed fresh inside run_pipeline's own
        identify stage regardless of whether this GUI-side preview succeeds.

        A title identify filled in for the PREVIOUS file is not the owner's: picking another file clears it (and its work
        folder and artist) and identifies the new one (issue #7 review, F031/F032 -- the old title and work/<old title>
        stayed, so Generate overwrote the previous song's folder, inheriting its upload record, key and lyrics)."""
        current = self.title_var.get().strip()
        # The previous file's artist is never this one's, even under a typed title: it feeds the upload-title preview
        # and Generate's "does this folder hold another recording?" check. The new file is identified either way; a
        # typed title is kept (_apply_identified_title then only takes the artist).
        self._identified_artist = ""
        if current and current != getattr(self, "_auto_filled_title", ""):
            self._update_youtube_title_preview()
        else:
            self._auto_filled_title = ""
            self.title_var.set("")
            self.work_dir_var.set("")  # after title_var -- overrides its own auto-fill trace
        thread = threading.Thread(target=self._identify_worker, args=(path,), daemon=True)
        thread.start()

    def _identify_worker(self, path: str) -> None:
        try:
            info = extract_metadata(Path(path))
        except Exception:
            return
        self.root.after(0, lambda: self._apply_identified_title(info.title, info.artist, path))

    def _apply_identified_title(self, title: str, artist: str = "", path: str | None = None) -> None:
        if path is not None and path != self.audio_var.get():
            return  # a slower lookup for a file the owner has since replaced -- never applied to the new one
        self._identified_artist = artist
        if not self.title_var.get().strip():  # still empty -- no manual edit arrived meanwhile
            self._auto_filled_title = title.strip()
            self.title_var.set(title)
        else:
            self._update_youtube_title_preview()  # title unchanged, but artist just arrived

    def _check_api_keys(self) -> None:
        load_dotenv(PROJECT_ROOT / ".env")
        missing = [k for k in ("ANTHROPIC_API_KEY", "REPLICATE_API_TOKEN") if not os.environ.get(k)]
        if missing:
            messagebox.showwarning(
                "Missing API keys",
                f"{', '.join(missing)} not found in {PROJECT_ROOT / '.env'}.\n"
                "Video generation will fail at the images stage without them.",
            )

    def _start_update_check(self) -> None:
        thread = threading.Thread(target=self._update_check_worker, daemon=True)
        thread.start()
        self.root.after(200, self._poll_update_queue)

    def _update_check_worker(self) -> None:
        try:
            release = check_for_update(self._current_version, RELEASES_REPO)
        except Exception:
            # Offline, GitHub hiccup, or no releases cut yet -- the launch-
            # time check must never surface an error or crash the GUI.
            return
        if release is not None:
            self._update_queue.put(("available", release))

    def _handle_update_message(self, kind: str, payload) -> None:
        if kind == "available":
            self._available_update = payload
            self.update_banner_var.set(
                f"Update available: {payload['tag_name']} — click for details"
            )
            self.update_banner.pack(
                fill="x", padx=8, pady=(4, 0), before=self.top_frame
            )
        elif kind == "apply_status":
            if hasattr(self, "_update_status_var") and self._update_dialog_alive():
                self._update_status_var.set(payload)
        elif kind == "apply_done":
            self._on_apply_update_done(payload)
        elif kind == "apply_up_to_date":
            self._on_apply_update_up_to_date(payload)
        elif kind == "apply_error":
            self._on_apply_update_error(payload)

    def _poll_update_queue(self) -> None:
        """Handles each queued update message on its own and ALWAYS reschedules (issue #7 review, F107-F109: a handler
        that raised -- e.g. drawing Relaunch Now into a dialog closed during the download -- ended this poll for the rest of
        the session, so no later result was ever shown; the same failure _poll_queue was fixed for on 2026-09-22)."""
        try:
            while True:
                try:
                    kind, payload = self._update_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_update_message(kind, payload)
                except Exception as e:
                    print(f"WARNING: could not handle a {kind!r} update message: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            if not getattr(self, "_closing", False):
                try:
                    self.root.after(200, self._poll_update_queue)
                except tk.TclError:
                    pass  # the window is gone

    def _update_dialog_alive(self) -> bool:
        dialog = getattr(self, "_update_dialog_window", None)
        try:
            return dialog is not None and bool(dialog.winfo_exists())
        except tk.TclError:
            return False

    def _show_update_dialog_again(self) -> None:
        """Brings back the Update dialog -- hidden while an apply ran, or just behind the main window."""
        dialog = self._update_dialog_window
        try:
            dialog.deiconify()
            dialog.lift()
            dialog.focus_force()
            dialog.attributes("-topmost", True)
            dialog.after(300, lambda: dialog.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _on_update_banner_clicked(self, _event=None) -> None:
        if self._update_dialog_alive():
            # One Update dialog at a time: a second one offered a second, concurrent Apply (two pip installs in one venv).
            self._show_update_dialog_again()
            return
        if self._available_update is not None:
            self._open_update_dialog(self._available_update)

    def _open_update_dialog(self, release: dict) -> None:
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Update available: {release['tag_name']}")
        dialog_w, dialog_h = 480, 360
        self.root.update_idletasks()
        # Centered over the main window and kept above it (transient +
        # grab_set + lift/focus_force) -- a plain Toplevel can otherwise open
        # behind the main window with no visible indication, which is
        # exactly how the owner missed the Relaunch Now button appearing.
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(x, 0)}+{max(y, 0)}")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        # lift()/focus_force() alone are NOT reliably honored by every Linux
        # window manager -- several (Cinnamon included) block focus-stealing
        # outright, so the dialog can still open silently behind the main
        # window despite the calls above (confirmed live, 2026-09-10, on
        # Linux Mint -- this is a second, harder recurrence of the exact bug
        # this function's docstring already describes once). Briefly forcing
        # "always on top" is the one approach every window manager actually
        # respects; clearing it a moment later avoids permanently pinning
        # this dialog above every other window on the desktop.
        dialog.attributes("-topmost", True)
        dialog.after(300, lambda: dialog.attributes("-topmost", False))
        self._update_dialog_window = dialog

        notes_widget = ctk.CTkTextbox(dialog, wrap="word", height=220)
        notes_widget.insert("1.0", release.get("notes", "") or "(no release notes)")
        notes_widget.configure(state="disabled")
        notes_widget.pack(fill="both", expand=True, padx=8, pady=8)

        self._update_status_var = tk.StringVar(value="")
        ctk.CTkLabel(dialog, textvariable=self._update_status_var, text_color="gray60").pack(
            anchor="w", padx=8
        )

        self._update_button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        self._update_button_frame.pack(fill="x", padx=8, pady=8)

        self._update_apply_button = ctk.CTkButton(
            self._update_button_frame,
            text="Apply Update",
            command=lambda: self._on_apply_update_clicked(release),
            state="disabled" if self._update_apply_in_progress else "normal",
        )
        self._update_apply_button.pack(side="left")

        def _on_close() -> None:
            # Close and the window's X do the same thing (issue #7 review, F107-F109: Close used to destroy the dialog
            # without forgetting it). While an apply is still running the dialog is only HIDDEN: its result brings it
            # back (with Relaunch Now), and the banner reopens this same one -- never a second dialog with a second Apply.
            if self._update_apply_in_progress:
                try:
                    dialog.grab_release()
                    dialog.withdraw()
                except tk.TclError:
                    pass
                return
            self._update_dialog_window = None
            dialog.destroy()

        ctk.CTkButton(self._update_button_frame, text="Close", command=_on_close).pack(
            side="right"
        )
        dialog.protocol("WM_DELETE_WINDOW", _on_close)

    def _on_apply_update_clicked(self, release: dict) -> None:
        # Real recurrence, 2026-09-10 (screenshots): with no `parent=` given,
        # this confirmation isn't WM-recognized as belonging to the "Update
        # available" dialog it's actually triggered from -- on the same
        # flaky window manager already found for that dialog, it could open
        # behind it instead of on top. `parent=dialog` gives it the correct
        # transient relationship; briefly forcing `dialog` itself topmost
        # around the call is the same belt-and-suspenders fix already used
        # for that dialog's own visibility.
        dialog = self._update_dialog_window
        if self._update_apply_in_progress:
            return  # one apply at a time (issue #7 review, F108/F109)
        busy = self._busy_reasons()
        if busy:
            # pip install and the file copy would swap the program's own packages and files under a live pipeline or
            # upload (issue #7 review, F110/F111).
            messagebox.showinfo(
                "Apply update",
                f"Wait for {' and '.join(busy)} to finish before applying an update -- it reinstalls the program's own "
                "files under it.",
                parent=dialog,
            )
            return
        if dialog is not None:
            dialog.attributes("-topmost", True)
        try:
            confirmed = messagebox.askyesno(
                "Apply update",
                f"Download and apply {release['tag_name']} now?\n\n"
                "This reinstalls dependencies if they changed and overwrites the "
                "program's own files. Your songs, work files, and .env are never "
                "touched.",
                parent=dialog,
            )
        finally:
            if dialog is not None:
                dialog.attributes("-topmost", False)
        if not confirmed:
            return
        self._update_apply_in_progress = True
        self._update_apply_button.configure(state="disabled")
        self._update_status_var.set("Downloading...")
        thread = threading.Thread(
            target=self._apply_update_worker, args=(release,), daemon=True
        )
        try:
            thread.start()
        except BaseException:
            self._update_apply_in_progress = False
            raise

    def _apply_update_worker(self, release: dict) -> None:
        try:
            self._update_queue.put(("apply_status", "Downloading..."))
            import httpx  # imported here, not at launch: only an Apply Update click needs it (issue #7)

            # follow_redirects=True is required: unlike requests, httpx does
            # NOT follow redirects by default, and this URL 302s to
            # codeload.github.com.
            response = httpx.get(release["download_url"], timeout=60, follow_redirects=True)
            response.raise_for_status()

            with tempfile.TemporaryDirectory() as tmp_dir:
                archive_path = Path(tmp_dir) / "release.tar.gz"
                archive_path.write_bytes(response.content)

                self._update_queue.put(("apply_status", "Extracting..."))
                extract_dir = Path(tmp_dir) / "extracted"
                extract_dir.mkdir()
                extracted_root = extract_release_archive(str(archive_path), str(extract_dir))

                source_commit = read_release_source_commit(extracted_root)
                if is_source_commit_already_applied(source_commit, str(PROJECT_ROOT)):
                    write_local_version(str(_VERSION_FILE_PATH), release["tag_name"])
                    self._update_queue.put(("apply_up_to_date", release["tag_name"]))
                    return

                old_requirements_path = PROJECT_ROOT / "requirements.txt"
                old_requirements = (
                    old_requirements_path.read_text(encoding="utf-8")
                    if old_requirements_path.exists()
                    else ""
                )
                new_requirements_path = Path(extracted_root) / "requirements.txt"
                new_requirements = (
                    new_requirements_path.read_text(encoding="utf-8")
                    if new_requirements_path.exists()
                    else old_requirements
                )

                if requirements_changed(old_requirements, new_requirements):
                    self._update_queue.put(("apply_status", "Installing dependencies..."))
                    pip_result = subprocess.run(
                        [str(venv_python(PROJECT_ROOT)), "-m", "pip", "install", "-r", "requirements.txt"],
                        cwd=extracted_root,
                        capture_output=True,
                        text=True,
                    )
                    if pip_result.returncode != 0:
                        self._update_queue.put((
                            "apply_error",
                            f"pip install failed — program left unchanged:\n{pip_result.stderr}",
                        ))
                        return

                self._update_queue.put(("apply_status", "Copying files..."))
                copy_updatable_files(extracted_root, str(PROJECT_ROOT))
                write_local_version(str(_VERSION_FILE_PATH), release["tag_name"])

            self._update_queue.put(("apply_done", release["tag_name"]))
        except Exception as e:
            self._update_queue.put(("apply_error", f"{type(e).__name__}: {e}"))

    def _show_apply_result(self, status: str, relaunch: bool) -> None:
        """Shows an Apply Update result in its dialog, bringing a dialog hidden during the apply back first. If the dialog
        is gone altogether, a plain message says it instead -- never a TclError from a destroyed widget."""
        self._update_apply_in_progress = False
        if not self._update_dialog_alive():
            (messagebox.showinfo if relaunch else messagebox.showerror)(
                "Apply update", status + (" Close and reopen the app to use it." if relaunch else ""),
            )
            return
        self._show_update_dialog_again()
        self._update_status_var.set(status)
        if relaunch:
            self._update_apply_button.pack_forget()
            ctk.CTkButton(
                self._update_button_frame, text="Relaunch Now", command=self._on_relaunch_clicked
            ).pack(side="left")
        else:
            self._update_apply_button.configure(state="normal")

    def _on_apply_update_done(self, tag_name: str) -> None:
        self._show_apply_result(f"Updated to {tag_name}. Relaunch to use it.", relaunch=True)

    def _on_apply_update_up_to_date(self, tag_name: str) -> None:
        """This checkout's own git history already contains the commit
        release tag_name was built from -- no files were touched, only
        VERSION was recorded, so a stale/out-of-order release can never
        silently revert newer local commits (see
        is_source_commit_already_applied)."""
        self._show_apply_result(
            f"Already up to date ({tag_name}) -- this checkout's own commits "
            "already include it, so no files were changed.",
            relaunch=True,
        )

    def _on_apply_update_error(self, message: str) -> None:
        self._show_apply_result(f"Update failed: {message}", relaunch=False)

    def _on_relaunch_clicked(self) -> None:
        # Relaunching quits this window: the same confirmation as its X while anything is running (issue #7 review,
        # F110/F111 -- it used to kill a running Batch or upload without a word).
        if not self._confirm_quit_if_busy(parent=getattr(self, "_update_dialog_window", None)):
            return
        subprocess.Popen([str(venv_python(PROJECT_ROOT)), "-m", "lyricvideo.gui"], cwd=str(PROJECT_ROOT))
        self._shut_down()

    def _on_new_song(self) -> None:
        if self._running:
            return
        self._identified_artist = ""
        self._auto_filled_title = ""
        self.title_var.set("")
        self.audio_var.set("")
        self.work_dir_var.set("")  # after title_var -- overrides its own auto-fill trace
        self.status_var.set("Ready")
        self.progress_bar.set(0.0)
        self._clear_log()
        self._last_work_dir = None

    def _on_close_window(self) -> None:
        """Bound to the window's own close (X) button, the only way to quit
        this app -- if nothing is running, closes immediately; if a
        Generate/Redo/Batch is in progress, confirms first, since closing
        mid-run kills the pipeline (and any in-flight Demucs/render/upload
        work) partway through with no way to resume it. Owner-requested,
        2026-09-10. A YouTube upload or an Apply Update in progress asks too
        (issue #7 review)."""
        if not self._confirm_quit_if_busy():
            return
        self._shut_down()

    def _busy_reasons(self) -> list[str]:
        """What quitting (or applying an update) right now would cut off, in the owner's words."""
        reasons = []
        if getattr(self, "_running", False):
            reasons.append("a video being made (Generate / Redo / Batch)")
        if _uploads_busy():
            reasons.append("a YouTube upload")
        if getattr(self, "_update_apply_in_progress", False):
            reasons.append("an update being applied")
        return reasons

    def _confirm_quit_if_busy(self, parent=None) -> bool:
        """True when nothing is running, or the owner agrees to stop it; the X button and Relaunch Now both ask this."""
        reasons = self._busy_reasons()
        if not reasons:
            return True
        return messagebox.askyesno(
            "Quit while running?",
            f"Still running: {'; '.join(reasons)}. Quitting now stops it partway through -- it will not resume from "
            "where it left off.\n\nQuit anyway?",
            **({"parent": parent} if parent is not None else {}),
        )

    def _shut_down(self) -> None:
        """Makes the window disappear at once, then tears it down (issue #7, "long time ... to x out"). tkinter's
        destroy() walks every widget from Python, and it used to do that with the window still on screen and frozen --
        seconds with a song list open (~5,000 widgets for the owner's library), far longer on his box. Now the window is
        withdrawn first, so it is gone the moment X is clicked, and there are only a few hundred widgets left to free
        (the song lists are Treeviews). A song-list scan still running finishes on its own (non-daemon) thread, so a timing
        hold it is writing into a song's lyrics_timed.json is never cut off halfway; nothing new starts once _closing is
        set."""
        self._closing = True
        try:
            self.root.withdraw()
            self.root.update_idletasks()
        except tk.TclError:
            pass
        self._destroy_root()

    def _destroy_root(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _on_browse_batch_folder(self) -> None:
        current = self.batch_folder_var.get()
        initialdir = current if current and Path(current).is_dir() else None
        folder = filedialog.askdirectory(initialdir=initialdir) if initialdir else filedialog.askdirectory()
        if folder:
            # resolve_existing_folder recovers a trailing/leading space the dialog
            # itself silently drops (see resolve_existing_folder's docstring) --
            # saving the corrected path keeps next time's initialdir usable too.
            folder = str(resolve_existing_folder(Path(folder)))
            self.batch_folder_var.set(folder)
            save_last_batch_folder(folder)

    def _on_start_batch(self) -> None:
        if self._running:
            return
        # NOT .strip()'d -- unlike the typed title/audio/work-dir fields, this
        # value comes verbatim from a real folder the OS file dialog resolved,
        # and a real folder name can legitimately have leading/trailing
        # whitespace (confirmed live, 2026-09-10: a folder literally named
        # "batch music " with a trailing space -- stripping it here made the
        # app look for a folder that doesn't exist).
        folder = self.batch_folder_var.get()
        if not folder.strip():
            messagebox.showerror("No folder selected", "Choose a folder to batch-process first.")
            return

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.status_var.set("Scanning folder...")
        self._clear_log()

        thread = threading.Thread(target=self._resolve_batch_worker, args=(Path(folder),), daemon=True)
        thread.start()
        self.root.after(100, self._poll_queue)

    def _resolve_batch_worker(self, folder: Path) -> None:
        try:
            folder = resolve_existing_folder(folder)
            files = find_audio_files(folder)
            items = resolve_batch_items(files, PROJECT_ROOT / "work")
            self._queue.put(("batch_resolved", items))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

    def _start_batch_run(self, items: list) -> None:
        self._batch_items = items
        self._batch_index = 0
        self._batch_results = {"succeeded": [], "skipped_already_done": [], "failed": []}
        self.status_var.set(f"Starting batch (0/{len(items)})...")
        self.progress_bar.set(0.0)

        thread = threading.Thread(target=self._run_batch_worker, args=(items,), daemon=True)
        thread.start()
        self.root.after(100, self._poll_queue)

    def _run_batch_worker(self, items: list) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        results = {"succeeded": [], "skipped_already_done": [], "failed": []}
        try:
            for index, item in enumerate(items, start=1):
                self._queue.put(("batch_file_start", (index, len(items), item.title)))
                try:
                    undismiss_song("flagged", item.work_dir.name)     # regenerating a removed song brings it back to review
                    if item.already_done:
                        backup_song_outputs(item.work_dir, _slugify(item.title))
                        run_pipeline(
                            item.audio_path, item.work_dir, item.title,
                            start_stage="fetch_lyrics", settings=self.settings,
                            progress_callback=lambda stage: self._queue.put(("stage", stage)),
                        )
                    else:
                        run_pipeline(
                            item.audio_path, item.work_dir, item.title,
                            start_stage=item.resume_stage, settings=self.settings,
                            progress_callback=lambda stage: self._queue.put(("stage", stage)),
                        )
                    _maybe_upload_to_youtube(item.work_dir, self.settings)
                    self._queue.put(("batch_item_done", None))
                    results["succeeded"].append(item.title)
                except HeldBeforeVideo as e:
                    # Not a failure: the sync check said no, so no chords/images/video were made (a big saving) and it waits
                    # in Flagged for Lyrics Review. The batch just moves on to the next song.
                    results.setdefault("held", []).append(item.title)
                    print(f"Batch item {item.title!r} held for review before its video: {e.concern}")
                    self._queue.put(("batch_item_done", None))
                except Exception as e:
                    results["failed"].append((item.title, f"{type(e).__name__}: {e}"))
                    print(f"Batch item {item.title!r} failed: {type(e).__name__}: {e}")
                finally:
                    # Real incident, 2026-09-18: earlyoom killed the app mid-
                    # batch (song #24 of 100) after memory crept up across
                    # dozens of songs in this one long-lived process. Runs
                    # after every song, success or failure, so the baseline
                    # never climbs from one song into the next.
                    release_memory()
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        self._queue.put(("batch_done", results))

    def _on_batch_resolved(self, items: list) -> None:
        if not items:
            self._running = False
            self.generate_button.configure(state="normal")
            self.redo_button.configure(state="normal")
            self.batch_button.configure(state="normal")
            self.status_var.set("Ready")
            messagebox.showinfo("No audio files found", "That folder has no .mp3/.wav/.m4a/.flac files.")
            return

        done_count = sum(1 for i in items if i.already_done)
        to_process = items
        if done_count:
            # Yes / No / Cancel (issue #7 review, F112): with a plain yes/no, closing the prompt or pressing Esc answered
            # "No" -- regenerate every finished song, hours of work and API spend -- and nothing could stop the batch.
            skip_done = messagebox.askyesnocancel(
                "Some songs already done",
                f"{done_count} of {len(items)} songs already have a finished video.\n\n"
                "Skip those and only process the rest?\n\n"
                "Yes = skip them. No = regenerate everyone, backing up each one's current video/timing first, "
                "same as Redo. Cancel = don't start the batch.",
            )
            if skip_done is None:
                self._running = False
                self.generate_button.configure(state="normal")
                self.redo_button.configure(state="normal")
                self.batch_button.configure(state="normal")
                self.status_var.set("Ready")
                return
            if skip_done:
                to_process = [i for i in items if not i.already_done]

        if not to_process:
            self._running = False
            self.generate_button.configure(state="normal")
            self.redo_button.configure(state="normal")
            self.batch_button.configure(state="normal")
            self.status_var.set("Ready")
            messagebox.showinfo("Nothing to do", "Every song in that folder is already done.")
            return

        self._start_batch_run(to_process)

    def _on_batch_done(self, results: dict) -> None:
        self._batch_items = []
        self._batch_index = 0
        self.status_var.set("Batch done")
        self.progress_bar.set(1.0)
        self._running = False
        self.generate_button.configure(state="normal")
        self.redo_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self._refresh_retry_upload_options()

        summary = (
            f"Succeeded: {len(results['succeeded'])}\n"
            f"Failed: {len(results['failed'])}"
        )
        if results.get("held"):
            summary += f"\nHeld for review (no video made): {len(results['held'])}"
        if results["failed"]:
            failed_names = "\n".join(f"  - {title}: {err}" for title, err in results["failed"])
            messagebox.showwarning("Batch finished with failures", f"{summary}\n\n{failed_names}")
        else:
            messagebox.showinfo("Batch finished", summary)

    def _settle_generate_work_dir(self, audio: Path, work_dir: Path, title: str) -> Path | None:
        """The folder Generate may write into, or None when the owner cancels (issue #7 review, F027/F034). A folder that
        already holds a DIFFERENT recording -- a same-title cover -- is not taken over without asking: it would lose that
        song's video and hand this one its upload record (so it never uploads), its key and its edited lyrics. A folder
        that already holds this song has its video and timing backed up first, as Redo does."""
        stored = stored_song_identity(work_dir)
        if stored is None:
            return work_dir
        other_title, other_artist, _other_audio = stored
        artist = getattr(self, "_identified_artist", "")
        if holds_other_recording(work_dir, artist, audio):
            alternative = work_dir_for(work_dir.parent, title or audio.stem, artist, audio)
            answer = messagebox.askyesnocancel(
                "Work folder holds another song",
                f'"{work_dir.name}" already holds "{other_title or work_dir.name}" by {other_artist}, not this recording '
                f"by {artist}.\n\nYes = use a new folder, \"{alternative.name}\".\nNo = replace that song in "
                f'"{work_dir.name}" (its video and timing are backed up first; its YouTube upload record, key and '
                "edited lyrics stay with the folder).\nCancel = don't start.",
            )
            if answer is None:
                return None
            if answer:
                self.work_dir_var.set(str(alternative))
                work_dir = alternative
                # The new folder may already hold THIS recording from an earlier Generate: backed up like any other.
                stored = stored_song_identity(work_dir)
                if stored is None:
                    return work_dir
                other_title = stored[0]
        try:
            backup_song_outputs(work_dir, _slugify(other_title or title or work_dir.name))
        except OSError as e:
            messagebox.showerror("Could not back up the song", f"Nothing was started.\n\n{type(e).__name__}: {e}")
            return None
        return work_dir

    def _on_generate(self) -> None:
        if self._running:
            return

        title = self.title_var.get().strip()  # optional -- run_pipeline's own
        audio = self.audio_var.get().strip()   # identify stage falls back if blank
        work_dir = self.work_dir_var.get().strip()

        if not audio:
            messagebox.showerror("Missing input", "Audio file is required.")
            return
        if not Path(audio).is_file():
            # A typo'd/moved path used to surface only minutes later, as an
            # obscure Demucs or ffmpeg failure deep in the log.
            messagebox.showerror("Audio file not found", f"No such file:\n{audio}")
            return
        if not work_dir:
            work_dir = _default_work_dir_from_audio(audio)
            self.work_dir_var.set(work_dir)
        settled = self._settle_generate_work_dir(Path(audio), Path(work_dir), title)
        if settled is None:
            return
        work_dir = str(settled)

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.status_var.set("Starting...")
        self.progress_bar.set(0.0)
        self._clear_log()

        self._last_work_dir = Path(work_dir)
        thread = threading.Thread(
            target=self._run_worker,
            args=(Path(audio), Path(work_dir), title or None),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _on_redo(self) -> None:
        if self._running:
            return

        slug = self.redo_song_var.get().strip()
        if not slug:
            messagebox.showerror("No song selected", "Pick a song from the list to redo.")
            return
        if _refuse_easy_variant(slug, "Redo"):
            return

        song_dir = PROJECT_ROOT / "work" / slug
        try:
            audio_path, title = load_redo_inputs(song_dir)
        except Exception as e:
            messagebox.showerror("Could not load song", f"{type(e).__name__}: {e}")
            return
        if not audio_path.is_file():
            # The redo re-reads the ORIGINAL audio (sidecar lyrics check, and
            # the final render muxes it in) -- if the owner has since moved or
            # renamed that file, fail here with the path, before backing
            # anything up or starting a run that would die at render time.
            messagebox.showerror(
                "Original audio file not found",
                f'"{title}" was generated from:\n{audio_path}\n\n'
                "That file no longer exists. Put it back (or generate the song "
                "again from the moved file as a new song).",
            )
            return

        generate_new_images = self.redo_new_images_var.get()
        easy_chord = self.redo_easy_chord_var.get()
        if not messagebox.askyesno(
            "Redo song",
            f'Redo "{title}" using the current program?\n\n'
            "This re-syncs chords/lyrics with today's code and re-renders the "
            "video, overwriting it in place -- the current video and timing "
            "data are backed up first. "
            + ("New AI images will be generated. " if generate_new_images
               else "Existing images will be reused (no AI cost). ")
            + ("An EASY CHORD (capo) version will also be built." if easy_chord else ""),
        ):
            return

        backup_song_outputs(song_dir, _slugify(title))
        if generate_new_images:
            prepare_images_for_fresh_regeneration(song_dir / "images")

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.status_var.set("Starting...")
        self.progress_bar.set(0.0)
        self._clear_log()

        self._last_work_dir = song_dir
        thread = threading.Thread(
            target=self._run_worker,
            args=(audio_path, song_dir, title, "fetch_lyrics"),
            kwargs={"force_easy_chord": easy_chord, "fresh_images": generate_new_images},
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _refresh_youtube_status(self) -> None:
        """Refreshes the "YouTube: ..." status label on a background thread.
        Both load_credentials() (a token refresh is a real network call) and
        get_channel_title() (an API call) can block for seconds -- or for a
        whole network timeout when offline -- and this used to run them
        straight on the GUI thread from __init__, so the main window couldn't
        even finish appearing until YouTube answered (found by code review,
        2026-09-14). The 20-minute periodic tick already did it this way;
        the launch-time and post-connect refreshes now match it."""
        threading.Thread(target=self._refresh_youtube_status_worker, daemon=True).start()

    def _youtube_status_text(self) -> str:
        """The connect-status label's text, computed with real network calls
        -- only ever call this from a background thread. Checks the quota
        cooldown BEFORE calling get_channel_title() (2026-09-18): a real
        API call here every 20 minutes (plus at launch and after every
        Connect) is exactly the kind of steady drumbeat of failing requests
        that could make YouTube read the app as abusive once quota's
        exceeded, so this makes zero real calls while blocked, at every
        caller, for free -- no per-caller gating needed."""
        credentials = youtube_auth.load_credentials()
        if credentials is None:
            return "YouTube: not connected"
        blocked_until = load_quota_blocked_until()
        if blocked_until is not None and datetime.now().astimezone() < blocked_until:
            return f"YouTube: quota exceeded, retrying after {blocked_until.astimezone():%Y-%m-%d %H:%M}"
        try:
            return f"YouTube: connected as {youtube_auth.get_channel_title(credentials)}"
        except Exception as e:
            if is_quota_exceeded_error(e):
                blocked_until = datetime.now().astimezone() + timedelta(hours=self.settings.youtube_quota_retry_hours)
                save_quota_blocked_until(blocked_until)
                return f"YouTube: quota exceeded, retrying after {blocked_until:%Y-%m-%d %H:%M}"
            return "YouTube: connected (channel name unavailable)"

    def _refresh_youtube_status_worker(self) -> None:
        text = self._youtube_status_text()
        self.root.after(0, lambda: self.youtube_status_var.set(text))

    def _confirm_quota_override_if_blocked(self) -> bool:
        """Gate for every MANUAL YouTube action (upload buttons, Check Now,
        Approve reply/comment) -- returns True immediately when no quota
        cooldown is active. While one is active, asks first instead of
        silently either blocking or firing off the call: automatic triggers
        (the 20-minute tick) stay fully silent during a cooldown, but a
        deliberate click is the owner's own call to make (owner decision,
        2026-09-18). Must only be called from the main thread -- askyesno
        blocks it, which is fine here since a button click is already a
        synchronous, main-thread event."""
        blocked_until = load_quota_blocked_until()
        if blocked_until is None or datetime.now().astimezone() >= blocked_until:
            return True
        return messagebox.askyesno(
            "YouTube quota exceeded",
            "YouTube's API quota was exceeded; this is scheduled to retry "
            f"automatically after {blocked_until.astimezone():%Y-%m-%d %H:%M}. Continue anyway?",
        )

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
                # The message is formatted HERE, not inside the lambda: Python
                # unbinds `e` the moment this except block ends, so a lambda
                # that reads `e` later (from the Tk event loop, via after())
                # raised NameError instead of ever showing the dialog -- the
                # owner saw nothing at all when a connect failed (found by
                # static analysis, 2026-09-14; same latent bug in the manual
                # upload and Approve-reply workers below).
                message = f"{type(e).__name__}: {e}"
                self.root.after(0, lambda: messagebox.showerror("Could not connect to YouTube", message))

        threading.Thread(target=worker, daemon=True).start()

    def _on_retry_upload(self) -> None:
        if self._running:
            return
        slug = self.retry_upload_song_var.get().strip()
        if not slug:
            messagebox.showerror("No song selected", "Pick a song from the list.")
            return
        song_dir = PROJECT_ROOT / "work" / slug
        if load_youtube_state(song_dir) is not None or _state_file_exists(song_dir):
            # Already uploaded -- reachable for any past song now, not just
            # the one just generated, so a stray click shouldn't silently
            # duplicate a video already live on the channel. An unreadable
            # youtube_state.json still means "uploaded" (F106).
            if not messagebox.askyesno(
                "Already uploaded",
                f'"{slug}" was already uploaded to YouTube. Upload again and create a duplicate video?',
            ):
                return
            self._start_retry_upload([slug], allow_reupload=True)   # the owner's deliberate duplicate
            return
        self._start_retry_upload([slug])

    def _on_toggle_pending_select_all(self) -> None:
        value = self.pending_select_all_var.get()
        for var in self._pending_upload_vars.values():
            var.set(value)

    def _on_upload_selected_pending(self) -> None:
        if self._running:
            return
        slugs = [slug for slug, var in self._pending_upload_vars.items() if var.get()]
        if not slugs:
            messagebox.showerror("No songs selected", "Check at least one pending song to upload.")
            return
        self._start_retry_upload(slugs)

    def _start_retry_upload(self, slugs: list[str] | None, allow_reupload: bool = False) -> None:
        """Starts one manual upload run (Upload, Upload Selected, Upload Anyway). Only one run exists at a time -- a click
        while one (or the 20-minute auto-retry) is uploading is refused, not queued (issue #7 review: two runs uploaded the
        same songs twice). Every song is re-checked when its turn comes and skipped if it was uploaded meanwhile, except
        after the owner confirmed a deliberate duplicate (`allow_reupload`, the single-song Upload button only)."""
        if _upload_run_active():
            messagebox.showinfo(
                "Upload already running",
                "An upload is already running. It carries on in the background -- wait for its results, then try again.",
            )
            return
        # A deliberate manual click always gets to try past a quota
        # cooldown if the owner confirms (2026-09-18) -- force=True is a
        # no-op when there's no cooldown active, so this is safe to pass
        # unconditionally once the confirm gate below has been cleared.
        if not self._confirm_quota_override_if_blocked():
            return
        if not _UPLOAD_RUN_LOCK.acquire(blocking=False):   # the tick's auto-retry started meanwhile
            messagebox.showinfo("Upload already running", "An upload started meanwhile -- wait for it, then try again.")
            return
        worker_ran = []

        def worker():
            worker_ran.append(True)
            results, message = None, None
            try:
                results = _retry_pending_uploads(
                    PROJECT_ROOT / "work", self.settings, slugs, force=True, only_if_not_uploaded=not allow_reupload,
                )
            except Exception as e:
                message = f"{type(e).__name__}: {e}"  # see _on_connect_youtube: never read `e` inside the lambda
            finally:
                # Released before the results are shown, so the buttons they turn back on stay on.
                _UPLOAD_RUN_LOCK.release()
            if message is not None:
                self.root.after(0, lambda: messagebox.showerror("Upload failed", message))
                self.root.after(0, self._refresh_retry_upload_options)
                return
            self.root.after(0, lambda: self._on_retry_upload_done(results))

        try:
            _set_upload_buttons(self, "disabled")
            threading.Thread(target=worker, daemon=True).start()
        except BaseException:
            if not worker_ran:          # the worker never started: nothing else will end the run
                _UPLOAD_RUN_LOCK.release()
                try:
                    _set_upload_buttons(self, "normal")
                except Exception:
                    pass
            raise

    def _on_retry_upload_done(self, results: dict) -> None:
        self._refresh_retry_upload_options()
        lines = [f"Uploaded {len(results['succeeded'])} song(s)."]
        approved = results.get("approved", [])
        waiting = [slug for slug in results.get("deferred", []) if slug not in approved]
        limit = f"today's upload limit ({self.settings.youtube_max_uploads_per_day}/day) is reached"
        if approved:
            lines.append(
                f"{len(approved)} marked verified by you (that click is your approval) and waiting because {limit}: "
                "they are now in Pending YouTube Uploads."
                + ("" if self.settings.youtube_auto_upload else " Auto-upload is off, so upload them from there once the limit resets.")
            )
        if waiting:
            lines.append(
                f"{len(waiting)} left pending -- {limit}; "
                + ("they'll upload automatically over the next few days."
                   if self.settings.youtube_auto_upload else "they stay in Pending YouTube Uploads for you to upload.")
            )
        already = results.get("already_uploaded", [])
        if already:
            lines.append(f"{len(already)} already uploaded meanwhile -- not sent again: {', '.join(already)}")
        if results["failed"]:
            lines.append(f"{len(results['failed'])} failed:")
            lines.extend(f"  {slug}: {reason}" for slug, reason in results["failed"])
        messagebox.showinfo("Retry upload results", "\n".join(lines))

    def _on_toggle_easy_chord_backfill_select_all(self) -> None:
        value = self.easy_chord_select_all_var.get()
        for var in self._easy_chord_backfill_vars.values():
            var.set(value)

    def _on_generate_selected_easy_chord_backfill(self) -> None:
        if self._running:
            return
        slugs = [slug for slug, var in self._easy_chord_backfill_vars.items() if var.get()]
        if not slugs:
            messagebox.showerror("No songs selected", "Check at least one song to generate an EASY CHORD version for.")
            return
        if not messagebox.askyesno(
            "Generate EASY CHORD Versions",
            f"Build an EASY CHORD (capo) version for {len(slugs)} song(s)?\n\n"
            "Each one reuses its own images and audio and only re-renders -- no new AI cost.",
        ):
            return

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.generate_easy_chord_backfill_button.configure(state="disabled")
        self.status_var.set(f"Generating EASY CHORD versions (0/{len(slugs)})...")
        self.progress_bar.set(0.0)
        self._clear_log()

        thread = threading.Thread(target=self._run_easy_chord_backfill_worker, args=(slugs,), daemon=True)
        thread.start()
        self.root.after(100, self._poll_queue)

    def _run_easy_chord_backfill_worker(self, slugs: list[str]) -> None:
        """Builds each selected song's EASY CHORD (capo) variant in turn -- one bad song (missing images,
        corrupt chord track) is caught and logged, never aborting the rest, same convention as every other
        batch operation in this app (_run_batch_worker above). Every render uses the owner's Settings, exactly like
        the songs' own videos (issue #7 review: they were rendered with the factory defaults -- no support overlay,
        default resolution, colors and count-in), from one snapshot taken here so a Settings change mid-run never
        splits the batch. A song that is not built is reported with the reason, never as built; memory is released
        after every song, as after every Batch song (each one is a full render in this same process)."""
        from .pipeline import easy_chord_build_problem

        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        results = {"succeeded": [], "failed": []}
        try:
            settings = replace(self.settings)
            for index, slug in enumerate(slugs, start=1):
                self._queue.put(("batch_file_start", (index, len(slugs), slug)))
                work_dir = PROJECT_ROOT / "work" / slug
                try:
                    if build_capo_variant(work_dir, settings=settings) is None:
                        # Nothing was built (its key is not settled, already easy, its audio is gone): never a success.
                        reason = easy_chord_build_problem(work_dir) or "see the log for why"
                        results["failed"].append((slug, f"not built -- {reason}"))
                        print(f"{slug}: EASY CHORD version NOT built -- {reason}")
                    else:
                        print(f"{slug}: EASY CHORD version built.")
                        results["succeeded"].append(slug)
                except Exception as e:
                    results["failed"].append((slug, f"{type(e).__name__}: {e}"))
                    print(f"{slug}: FAILED -- {type(e).__name__}: {e}")
                finally:
                    release_memory()
                self._queue.put(("batch_item_done", None))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            self._queue.put(("easy_chord_backfill_done", results))

    def _on_easy_chord_backfill_done(self, results: dict) -> None:
        self._running = False
        self.generate_button.configure(state="normal")
        self.redo_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.generate_easy_chord_backfill_button.configure(state="normal")
        self.status_var.set("Ready")
        self.progress_bar.set(1.0)
        self._invalidate_easy_chord_backfill_list()
        self._refresh_retry_upload_options()
        not_built = [(slug, reason) for slug, reason in results["failed"] if reason.startswith("not built")]
        errors = [(slug, reason) for slug, reason in results["failed"] if not reason.startswith("not built")]
        lines = [f"Built {len(results['succeeded'])} EASY CHORD version(s)."]
        if not_built:
            lines.append(f"{len(not_built)} not built:")
            lines.extend(f"  {slug}: {reason}" for slug, reason in not_built)
        if errors:
            lines.append(f"{len(errors)} failed:")
            lines.extend(f"  {slug}: {reason}" for slug, reason in errors)
        messagebox.showinfo("EASY CHORD generation results", "\n".join(lines))

    def _refresh_easy_chord_backfill_list(self) -> None:
        """Re-scans the EASY CHORD backfill checklist from the filesystem, in the background (issue #7): songs whose EASY
        CHORD version the build WILL make (settled hard key, audio on disk) and that have no current one -- no EASY video,
        or one that no longer matches its song (labelled; pipeline.easy_chord_backfill_listing). A song leaves the list
        once its EASY video exists and matches, with no separate bookkeeping, like Pending YouTube Uploads."""
        self._refresh_song_list("easy_chord_backfill")

    def _refresh_retry_upload_options(self) -> None:
        # invalidate(), not a direct rebuild: real owner complaint,
        # 2026-09-15 -- rebuilding a CLOSED, never-opened list after every
        # single upload froze the window for nobody's benefit. A closed
        # section just gets marked stale, rescanned next time it's actually
        # opened; an OPEN one re-scans in the background right away (issue #7:
        # it used to rebuild synchronously, freezing the window after every
        # Batch song). Every list whose contents a finished run or upload can
        # change is included -- Redo (a new song), Pending Engagement
        # Comments (each upload drafts one) and the EASY CHORD candidates too.
        self._invalidate_upload_list()
        if not _upload_run_active():
            # A Batch song or a tick finishing must not re-enable Upload while a manual upload run is still going (issue
            # #7 review) -- that run turns them back on itself when it ends.
            self.retry_upload_button.configure(state="normal")
            self.upload_selected_button.configure(state="normal")
        self._invalidate_pending_list()
        self._invalidate_flagged_list()
        for name in ("_invalidate_redo_list", "_invalidate_pending_comments", "_invalidate_easy_chord_backfill_list"):
            invalidate = getattr(self, name, None)
            if invalidate is not None:
                invalidate()

    # --- song lists: one Treeview each, filled from a background scan (issue #7) -------------------------------------

    def _make_song_view(self, parent, list_name: str, **kwargs) -> _SongListView:
        kwargs.setdefault("on_activate", self._on_watch_song)
        view = _SongListView(parent, **kwargs)
        view.pack(fill="x", padx=8, pady=(0, 4))
        self._song_views[list_name] = view
        return view

    def _add_watch_remove_buttons(self, parent, list_name: str) -> None:
        """The list's one Watch / Remove pair, acting on its highlighted row (the old lists had a pair on EVERY row)."""
        ctk.CTkButton(
            parent, text="✕ Remove", width=90, fg_color="gray30", hover_color="#8b2020",
            command=lambda: self._with_highlighted(list_name, lambda slug: self._on_remove_song(list_name, slug)),
        ).pack(side="right", padx=(4, 8))
        ctk.CTkButton(
            parent, text="▶ Watch", width=80, fg_color="gray30", hover_color="gray20",
            command=lambda: self._with_highlighted(list_name, self._on_watch_song),
        ).pack(side="right", padx=4)

    def _with_highlighted(self, list_name: str, action) -> None:
        view = self._song_views.get(list_name)
        slug = view.selected() if view is not None else None
        if not slug:
            messagebox.showinfo("No song highlighted", "Click a song in the list first.")
            return
        action(slug)

    def _refresh_song_list(self, list_name: str) -> None:
        """Starts a background re-scan of one song list (by its dismissed_songs.py list_name) -- its rows are replaced once
        the scan finishes; until then the old rows (or "Loading...") stay up and the window keeps responding."""
        scans = {
            "redo": self._scan_redo_list, "upload": self._scan_upload_list, "pending": self._scan_pending_list,
            "easy_chord_backfill": self._scan_easy_chord_backfill_list, "flagged": self._scan_flagged_list,
        }
        applies = {
            "redo": self._show_redo_list, "upload": self._show_upload_list, "pending": self._show_pending_list,
            "easy_chord_backfill": self._show_easy_chord_backfill_list, "flagged": self._show_flagged_list,
        }
        if self._closing:
            return
        view = self._song_views.get(list_name)
        if view is not None and not view.loaded:
            view.set_status("Loading...")
        self._start_list_load(list_name, scans[list_name], applies[list_name])

    def _start_list_load(self, name: str, scan, apply) -> None:
        """Runs `scan()` -- the list_* filesystem walks, which read every song's lyrics_timed.json and can take many
        seconds on the owner's box -- on the list scanner thread, and hands its result to `apply(result)` back on the Tk
        thread. Scans run one at a time on ONE thread (two lists' scans never write the same song's timing hold at once).
        A newer request for the same list supersedes an older one: a queued stale scan is skipped and a stale result is
        dropped, so a burst of refreshes costs one extra scan, not one each."""
        if self._closing:
            return
        gen = self._list_gen.get(name, 0) + 1
        self._list_gen[name] = gen
        self._list_pending[name] = gen
        with self._list_lock:
            self._list_jobs.append((name, gen, scan, apply))
            start = not self._list_worker_running
            if start:
                self._list_worker_running = True
        if start:
            # NOT a daemon: closing the app mid-scan lets that scan finish, rather than killing it halfway through
            # rewriting a song's lyrics_timed.json (a timing hold); _closing stops anything queued behind it.
            self._list_worker_thread = threading.Thread(target=self._list_worker_loop, args=(), daemon=False)
            self._list_worker_thread.start()
        self._schedule_list_poll()

    def _list_worker_loop(self) -> None:
        while True:
            with self._list_lock:
                if not self._list_jobs or self._closing:
                    self._list_jobs.clear()
                    self._list_worker_running = False
                    return
                name, gen, scan, apply = self._list_jobs.popleft()
            if gen != self._list_gen.get(name):
                continue  # a newer refresh of this list is queued behind it
            try:
                self._list_results.put((name, gen, apply, scan(), None))
            except Exception as e:
                self._list_results.put((name, gen, apply, None, f"{type(e).__name__}: {e}"))
            # The Tk thread owns the widgets these reach; never be the one left holding them (a Tk object freed on
            # this thread would make a Tcl call from the wrong thread).
            del scan, apply

    def _schedule_list_poll(self) -> None:
        if not self._list_poll_scheduled:
            self._list_poll_scheduled = True
            self.root.after(40, self._poll_list_results)

    def _poll_list_results(self) -> None:
        self._list_poll_scheduled = False
        while True:
            try:
                name, gen, apply, result, error = self._list_results.get_nowait()
            except queue.Empty:
                break
            if gen != self._list_gen.get(name):
                continue  # superseded by a newer refresh still running
            self._list_pending.pop(name, None)
            view = self._song_views.get(name)
            if error is not None:
                print(f"WARNING: could not load the {name} list: {error}", file=sys.stderr)
                if view is not None:
                    view.set_status(f"Could not load this list: {error}")
                continue
            try:
                apply(result)
            except Exception as e:
                # One bad list must never break the others (or leave "Loading..." up forever with no visible reason).
                print(f"WARNING: could not show the {name} list: {type(e).__name__}: {e}", file=sys.stderr)
                if view is not None:
                    view.set_status(f"Could not show this list: {type(e).__name__}: {e}")
        if self._list_pending and not self._closing:
            self._schedule_list_poll()

    def _list_loads_busy(self) -> bool:
        """True while a list scan has been asked for and its rows are not on screen yet."""
        return bool(self._list_pending)

    def _scan_redo_list(self) -> dict:
        return {"songs": _not_dismissed("redo", list_redoable_songs(PROJECT_ROOT / "work"))}

    def _scan_upload_list(self) -> dict:
        songs, note = self._uploadable_songs()
        songs = _not_dismissed("upload", songs)
        return {"songs": songs, "labels": {s: self._song_label("upload", s) for s in songs}, "note": note}

    def _scan_pending_list(self) -> dict:
        return {"songs": _not_dismissed("pending", list_pending_uploads(PROJECT_ROOT / "work"))}

    def _scan_easy_chord_backfill_list(self) -> dict:
        """The EASY CHORD list's rows (on the list scanner thread): the songs, a label for one whose EASY version is out of
        date or has no video, and a note counting the hard-key songs left out only because their key is not settled."""
        from .pipeline import easy_chord_backfill_listing

        listing = easy_chord_backfill_listing(PROJECT_ROOT / "work")
        waiting = listing.waiting_for_key
        return {
            "songs": _not_dismissed("easy_chord_backfill", listing.songs),
            "labels": listing.labels,
            "note": f"{waiting} more wait for a settled key" if waiting else "",
        }

    def _show_redo_list(self, data: dict) -> None:
        self._song_views["redo"].set_songs(data["songs"])
        if self.redo_song_var.get() not in data["songs"]:
            self.redo_song_var.set("")  # a Redo list never starts with a song picked for you

    def _show_upload_list(self, data: dict) -> None:
        songs = data["songs"]
        self._song_views["upload"].set_songs(songs, labels=data.get("labels"), note=data.get("note", ""))
        if self.retry_upload_song_var.get() not in songs:
            self.retry_upload_song_var.set(songs[0] if songs else "")

    def _show_pending_list(self, data: dict) -> None:
        view = self._song_views["pending"]
        view.set_songs(data["songs"], default_checked=self.pending_select_all_var.get())
        self._pending_upload_vars = {slug: _TickFlag(view, slug) for slug in data["songs"]}

    def _show_easy_chord_backfill_list(self, data: dict) -> None:
        view = self._song_views["easy_chord_backfill"]
        view.set_songs(data["songs"], labels=data.get("labels"), note=data.get("note", ""),
                       default_checked=self.easy_chord_select_all_var.get())
        self._easy_chord_backfill_vars = {slug: _TickFlag(view, slug) for slug in data["songs"]}

    def _refresh_pending_uploads_list(self) -> None:
        """Re-scans the Pending Uploads checklist from the filesystem (never
        cached) -- schedule_upload() only writes youtube_state.json AFTER a
        successful upload, so a song simply stops appearing here once it
        succeeds, with no separate bookkeeping needed for "leaving the list"
        (owner request, 2026-09-15). The scan runs in the background (issue #7)."""
        self._refresh_song_list("pending")

    def _drop_list_row(self, list_name: str, slug: str) -> None:
        """Takes one song off an open list after Remove -- just that row, no rescan (removal is per-list, owner request
        2026-09-15: a song dismissed from Pending Uploads is still reachable via Upload to YouTube)."""
        view = self._song_views.get(list_name)
        if view is not None:
            view.remove(slug)
        if list_name == "pending":
            self._pending_upload_vars.pop(slug, None)
        elif list_name == "easy_chord_backfill":
            self._easy_chord_backfill_vars.pop(slug, None)
        elif list_name == "flagged":
            self._flagged_rows.pop(slug, None)

    def _on_watch_song(self, slug: str) -> None:
        video_path = song_video_path(PROJECT_ROOT / "work" / slug)
        if video_path is None:
            messagebox.showerror("No video yet", f'"{slug}" has not been rendered yet.')
            return
        try:
            _open_with_default_app(video_path)
        except Exception as e:
            messagebox.showerror("Could not open video", f"{type(e).__name__}: {e}")

    def _on_remove_song(self, list_name: str, slug: str) -> None:
        if not messagebox.askyesno(
            "Remove from list",
            f'Remove "{slug}" from this list?\n\n'
            "This only changes what shows up here -- the song's files aren't "
            "touched, and it can still be found in this app's other lists.",
        ):
            return
        dismiss_song(list_name, slug)
        self._drop_list_row(list_name, slug)

    def _build_youtube_panel(self, parent) -> None:
        """YouTube Comments: drafted replies waiting for the owner. A section that starts CLOSED like the song lists
        (issue #7: every draft used to be built at launch -- a CTkTextbox each, whose scrollbars force a whole-window
        layout pass -- slowing both launch and close), with the number waiting shown on its header."""
        self.youtube_replies_count_var = tk.StringVar(value="")

        def _header_extra(header) -> None:
            ctk.CTkButton(header, text="Check Now", command=self._on_check_youtube_comments, width=100).pack(
                side="right", padx=8)
            ctk.CTkLabel(header, textvariable=self.youtube_replies_count_var, text_color="#f0a339").pack(side="right")

        content, self._invalidate_pending_replies = self._make_collapsible_section(
            parent, "YouTube Comments", header_extra=_header_extra, on_first_expand=self._render_pending_replies,
        )
        self.replies_view = _DraftQueueView(
            content, empty_text="(no replies waiting)",
            on_approve=lambda reply, box: self._on_approve_reply(reply, box),
            on_dismiss=lambda reply: self._on_dismiss_reply(reply),
        )
        self.replies_view.pack(fill="x", padx=8, pady=(0, 8))
        try:
            self._show_replies_count(len(load_pending_replies()))
        except Exception:
            pass  # a missing/corrupt drafts file must never stop the window opening
        self.root.after(20 * 60 * 1000, self._schedule_youtube_comment_check)

    def _show_replies_count(self, count: int) -> None:
        self.youtube_replies_count_var.set(f"{count} waiting" if count else "")

    def _on_pending_replies_changed(self) -> None:
        """New drafts arrived (comment check): update the count; an open section adds just the new rows, a closed one
        picks them up when opened. The owner's edits are never touched."""
        try:
            self._show_replies_count(len(load_pending_replies()))
        except Exception:
            pass
        self._invalidate_pending_replies()

    def _render_pending_replies(self) -> None:
        """Brings the reply list in line with the saved drafts. Only list rows are added or dropped: every edit the owner
        has made is kept per comment (issue #7 review -- a full rebuild used to reset every reply box to Claude's draft on
        every 20-minute comment check and every other Approve/Dismiss, so Approve could post the wrong text)."""
        replies = load_pending_replies()
        rows = []
        for reply in replies:
            badge = "⚠ " if reply.is_error_report else ""
            video = reply.video_title or "(unknown video)"   # owner, 2026-10-03: show which song/video this is on
            heading = f"{video} -- {reply.author}: {reply.comment_text}" + (
                " ⚠ possible error report" if reply.is_error_report else "")
            preview = f"[{video}] {_one_line(reply.comment_text)}"
            rows.append((reply.comment_id, reply, f"{badge}{reply.author}", preview, heading, reply.draft_reply))
        self.replies_view.set_items(rows)
        self._show_replies_count(len(replies))

    def _on_approve_reply(self, reply: PendingReply, text_box) -> None:
        key = f"reply:{reply.comment_id}"
        if _is_posting(self, key):
            return  # already being posted (or posted) -- a second click must never post it twice
        if not self._confirm_quota_override_if_blocked():
            return
        if not _claim_posting(self, key):
            return
        text = text_box.get("1.0", "end").strip()

        def post(youtube_client) -> bool:
            try:
                post_reply(youtube_client, reply.comment_id, text)
            except Exception as e:
                if is_quota_exceeded_error(e):
                    save_quota_blocked_until(
                        datetime.now().astimezone() + timedelta(hours=self.settings.youtube_quota_retry_hours)
                    )
                message = f"{type(e).__name__}: {e}"  # see _on_connect_youtube: never read `e` inside the lambda
                self.root.after(0, lambda: messagebox.showerror("Could not post reply", message))
                return False
            try:
                remove_pending_reply(reply.comment_id)
            except Exception as e:
                print(f"WARNING: posted, but could not take the reply off the list: {type(e).__name__}: {e}",
                      file=sys.stderr)
            self.root.after(0, self._render_pending_replies)
            return True

        _post_draft_in_background(self, key, "replies_view", reply.comment_id, post)

    def _on_dismiss_reply(self, reply: PendingReply) -> None:
        if _is_posting(self, f"reply:{reply.comment_id}"):
            return  # being posted right now
        remove_pending_reply(reply.comment_id)
        self._render_pending_replies()

    def _build_pending_comments_panel(self, parent) -> None:
        content, self._invalidate_pending_comments = self._make_collapsible_section(
            parent, "Pending Engagement Comments", on_first_expand=self._refresh_pending_comments,
        )
        self.pending_comments_view = _DraftQueueView(
            content,
            on_approve=lambda comment, box: self._on_approve_comment(comment, box),
            on_dismiss=lambda comment: self._on_dismiss_comment(comment),
            empty_text="(no comments waiting)",
        )
        self.pending_comments_view.pack(fill="x", padx=8, pady=(0, 8))

    def _refresh_pending_comments(self) -> None:
        """Same keep-the-edits refresh as _render_pending_replies (one list + one editor, issue #7): only list rows for
        comments that are gone are dropped and new ones added, so an edit in progress survives an Approve/Dismiss of
        another comment or a new upload's draft arriving."""
        rows = [
            (comment.video_id, comment, comment.song_title, _one_line(comment.draft_text), comment.song_title,
             comment.draft_text)
            for comment in load_pending_comments()
        ]
        self.pending_comments_view.set_items(rows)

    def _on_approve_comment(self, comment: PendingComment, text_box) -> None:
        key = f"comment:{comment.video_id}"
        if _is_posting(self, key):
            return  # already being posted (or posted) -- a second click must never post it twice
        if not self._confirm_quota_override_if_blocked():
            return
        if not _claim_posting(self, key):
            return
        text = text_box.get("1.0", "end").strip()

        def post(youtube_client) -> bool:
            try:
                if not is_video_public(youtube_client, comment.video_id):
                    self.root.after(0, lambda: messagebox.showinfo(
                        "Video not public yet",
                        "This video is still scheduled/private on YouTube, so comments can't be "
                        "posted to it yet. Try Approve again after it publishes.",
                    ))
                    return False
                thread_id = post_top_level_comment(youtube_client, comment.video_id, text)
            except Exception as e:
                if is_quota_exceeded_error(e):
                    save_quota_blocked_until(
                        datetime.now().astimezone() + timedelta(hours=self.settings.youtube_quota_retry_hours)
                    )
                # See _on_connect_youtube: never read `e` inside the lambda.
                message = f"{type(e).__name__}: {e}"
                self.root.after(0, lambda: messagebox.showerror("Could not post comment", message))
                return False
            # Posted: from here on nothing may make it look unposted (a retry would post it twice).
            try:
                if thread_id:
                    # The channel's own comment comes back from the next comment check as a "new" one; seen now, it is
                    # never drafted a reply to itself (issue #7 review, F116).
                    mark_comment_seen(str(thread_id))
                remove_pending_comment(comment.video_id)
                _mark_engagement_comment_posted(comment.video_id)
            except Exception as e:
                print(f"WARNING: posted, but could not record it: {type(e).__name__}: {e}", file=sys.stderr)
            self.root.after(0, self._invalidate_pending_comments)
            self.root.after(0, lambda: messagebox.showinfo(
                "Comment posted",
                "Posted. Remember to pin it from YouTube Studio -- the API has no way to do that part.",
            ))
            return True

        _post_draft_in_background(self, key, "pending_comments_view", comment.video_id, post)

    def _on_dismiss_comment(self, comment: PendingComment) -> None:
        if _is_posting(self, f"comment:{comment.video_id}"):
            return  # being posted right now
        remove_pending_comment(comment.video_id)
        try:
            # Dismissed means "no engagement comment on this video": recorded, so a later organize/backfill run does not
            # pay Claude for a new draft and queue it again (issue #7 review; youtube_state.mark_engagement_comment_posted).
            _mark_engagement_comment_posted(comment.video_id)
        except Exception as e:
            print(f"WARNING: could not record the dismissed comment: {type(e).__name__}: {e}", file=sys.stderr)
        self._invalidate_pending_comments()

    def _build_flagged_songs_panel(self, parent) -> None:
        """Flagged for Lyrics Review (issue #7): a song list (one Treeview, with a short reason per song) above ONE detail
        pane, built once, showing the highlighted song's concern and its action buttons -- enabled or disabled for that
        song. It used to build ~32 widgets per flagged song, over 4,000 for the owner's library."""
        content, self._invalidate_flagged_list = self._make_collapsible_section(
            parent, "Flagged for Lyrics Review", on_first_expand=self._refresh_flagged_songs,
        )
        self._flagged_rows: dict[str, dict] = {}
        self._make_song_view(
            content, "flagged", height_px=230, extra_column=True, on_select=self._show_flagged_details,
            on_activate=lambda slug: self._on_flagged_watch_or_play(slug),
        )
        self._build_flagged_details(content)

    def _build_flagged_details(self, parent) -> None:
        detail = ctk.CTkFrame(parent)
        detail.pack(fill="x", padx=8, pady=(0, 8))
        self._flagged_detail_frame = detail
        self._flagged_title_label = ctk.CTkLabel(detail, text="", anchor="w", font=ctk.CTkFont(weight="bold"))
        self._flagged_title_label.pack(fill="x", padx=6, pady=(6, 2))
        self._flagged_concern_label = ctk.CTkLabel(
            detail, text="Click a song above to see why it is here.", anchor="w", wraplength=440, justify="left",
        )
        self._flagged_concern_label.pack(fill="x", padx=6, pady=(0, 4))
        self._flagged_note_label = ctk.CTkLabel(
            detail, text="", anchor="w", text_color="gray60", wraplength=440, justify="left",
        )
        self._flagged_note_label.pack(fill="x", padx=6, pady=(0, 4))

        def button(row, key, text, width, handler_name, **colors):
            # The handler is looked up when clicked, by name, so the existing _on_*_flagged methods stay the single place
            # each action lives.
            colors = colors or {"fg_color": "gray30", "hover_color": "gray20"}
            b = ctk.CTkButton(row, text=text, width=width, state="disabled",
                              command=lambda: self._with_flagged_song(getattr(self, handler_name)), **colors)
            b.pack(side="left", padx=(0, 6))
            self._flagged_buttons[key] = b

        self._flagged_buttons: dict[str, ctk.CTkButton] = {}
        green = {"fg_color": "#2b7a3d", "hover_color": "#236232"}
        # Rows of at most four buttons: one long row got its last buttons cropped in a narrower window (owner, 2026-09-21).
        row1 = ctk.CTkFrame(detail, fg_color="transparent")
        row1.pack(fill="x", padx=6, pady=(0, 4))
        button(row1, "watch", "▶ Watch", 90, "_on_flagged_watch_or_play")
        button(row1, "whisper", "Whisper Text", 110, "_on_whisper_text_flagged")
        button(row1, "edit_lyrics", "✎ Edit Lyrics", 100, "_on_edit_lyrics_flagged", fg_color=None, hover_color=None)
        button(row1, "redo", "Redo", 70, "_on_redo_flagged", fg_color=None, hover_color=None)
        row2 = ctk.CTkFrame(detail, fg_color="transparent")
        row2.pack(fill="x", padx=6, pady=(0, 4))
        button(row2, "render_anyway", "Render Anyway", 110, "_on_render_anyway_flagged", **green)
        button(row2, "mark_verified", "✔ Mark Verified", 120, "_on_mark_verified", **green)
        button(row2, "upload_anyway", "Upload Anyway", 110, "_on_upload_anyway_flagged")
        button(row2, "remove", "✕ Remove", 90, "_on_remove_flagged")
        row3 = ctk.CTkFrame(detail, fg_color="transparent")
        row3.pack(fill="x", padx=6, pady=(0, 6))
        button(row3, "set_key", "Set Key", 90, "_on_set_key_flagged", **green)
        button(row3, "rebuild_easy", "Rebuild EASY version", 160, "_on_rebuild_easy_flagged", **green)

    def _with_flagged_song(self, action) -> None:
        view = self._song_views.get("flagged")
        slug = view.selected() if view is not None else None
        if slug:
            action(slug)

    def _on_flagged_watch_or_play(self, slug: str) -> None:
        """Watch the video -- or, for a song held before its video, play its MP3 (the only way to hear it; 2026-09-22)."""
        row = self._flagged_rows.get(slug, {})
        if row.get("has_video", True):
            self._on_watch_song(slug)
        else:
            self._on_play_mp3_flagged(slug)

    def _show_flagged_details(self, slug: str | None) -> None:
        """Fills the detail pane for the highlighted song and turns each action on or off for it. An EASY CHORD version
        ("<song>/easychords") is made FROM its song -- its lyrics, timing and key all come from there -- so it never gets
        Redo, Render Anyway, Set Key or Edit Lyrics: each of those ran the whole pipeline (or saved a key) on the capo folder
        itself, dropping the CAPO badge, labelling shape chords with the original key, even nesting a second capo version
        (issue #7 review). It gets Rebuild EASY version instead, which remakes it from its song."""
        row = self._flagged_rows.get(slug) if slug else None
        buttons = self._flagged_buttons
        if row is None:
            self._flagged_title_label.configure(text="")
            self._flagged_concern_label.configure(text="Click a song above to see why it is here.")
            self._flagged_note_label.configure(text="")
            for b in buttons.values():
                b.configure(state="disabled")
            return
        easy, has_video, uploaded = row["is_easy"], row["has_video"], row["uploaded"]
        notes = []
        if uploaded:
            notes.append("Already on YouTube -- replacing it there is your call.")
        if not has_video:
            notes.append("Held before the video -- no video was made.")
        if easy:
            notes.append(
                f'EASY CHORD version of "{row["parent"]}": its lyrics, timing and key come from that song. Fix them there '
                "(Edit Lyrics / Redo / Set Key on that song), then use Rebuild EASY version here."
            )
        self._flagged_title_label.configure(text=slug)
        self._flagged_concern_label.configure(text=row["concern"] or "(no concern text saved)")
        self._flagged_note_label.configure(text="\n".join(notes))
        enabled = {
            "watch": True,
            "whisper": not easy,
            "edit_lyrics": not easy,
            "redo": not easy,
            "render_anyway": not easy and not has_video,
            "mark_verified": has_video and not uploaded,        # an uploaded song would be a duplicate video
            "upload_anyway": has_video and not uploaded and not _upload_run_active(),   # one upload run at a time
            "remove": True,
            "set_key": not easy and row["key_waiting"],
            "rebuild_easy": easy,
        }
        buttons["watch"].configure(text="▶ Watch" if has_video else "▶ Play MP3")
        for key, b in buttons.items():
            b.configure(state="normal" if enabled[key] else "disabled")

    def _refresh_flagged_songs(self) -> None:
        """Re-scans Flagged for Lyrics Review in the background (issue #7)."""
        self._refresh_song_list("flagged")

    def _scan_flagged_list(self) -> dict:
        slugs = self._visible_flagged_songs()
        rows = {}
        for slug in slugs:
            try:
                rows[slug] = self._flagged_row_data(slug)
            except Exception as e:
                # One unreadable song must never blank the whole list (2026-09-15: a list once showed nothing at all).
                print(f"WARNING: could not read flagged song {slug!r}: {type(e).__name__}: {e}", file=sys.stderr)
                rows[slug] = {"concern": f"Could not read this song: {type(e).__name__}: {e}", "uploaded": False,
                              "key_waiting": False, "has_video": False, "is_easy": _is_easy_variant(slug),
                              "parent": _easy_variant_parent(slug), "reason": "unreadable"}
        return {"songs": slugs, "rows": rows}

    def _show_flagged_list(self, data: dict) -> None:
        self._flagged_rows = data["rows"]
        view = self._song_views["flagged"]
        view.set_songs(data["songs"], extras={slug: row["reason"] for slug, row in data["rows"].items()})
        self._show_flagged_details(view.selected())

    def _visible_flagged_songs(self) -> list[str]:
        """The songs Flagged for Lyrics Review lists: every flagged song except the ones the owner removed."""
        removed = load_dismissed("flagged")
        return [s for s in list_flagged_songs(PROJECT_ROOT / "work", include_uploaded=True) if s not in removed]

    def _on_remove_flagged(self, slug: str) -> None:
        """Takes a song out of Flagged for Lyrics Review (owner, 2026-09-21: "remove not delete" -- some songs are too hard to
        fix). Only hides it: nothing on disk is touched, the Batch leaves it alone, and a Redo brings it back."""
        if not messagebox.askyesno(
            "Remove from review",
            f'Remove "{slug}" from Flagged for Lyrics Review?\n\nNothing is deleted: the song\'s files stay where they are '
            "and the Batch will skip it. Redo it from the Redo list to bring it back.",
        ):
            return
        dismiss_song("flagged", slug)
        self._drop_list_row("flagged", slug)  # just that row -- no rescan of the whole list (issue #7)

    def _flagged_row_data(self, slug: str) -> dict:
        """Everything the Flagged detail pane shows for one song -- file reads only, so it runs on the list scanner's
        background thread (issue #7), never the Tk thread."""
        song_dir = PROJECT_ROOT / "work" / slug
        concern = ""
        try:
            # The reason as it stands at the CURRENT pass mark: the lists no longer write the timing verdict into the file,
            # so the stored concern can be empty or name an old bar (wave-1 scan-speed request).
            concern = review_concern(song_dir)
        except Exception:
            pass
        lyrics_concern = bool(concern)
        easy = _is_easy_variant(slug)
        parent = _easy_variant_parent(slug)
        uploaded = _state_file_exists(song_dir)       # an unreadable youtube_state.json still means uploaded (F106)
        # A song already on YouTube is listed only when its EASY CHORD version waits for THIS song's key
        # (pipeline.easy_version_waits_on_key): Set Key here settles it, then Rebuild EASY version.
        easy_waits = uploaded and not easy and easy_version_waits_on_key(song_dir)
        key_waiting = (not uploaded and key_state(song_dir) != "confirmed") or easy_waits
        if easy_waits:
            key_concern = (
                f"{KEY_HOLD_PREFIX} its EASY CHORD version waits for this song's key -- Set Key, then Rebuild EASY version."
            )
            concern = "\n".join(part for part in (concern, key_concern) if part).strip()
        elif key_waiting:
            # An EASY CHORD version's key IS its song's key (key_decision reads the parent's decision for it).
            decision = load_decision(song_dir.parent if easy else song_dir)
            if decision:
                key_concern = decision.concern()
            elif easy:
                key_concern = f'{KEY_HOLD_PREFIX} the key of "{parent}" has not been checked yet.'
            else:
                key_concern = (
                    f"{KEY_HOLD_PREFIX} this song's key has not been checked yet. Set Key to confirm it, then make the video again."
                )
            concern = "\n".join(part for part in (concern, key_concern) if part).strip()
        has_video = song_video_path(song_dir) is not None
        reasons = []
        if not has_video:
            reasons.append("no video yet")
        if easy_waits:
            reasons.append("key (EASY waits)")
        elif key_waiting:
            reasons.append("key")
        if lyrics_concern:
            reasons.append("lyrics/timing")
        if uploaded:
            reasons.append("on YouTube")
        if easy:
            reasons.append("EASY")
        return {
            "concern": concern, "uploaded": uploaded, "key_waiting": key_waiting, "has_video": has_video,
            "is_easy": easy, "parent": parent, "reason": ", ".join(reasons), "easy_waits": easy_waits,
        }

    def _settle_key_of_uploaded_song(self, slug: str, song_dir: Path, key: str, shown_key: str) -> None:
        """Set Key on a song already on YouTube (listed because its EASY CHORD version waits for this key): the key goes
        onto the saved chords and is CONFIRMED at once (key_decision.apply_saved_owner_key) -- its video is never made
        again (that would be a second video of an uploaded song). Then offers Rebuild EASY version."""
        from .key_decision import apply_saved_owner_key
        from .key_estimate import parse_key
        from .models import save_song

        timed = song_dir / "lyrics_timed.json"
        prefer_flats = getattr(getattr(self, "settings", None), "prefer_flats", True)
        try:
            song = load_song(timed)
            if apply_saved_owner_key(song_dir, song, prefer_flats=prefer_flats):
                save_song(song, timed)
        except Exception as e:
            messagebox.showerror("Could not settle the key", f"{type(e).__name__}: {e}")
            return
        for name in ("_invalidate_flagged_list", "_invalidate_pending_list", "_invalidate_upload_list",
                     "_invalidate_easy_chord_backfill_list"):
            invalidate = getattr(self, name, None)
            if invalidate is not None:
                invalidate()
        differs = parse_key(shown_key) is not None and parse_key(shown_key) != parse_key(key)
        note = (f"\n\nIts video on YouTube shows {shown_key}; it is left as it is (scripts/add_key_note.py can add a note "
                "to its description).") if differs else ""
        if (song_dir / "easychords").is_dir() and messagebox.askyesno(
            "Key confirmed",
            f'"{slug}" is set to {key}.{note}\n\nRebuild its EASY CHORD version now? (No new AI cost.)',
        ):
            self._on_rebuild_easy_flagged(f"{slug}/easychords", confirm=False)
        elif not (song_dir / "easychords").is_dir():
            messagebox.showinfo("Key confirmed", f'"{slug}" is set to {key}.{note}')

    def _on_set_key_flagged(self, slug: str) -> None:
        """Set Key (owner, 2026-09-26): the owner's own answer for a song whose key was not settled by the chords and the second
        opinion agreeing. Saved as key_owner.json (it always wins, and survives a Redo). When the saved chords already carry
        that key, the key is CONFIRMED at once (key_decision.confirm_owner_key -- issue #7 review, F013: agreeing with the
        chords, the most common answer, used to leave the song held for its key for good), and a video that already exists
        needs no remaking. Otherwise the owner can make the video now; the render puts the key on the chords and confirms it.
        While a job runs it says so instead of silently doing nothing."""
        from .key_decision import confirm_owner_key
        from .key_estimate import parse_key

        if self._running:
            messagebox.showinfo("Set Key", "A job is running. Set the key when it has finished.")
            return
        if _refuse_easy_variant(slug, "Set Key"):
            return
        song_dir = PROJECT_ROOT / "work" / slug
        decision = load_decision(song_dir)
        guess, options, chords = "", "", None
        try:
            chords = load_song(song_dir / "lyrics_timed.json").chord_track
            estimate = estimate_key_from_chords(chords)
            guess = estimate.name if estimate else ""
            options = ", ".join(candidate_keys(chords))
        except Exception:
            pass
        said = f"The chords say {decision.chord_key}. The second opinion says {decision.published_key or 'nothing'}.\n\n" if decision else ""
        typed = simpledialog.askstring(
            "Set the song's key",
            f'{said}What key is "{slug}" in (the original key of the song)?\nLikely keys: {options or "unknown"}\n\n'
            "Type it like: D major   or   F# minor",
            initialvalue=guess, parent=self.root,
        )
        if typed is None:
            return
        try:
            key = save_owner_key(song_dir, typed)
        except ValueError as e:
            messagebox.showerror("Not a key", f"{e}\n\nNothing was saved. Use Set Key again and type a key like D major or F# minor.")
            return
        except OSError as e:
            messagebox.showerror("Could not save the key", f"{type(e).__name__}: {e}")
            return
        if _state_file_exists(song_dir):
            self._settle_key_of_uploaded_song(slug, song_dir, key, chords.key if chords is not None else "")
            return
        shown_key = chords.key if chords is not None else ""
        confirmed_now = parse_key(shown_key) is not None and parse_key(shown_key) == parse_key(key)
        if confirmed_now:
            try:
                confirm_owner_key(song_dir, chords)
            except OSError as e:
                messagebox.showerror("Could not save the key", f"{type(e).__name__}: {e}")
                return
        for name in ("_invalidate_flagged_list", "_invalidate_pending_list", "_invalidate_upload_list",
                     "_invalidate_easy_chord_backfill_list"):
            invalidate = getattr(self, name, None)
            if invalidate is not None:
                invalidate()
        has_video = song_video_path(song_dir) is not None
        if confirmed_now and has_video:
            messagebox.showinfo(
                "Key confirmed", f'"{slug}" is set to {key}, the key its video already shows -- no need to make it again.',
            )
            return
        stale = f"\n\nIts current video shows {shown_key or 'another key'}, so it has to be made again before it can upload." if has_video else ""
        if messagebox.askyesno(
            "Make the video?",
            f'"{slug}" is set to {key}.{stale}\n\nMake the video now with this key? (Images already bought are reused.)',
        ):
            self._on_render_anyway_flagged(slug, confirm=False)

    def _on_edit_lyrics_flagged(self, slug: str) -> None:
        self._open_lyrics_editor(slug)

    def _on_play_mp3_flagged(self, slug: str) -> None:
        """Hands the original song audio off to the OS's default player -- the only way to hear a song held
        before its video was ever rendered (owner request, 2026-09-22; a rendered song has Watch instead)."""
        try:
            audio_path, _title = load_redo_inputs(PROJECT_ROOT / "work" / slug)
        except Exception as e:
            messagebox.showerror("Could not find the audio", f"{type(e).__name__}: {e}")
            return
        if not audio_path.exists():
            messagebox.showerror("No audio file", f'"{slug}"\'s audio file could not be found at {audio_path}.')
            return
        try:
            _open_with_default_app(audio_path)
        except Exception as e:
            messagebox.showerror("Could not open the audio", f"{type(e).__name__}: {e}")

    def _on_whisper_text_flagged(self, slug: str) -> None:
        """A popup showing what Whisper heard sung, one row per LYRIC line (owner request, 2026-09-22: "make the
        whisper text line by line like the lyrics text ... would make it a lot easier to figure out"), EDITABLE
        since 2026-09-27 (owner request, confirmed against real evidence -- Boris the Spider: the fetched/edited
        lyrics were exactly right while Whisper genuinely mis-transcribed or skipped several passages, scoring
        correctly-timed lines as "out of sync" purely because Whisper's own guess at the words was wrong there).
        Save Corrections stores only the rows actually changed (owner_whisper.save_owner_whisper_line), read by
        the sync check and this same popup's own next opening on a later Redo (owner_whisper.corrected_heard_words/
        owner_whisper_corrections) -- never the whole box, so an untouched row is never treated as a correction.
        Every currently flagged song already has a cached transcript, so whisper_lines_for() returns instantly; a
        rare older song without one is transcribed fresh (~70s) off the GUI thread so the window never freezes."""
        work_dir = PROJECT_ROOT / "work" / slug
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Whisper text -- {slug}")
        dialog_w, dialog_h = 600, 500
        self.root.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(x, 0)}+{max(y, 0)}")
        dialog.transient(self.root)
        dialog.lift()
        dialog.focus_force()
        dialog.attributes("-topmost", True)
        dialog.after(300, lambda: dialog.attributes("-topmost", False))

        ctk.CTkLabel(
            dialog, anchor="w", wraplength=dialog_w - 40, justify="left", text_color="gray60",
            text="What Whisper (speech recognition) heard sung, one row per lyric line, for reading side-by-side "
                 "against the lyrics in Edit Lyrics. Whisper sometimes mishears or misses a line entirely -- if a "
                 "line here is wrong but you're sure of the real words, correct just that row and Save Corrections; "
                 "an untouched row is left exactly as Whisper heard it.",
        ).pack(fill="x", padx=14, pady=(12, 6))
        box = ctk.CTkTextbox(dialog, wrap="word", font=ctk.CTkFont(size=14))
        box.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        box.insert("1.0", "Loading... (transcribing fresh audio can take about a minute)")
        box.configure(state="disabled")
        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=14, pady=(0, 12))
        # Populated once the real rows load (worker/show_in_box below); a Save click before then has nothing to
        # compare against, so it's refused rather than guessing.
        loaded: dict[str, list[str] | None] = {"shown": None, "lyric_texts": None}

        def save_corrections() -> None:
            if loaded["shown"] is None:
                messagebox.showerror("Not ready yet", "Still loading the Whisper text -- try again in a moment.")
                return
            shown, lyric_texts = loaded["shown"], loaded["lyric_texts"]
            edited = box.get("1.0", "end-1c").split("\n")
            if len(edited) != len(shown):
                messagebox.showerror(
                    "Could not save",
                    "A row seems to have been added or removed while editing, so the rows no longer line up with "
                    "their own lyric lines. Undo the extra line break (Ctrl+Z) and try again.",
                )
                return
            changed = 0
            for row, (old_text, new_text) in enumerate(zip(shown, edited)):
                if new_text.strip() != old_text.strip():
                    save_owner_whisper_line(work_dir, row, new_text, lyric_texts[row])
                    changed += 1
            self._invalidate_flagged_list()
            messagebox.showinfo(
                "Saved" if changed else "Nothing to save",
                f"Saved {changed} correction{'s' if changed != 1 else ''}. The sync check will use it on the next "
                "Redo or Render Anyway." if changed else "No rows had changed.",
            )

        ctk.CTkButton(buttons, text="Save Corrections", width=140, command=save_corrections).pack(side="left")
        ctk.CTkButton(
            buttons, text="Close", width=80, fg_color="gray30", hover_color="gray20", command=dialog.destroy,
        ).pack(side="right")

        def show_in_box(text: str, shown: list[str] | None = None, lyric_texts: list[str] | None = None) -> None:
            if not dialog.winfo_exists():
                return  # the owner closed the popup before a fresh transcription finished
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", text)
            loaded["shown"], loaded["lyric_texts"] = shown, lyric_texts  # None on an error: Save stays refused

        def worker():
            try:
                lines = whisper_lines_for(work_dir)
                lyric_texts = current_lyric_line_texts(work_dir)
                corrections = owner_whisper_corrections(work_dir)
            except Exception as e:
                # Formatted here, not inside the lambda -- `e` is unbound once this except block ends.
                message = f"Could not get the Whisper text: {type(e).__name__}: {e}"
                self.root.after(0, lambda: show_in_box(message))
                return
            shown = [corrections.get(i, line) for i, line in enumerate(lines)]
            self.root.after(0, lambda: show_in_box("\n".join(shown), shown, lyric_texts))

        threading.Thread(target=worker, daemon=True).start()

    def _save_owner_lyrics_from_editor(self, slug: str, text: str) -> bool:
        """Saves the owner's edited lyrics (lyrics_owner.txt) for the next Redo. False (with a message) if the text
        is empty -- an accidental select-all-delete must never wipe a song's lyrics."""
        try:
            save_owner_lyrics(PROJECT_ROOT / "work" / slug, text)
        except (ValueError, OSError) as e:
            messagebox.showerror("Could not save lyrics", f"{e}")
            return False
        return True

    def _open_lyrics_editor(self, slug: str) -> None:
        """A window to watch the video and correct its lyrics: one lyric line per row. Save keeps them for the
        next Redo; Save & Redo runs the Redo right away. The owner's lyrics are used exactly as written."""
        work_dir = PROJECT_ROOT / "work" / slug
        concern = ""
        try:
            concern = review_concern(work_dir)     # the reason at the current pass mark, as the Flagged pane shows it
        except Exception:
            pass
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Edit lyrics -- {slug}")
        dialog_w, dialog_h = 680, 760
        self.root.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(x, 0)}+{max(y, 0)}")
        dialog.transient(self.root)   # deliberately NOT grab_set: the owner may want to Watch while editing
        dialog.lift()
        dialog.focus_force()
        dialog.attributes("-topmost", True)
        dialog.after(300, lambda: dialog.attributes("-topmost", False))

        if concern:
            ctk.CTkLabel(dialog, text=concern, anchor="w", wraplength=dialog_w - 40, justify="left").pack(
                fill="x", padx=14, pady=(12, 4)
            )
        ctk.CTkLabel(
            dialog, anchor="w", wraplength=dialog_w - 40, justify="left", text_color="gray60",
            text="One lyric line per row; blank rows are ignored. Your lyrics are used exactly as written on the "
                 "next Redo (no online lookup or AI check overrides them); the timing is still worked out from "
                 "the audio.",
        ).pack(fill="x", padx=14, pady=(0, 6))
        box = ctk.CTkTextbox(dialog, wrap="word", font=ctk.CTkFont(size=14))
        box.pack(fill="both", expand=True, padx=14, pady=6)
        box.insert("1.0", load_editable_lyrics(work_dir))

        def save(redo: bool) -> None:
            if not self._save_owner_lyrics_from_editor(slug, box.get("1.0", "end")):
                return
            dialog.destroy()
            if redo:
                self._on_redo_flagged(slug)
            else:
                messagebox.showinfo("Lyrics saved", "Saved. Use Redo to rebuild the video with these lyrics.")

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkButton(
            buttons, text="▶ Watch video", width=110, fg_color="gray30", hover_color="gray20",
            command=lambda: self._on_watch_song(slug),
        ).pack(side="left")
        ctk.CTkButton(buttons, text="Cancel", width=80, fg_color="gray30", hover_color="gray20",
                      command=dialog.destroy).pack(side="right", padx=(6, 0))
        ctk.CTkButton(buttons, text="Save & Redo", width=110, command=lambda: save(True)).pack(side="right", padx=(6, 0))
        ctk.CTkButton(buttons, text="Save", width=80, command=lambda: save(False)).pack(side="right")

    def _on_redo_flagged(self, slug: str) -> None:
        # Reuses the Redo dropdown + confirmation flow verbatim -- Redo
        # re-fetches lyrics fresh through the same multi-source accuracy
        # check, so a clean fetch this time clears the concern on its own.
        self.redo_song_var.set(slug)
        self._on_redo()

    def _on_held_before_video(self, concern: str) -> None:
        """A run stopped before its video because the sync check failed (HeldBeforeVideo): tell the owner plainly, free the
        buttons, and refresh the lists so the song shows in Flagged for Lyrics Review."""
        self.status_var.set("Held for review")
        self._running = False
        self.generate_button.configure(state="normal")
        self.redo_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self._refresh_retry_upload_options()
        if concern.startswith(KEY_HOLD_PREFIX):
            messagebox.showinfo(
                "Held for the key",
                f"No video was made yet.\n\n{concern}\n\nIt is in Flagged for Lyrics Review: use Set Key to give the song's "
                "key, then make the video.",
            )
            return
        messagebox.showinfo(
            "Held for review",
            f"No video was made.\n\n{concern}\n\nIt is in Flagged for Lyrics Review: edit the lyrics and Redo it, or use "
            "Render Anyway to make the video and watch it.",
        )

    def _on_render_anyway_flagged(self, slug: str, confirm: bool = True) -> None:
        """Makes the video for a song that was held before it (owner, 2026-09-21), from the timing already worked out
        (resumes at the chords stage). It stays flagged; the owner can then watch it and Mark Verified."""
        if self._running or _refuse_easy_variant(slug, "Render Anyway"):
            return
        song_dir = PROJECT_ROOT / "work" / slug
        try:
            audio_path, title = load_redo_inputs(song_dir)
        except Exception as e:
            messagebox.showerror("Could not load song", f"{type(e).__name__}: {e}")
            return
        if not audio_path.is_file():
            messagebox.showerror("Original audio file not found", f'"{title}" was generated from:\n{audio_path}\n\nThat file no longer exists.')
            return
        if confirm and not messagebox.askyesno(
            "Render anyway",
            f'Make the video for "{title}" anyway?\n\nIt was held before its video (the reason is shown with it in the list). '
            "This makes the video from the timing already worked out (about 15-25 minutes; new AI images are generated only "
            "if none exist yet). You can then watch it and use Mark Verified if it is good.",
        ):
            return
        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.status_var.set("Starting...")
        self.progress_bar.set(0.0)
        self._clear_log()
        self._last_work_dir = song_dir
        threading.Thread(target=self._run_worker, args=(audio_path, song_dir, title, _stage_to_resume(song_dir)), daemon=True).start()
        self.root.after(100, self._poll_queue)

    def _song_label(self, list_name: str, song: str) -> str:
        """A list row's text: the song's name, plus "verified by you (NN% automatic)" in the Upload list when it is."""
        return upload_label(PROJECT_ROOT / "work" / song) if list_name == "upload" else song

    def _on_mark_verified(self, slug: str) -> None:
        """"If i decide its a good video its a good video" (owner, 2026-09-20): after a confirm that shows the automatic
        score, records the owner's approval of THIS version (owner_verified.py). The song then leaves Flagged for Lyrics
        Review and is offered in the Upload and Pending lists; a Redo cancels it."""
        work_dir = PROJECT_ROOT / "work" / slug
        report = check_saved_song(work_dir)
        if report is None or report.share is None:
            score = "The automatic check could not score it."
        else:
            score = f"The automatic check scored it {report.share:.0%} ({report.needed:.0%} needed)."
        if not messagebox.askyesno(
            "Mark as verified",
            f"Mark '{slug}' as verified?\n\nThis says you watched this video and it is good. {score}\n\n"
            "It will then be offered for upload. Redoing the song cancels this.",
        ):
            return
        _record_owner_verification(work_dir)
        self._invalidate_flagged_list()
        self._invalidate_pending_list()
        self._invalidate_upload_list()
        self._invalidate_easy_chord_backfill_list()     # a newly passing hard-key song is an EASY CHORD candidate

    def _on_upload_anyway_flagged(self, slug: str) -> None:
        # A deliberate owner override, same as any other manual upload --
        # reuses the existing retry-upload path exactly, no separate
        # upload mechanics needed.
        self._start_retry_upload([slug])

    def _on_rebuild_easy_flagged(self, slug: str, confirm: bool = True) -> None:
        """Remakes a flagged EASY CHORD version from its song with pipeline.build_capo_variant -- the same worker the
        "Generate EASY CHORD Versions" list uses -- instead of running the pipeline on the capo folder itself (which lost
        the capo; issue #7 review). Its song's key must be settled first; if not, the result says so."""
        if self._running:
            return
        parent = _easy_variant_parent(slug)
        if confirm and not messagebox.askyesno(
            "Rebuild EASY version",
            f'Rebuild the EASY CHORD (capo) version of "{parent}" from that song as it is now?\n\n'
            "It reuses the song's own lyrics, timing, images and audio and only re-renders -- no new AI cost.",
        ):
            return
        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.generate_easy_chord_backfill_button.configure(state="disabled")
        self.status_var.set("Rebuilding the EASY CHORD version...")
        self.progress_bar.set(0.0)
        self._clear_log()
        thread = threading.Thread(target=self._run_easy_chord_backfill_worker, args=([parent],), daemon=True)
        thread.start()
        self.root.after(100, self._poll_queue)

    def _on_check_youtube_comments(self) -> None:
        if _COMMENT_CHECK_LOCK.locked():
            messagebox.showinfo(
                "Checking comments", "A comment check is already running -- new comments show up here when it is done.",
            )
            return
        if not self._confirm_quota_override_if_blocked():
            return
        threading.Thread(target=self._check_youtube_comments_worker, daemon=True).start()

    def _check_youtube_comments_worker(self) -> None:
        """Runs on a background thread, both on-demand (Check Now) and every
        20 minutes via _youtube_periodic_tick -- an unhandled exception here
        would otherwise recur forever on every future tick with no visible
        indication beyond a scary traceback in the log, so the whole body is
        one last defensive layer on top of the per-video and per-comment
        isolation in _scan_youtube_comments. Only one check runs at a time: a
        second one (Check Now during the tick's) returns at once."""
        if not _COMMENT_CHECK_LOCK.acquire(blocking=False):
            return
        try:
            _scan_youtube_comments(self)
        except Exception as e:
            print(f"WARNING: YouTube comment check failed: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            _COMMENT_CHECK_LOCK.release()

    def _schedule_youtube_comment_check(self) -> None:
        threading.Thread(target=self._youtube_periodic_tick, daemon=True).start()
        self.root.after(20 * 60 * 1000, self._schedule_youtube_comment_check)

    def _youtube_periodic_tick(self) -> None:
        """Runs every 20 minutes on a background thread (never the GUI thread,
        since both load_credentials()'s token refresh and get_channel_title()
        can make a real network call). Keeps the connection status label
        current even across a long-running session -- otherwise a token that
        expires mid-session (Google's own 7-day limit on an unverified/
        Testing-mode app, which this one always is for personal use) would
        only be noticed at next app launch. _youtube_status_text() itself
        makes zero real API calls while a quota cooldown is active, so it's
        always safe to call here regardless. The comment scan and
        auto-retry-upload below are NOT safe during a cooldown -- both make
        real calls, potentially across many videos/songs every 20 minutes --
        so both are skipped outright while blocked (2026-09-18), rather than
        each independently re-discovering the same quota error. They resume
        on their own the first tick after the cooldown passes.

        A tick that finds the previous one still working (a long upload run
        takes longer than 20 minutes on the owner's uplink) is skipped, never
        stacked on top of it (issue #7 review, F049)."""
        if not _TICK_LOCK.acquire(blocking=False):
            return
        try:
            status_text = self._youtube_status_text()
            self.root.after(0, lambda: self.youtube_status_var.set(status_text))
            blocked_until = load_quota_blocked_until()
            if blocked_until is not None and datetime.now().astimezone() < blocked_until:
                return
            # Returns immediately on its own when there are no credentials.
            self._check_youtube_comments_worker()
            try:
                self._retry_pending_uploads_if_due()
            except Exception as e:
                print(f"WARNING: automatic pending-upload retry failed: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            _TICK_LOCK.release()

    def _retry_pending_uploads_if_due(self) -> None:
        """Auto-recovers from a quota-exceeded day without the owner having
        to notice and click Retry (owner request, 2026-09-17) -- runs on the
        same 20-minute background tick that already refreshes comments and
        connect-status. Only acts when auto-upload is on (an owner who wants
        manual control over uploads shouldn't have this silently upload in
        the background either), never while a Generate/Redo/Batch is
        already active, and never for a song flagged for lyrics review
        (2026-09-18) -- only a deliberate Upload Anyway click uploads one
        of those.

        Never while a manual upload run is going, and never two at once (the
        upload-run lock; issue #7 review): the two used to upload the same
        pending songs twice. A song that failed here waits _AUTO_RETRY_BACKOFF
        before the next automatic try."""
        if self._running or not self.settings.youtube_auto_upload:
            return
        if _upload_run_active():
            return  # a manual upload run is going -- it handles the pending songs
        if youtube_auth.load_credentials() is None:
            return
        blocked_until = load_quota_blocked_until()
        now = datetime.now().astimezone()
        if blocked_until is not None and now < blocked_until:
            return
        dismissed = load_dismissed("pending")
        flagged = set(list_flagged_songs(PROJECT_ROOT / "work"))
        pending = [
            s for s in list_pending_uploads(PROJECT_ROOT / "work") if s not in dismissed and s not in flagged
        ]
        pending = _auto_retry_due(self, pending, now)
        if not pending:
            return
        if not _UPLOAD_RUN_LOCK.acquire(blocking=False):
            return  # a manual run started meanwhile
        try:
            results = _retry_pending_uploads(PROJECT_ROOT / "work", self.settings, pending)
        finally:
            _UPLOAD_RUN_LOCK.release()
        _note_auto_retry_results(self, results, now)
        self.root.after(0, self._refresh_retry_upload_options)

    def _run_worker(
        self,
        audio_path: Path,
        work_dir: Path,
        title: str | None = None,
        start_stage: str = "identify",
        force_easy_chord: bool = False,
        fresh_images: bool = False,
    ) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        # force_easy_chord (owner, 2026-09-23: a per-Redo "Easy Chords" checkbox) must work even when the
        # global Settings.generate_easy_chord_versions toggle is off, and must never flip that toggle itself
        # -- a copy via replace(), never a mutation of the shared self.settings object.
        settings = replace(self.settings, generate_easy_chord_versions=True) if force_easy_chord else self.settings
        try:
            undismiss_song("flagged", work_dir.name)      # a Redo of a song removed from review brings it back
            out_path = run_pipeline(
                audio_path,
                work_dir,
                title,
                start_stage=start_stage,
                settings=settings,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
                fresh_images=fresh_images,
            )
            _maybe_upload_to_youtube(work_dir, self.settings)
            self._queue.put(("done", str(out_path)))
        except HeldBeforeVideo as e:
            self._queue.put(("held", e.concern))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    def _poll_queue(self) -> None:
        # Drain everything queued since the last tick up front, rather than
        # handling each item with its own widget update -- moviepy/tqdm can
        # write dozens of progress-bar chunks within a single 100ms tick, and
        # one insert+see() per chunk against a growing Text widget is what
        # made the log pane (and the whole GUI) grind to a crawl.
        items: list[tuple[str, str]] = []
        try:
            while True:
                items.append(self._queue.get_nowait())
        except queue.Empty:
            pass

        log_chunks: list[str] = []

        def flush_log() -> None:
            if log_chunks:
                self._append_log("".join(log_chunks))
                log_chunks.clear()

        for kind, payload in items:
            if kind == "log":
                log_chunks.append(payload)
                continue
            flush_log()
            try:
                _dispatch_queue_message(self, kind, payload)
            except _StopPolling:
                return
            except Exception as e:
                # A single message's handler failing must never kill this recurring poll -- real incident,
                # 2026-09-22: rebuilding an OPEN review list (invalidate() -> populate_now(), from the routine
                # post-batch-item refresh) threw partway through a Batch run. With no guard here, that exception
                # propagated straight out of _poll_queue, so root.after(100, self._poll_queue) below never ran
                # again -- no later tick was left to read the batch's own eventual "batch_done" message, so
                # self._running stayed stuck True even though the pipeline had already finished. The owner's
                # window then insisted a video was "still being generated" every time they tried to close it.
                print(f"WARNING: could not handle a {kind!r} GUI update: {type(e).__name__}: {e}", file=sys.stderr)
        flush_log()

        if self._running:
            self.root.after(100, self._poll_queue)

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")
        self._log_pending = ""
        self._log_has_uncommitted_line = False

    def _append_log(self, text: str) -> None:
        self._log_pending, to_commit = _split_log_text(self._log_pending, text)

        self.log_widget.configure(state="normal")
        if self._log_has_uncommitted_line:
            # The widget's current last line was left showing a still-in-
            # progress update (e.g. a tqdm percentage) -- replace it rather
            # than appending, so a burst of \r updates collapses into one
            # line instead of piling up a new permanent line per update.
            self.log_widget.delete("end-1c linestart", "end-1c")
        if to_commit:
            self.log_widget.insert("end", to_commit)
        if self._log_pending:
            self.log_widget.insert("end", self._log_pending)
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")
        self._log_has_uncommitted_line = bool(self._log_pending)


def _dispatch_queue_message(self, kind: str, payload) -> None:
    """One queued message's worth of GUI update, split out of _poll_queue so a handler that raises can be caught
    there without losing track of which branches must stop the poll for this tick (_StopPolling, exactly the
    branches that used to `return` straight out of _poll_queue) vs. fall through to the next queued message. A
    plain module-level function, not a method, so _poll_queue's own tests (which pass a bare stub object as
    `self`, not a real LyricVideoGUI instance) don't need this bound onto every stub -- same reason
    _open_with_default_app/_slugify/etc. above are free functions rather than methods."""
    if kind == "stage":
        if self._batch_items:
            self.status_var.set(f"File {self._batch_index}/{len(self._batch_items)}: Stage: {payload}")
            try:
                stage_fraction = (STAGES.index(payload) + 1) / len(STAGES)
            except ValueError:
                stage_fraction = 0.0
            combined = (self._batch_index - 1 + stage_fraction) / len(self._batch_items)
            self.progress_bar.set(min(1.0, combined))
        else:
            self.status_var.set(f"Stage: {payload}")
            # +1: report("done") isn't a real STAGES entry, but seeing the
            # bar reach 100% only once done fires (not at the start of the
            # last real stage) reads better than stalling at 6/7.
            try:
                fraction = (STAGES.index(payload) + 1) / len(STAGES)
            except ValueError:
                fraction = self.progress_bar.get()
            self.progress_bar.set(min(1.0, fraction))
    elif kind == "done":
        self.status_var.set("Done")
        self.progress_bar.set(1.0)
        self._running = False
        self.generate_button.configure(state="normal")
        self.redo_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self._refresh_retry_upload_options()
        messagebox.showinfo("Video ready", f"Wrote {payload}")
        raise _StopPolling
    elif kind == "held":
        self._on_held_before_video(payload)
        raise _StopPolling
    elif kind == "error":
        self.status_var.set("Failed")
        self._running = False
        self.generate_button.configure(state="normal")
        self.redo_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self._append_log(f"\nERROR:\n{payload}\n")
        messagebox.showerror("Generation failed", payload.splitlines()[0])
        raise _StopPolling
    elif kind == "batch_resolved":
        self._on_batch_resolved(payload)
        raise _StopPolling
    elif kind == "batch_file_start":
        self._batch_index, total, title = payload
        self.status_var.set(f"File {self._batch_index}/{total}: {title}")
        self.progress_bar.set(min(1.0, (self._batch_index - 1) / total))
    elif kind == "batch_item_done":
        # So Pending/Flagged/Upload lists reflect each song as it
        # finishes, not only once the whole batch (e.g. 100 songs) ends.
        self._refresh_retry_upload_options()
    elif kind == "batch_done":
        self._on_batch_done(payload)
        raise _StopPolling
    elif kind == "easy_chord_backfill_done":
        self._on_easy_chord_backfill_done(payload)
        raise _StopPolling


def main() -> None:
    root = ctk.CTk()
    LyricVideoGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
