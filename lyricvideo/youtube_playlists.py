"""Orchestrates channel organization for one already-uploaded video:
All/Artist/Genre playlist membership, plus queuing its drafted engagement
comment for owner review. Called automatically right after every
schedule_upload() (see gui.py's two upload call sites) and by
scripts/backfill_channel_organization.py for the videos that predate this
feature. See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .models import load_song
from .youtube import add_video_to_playlist, create_playlist, find_playlist_by_id
from .youtube_comment_state import PendingComment, add_pending_comment, load_pending_comments
from .youtube_metadata import classify_genre, draft_engagement_comment
from .youtube_playlist_state import add_genre_if_new, load_genres, load_playlist_ids, save_playlist_id
from .youtube_state import load_youtube_state

_PLAYLIST_PROPAGATION_RETRIES = 3
_PLAYLIST_PROPAGATION_DELAY_SECONDS = 2.0

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


def _add_video_to_playlist_with_retry(youtube_client, playlist_id: str, video_id: str) -> None:
    """A playlist get_or_create_playlist() just created via playlists().insert()
    can still 404 as playlistNotFound on the very next playlistItems() call --
    real incident, 2026-09-18: a known Google API propagation lag right after
    creating a resource, hit in practice on the first video for a brand new
    artist/genre playlist (or a channel's very first-ever organized upload).
    Retries a few times with a short delay for exactly this error; anything
    else -- including a genuinely deleted/nonexistent playlist that never
    starts responding, or an unrelated failure like a quota-exceeded 429 --
    still raises immediately, since it would never resolve by waiting."""
    from googleapiclient.errors import HttpError

    for attempt in range(_PLAYLIST_PROPAGATION_RETRIES):
        try:
            add_video_to_playlist(youtube_client, playlist_id, video_id)
            return
        except HttpError as e:
            if e.status_code != 404 or attempt == _PLAYLIST_PROPAGATION_RETRIES - 1:
                raise
            time.sleep(_PLAYLIST_PROPAGATION_DELAY_SECONDS)


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
    _add_video_to_playlist_with_retry(youtube_client, all_playlist_id, state.video_id)

    for artist in _split_artists(artist_field):
        artist_playlist_id = get_or_create_playlist(
            youtube_client, f"artist:{artist}", f"{artist} - Play Along Videos",
            f"Every play-along lyrics & chords video on this channel by {artist}.",
        )
        _add_video_to_playlist_with_retry(youtube_client, artist_playlist_id, state.video_id)

    if genre:
        genre_playlist_id = get_or_create_playlist(
            youtube_client, f"genre:{genre}", f"{genre} - Play Along Videos",
            f"Every {genre} play-along lyrics & chords video on this channel.",
        )
        _add_video_to_playlist_with_retry(youtube_client, genre_playlist_id, state.video_id)

    if not state.engagement_comment_posted:
        already_pending = any(c.video_id == state.video_id for c in load_pending_comments())
        if not already_pending:
            comment_text = draft_engagement_comment(anthropic_client, title)
            add_pending_comment(PendingComment(video_id=state.video_id, song_title=title, draft_text=comment_text))
