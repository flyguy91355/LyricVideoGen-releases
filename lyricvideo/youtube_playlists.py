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

from .chord_theory import is_easy_key, ordered_unique_chords
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

# Owner, 2026-09-23: "i want all the videos that have easy chords already in the same playlist ... create a
# playlist called EASY CHORD Play Along Song." One fixed playlist (not per-key, unlike Artist/Genre) -- every
# song in an open-chord-friendly key (chord_theory.is_easy_key) goes in the same list.
EASY_CHORD_PLAYLIST_KEY = "easy_chord"
EASY_CHORD_PLAYLIST_TITLE = "EASY CHORD Play Along Songs"
EASY_CHORD_PLAYLIST_DESCRIPTION = (
    "Play-along videos in a natural-tonic key (C, D, E, F, G, A or B, major or minor) -- no sharp or flat key."
)

# Owner, 2026-09-23: "lets do 2 more.. 3 chord songs and 4 chord songs" -- same one-fixed-playlist pattern as
# EASY CHORD, keyed on the song's own chord count (pipeline.ordered_unique_chords, now homed in
# chord_theory.py) -- AND, per the owner's follow-up ("3 and 4 chord still has to have the easy chord rule"),
# the song's key must also be an easy (natural-tonic) one; chord count alone doesn't make a song easy to play.
THREE_CHORD_PLAYLIST_KEY = "three_chord"
THREE_CHORD_PLAYLIST_TITLE = "3 CHORD Play Along Songs"
THREE_CHORD_PLAYLIST_DESCRIPTION = "Play-along videos built from just 3 chords, in an easy (natural-tonic) key."
FOUR_CHORD_PLAYLIST_KEY = "four_chord"
FOUR_CHORD_PLAYLIST_TITLE = "4 CHORD Play Along Songs"
FOUR_CHORD_PLAYLIST_DESCRIPTION = "Play-along videos built from just 4 chords, in an easy (natural-tonic) key."

_CHORD_COUNT_PLAYLISTS = {
    3: (THREE_CHORD_PLAYLIST_KEY, THREE_CHORD_PLAYLIST_TITLE, THREE_CHORD_PLAYLIST_DESCRIPTION),
    4: (FOUR_CHORD_PLAYLIST_KEY, FOUR_CHORD_PLAYLIST_TITLE, FOUR_CHORD_PLAYLIST_DESCRIPTION),
}


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


def add_video_to_playlist_with_retry(youtube_client, playlist_id: str, video_id: str) -> None:
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


# Owner, 2026-09-23: "Crosby, Stills, Nash & Young" got split into three broken playlists ("Crosby",
# "Stills", "Nash & Young") because a plain comma-split can't tell a band's own name from a genuine
# multi-artist collaboration list like "Bryan Adams, Sting, Rod Stewart" -- both look identical as strings.
# A small, curated exception list of real bands whose own name contains a comma; anything not in it still
# splits normally, matching credit-metadata convention for actual collaborations.
_MULTI_COMMA_BAND_NAMES = frozenset(name.lower() for name in [
    "Crosby, Stills, Nash & Young", "Crosby, Stills & Nash", "Emerson, Lake & Palmer",
    "Blood, Sweat & Tears", "Earth, Wind & Fire",
])


def _split_artists(artist_field: str) -> list[str]:
    stripped = artist_field.strip()
    if stripped.lower() in _MULTI_COMMA_BAND_NAMES:
        return [stripped]
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
    song = load_song(work_dir / "lyrics_timed.json")   # needed for genre classification below, the song's key, and its chord count

    if not genre:
        full_lyrics = "\n".join(line.text for line in song.lines)
        genre = classify_genre(anthropic_client, title, artist_field, full_lyrics, load_genres())
        add_genre_if_new(genre)
        info["genre"] = genre
        (work_dir / "song_info.json").write_text(json.dumps(info), encoding="utf-8")

    all_playlist_id = get_or_create_playlist(
        youtube_client, ALL_PLAYLIST_KEY, ALL_PLAYLIST_TITLE, ALL_PLAYLIST_DESCRIPTION,
    )
    add_video_to_playlist_with_retry(youtube_client, all_playlist_id, state.video_id)

    if is_easy_key(song.chord_track.key):
        easy_playlist_id = get_or_create_playlist(
            youtube_client, EASY_CHORD_PLAYLIST_KEY, EASY_CHORD_PLAYLIST_TITLE, EASY_CHORD_PLAYLIST_DESCRIPTION,
        )
        add_video_to_playlist_with_retry(youtube_client, easy_playlist_id, state.video_id)

    # Owner, 2026-09-23: "3 and 4 chord still has to have the easy chord rule" -- chord count alone isn't
    # enough; a 3- or 4-chord song in a hard (sharp/flat-tonic) key still isn't an easy song to play.
    chord_count = len(ordered_unique_chords(song.chord_track))
    if chord_count in _CHORD_COUNT_PLAYLISTS and is_easy_key(song.chord_track.key):
        key, playlist_title, description = _CHORD_COUNT_PLAYLISTS[chord_count]
        chord_playlist_id = get_or_create_playlist(youtube_client, key, playlist_title, description)
        add_video_to_playlist_with_retry(youtube_client, chord_playlist_id, state.video_id)

    for artist in _split_artists(artist_field):
        artist_playlist_id = get_or_create_playlist(
            youtube_client, f"artist:{artist}", f"{artist} - Play Along Videos",
            f"Every play-along lyrics & chords video on this channel by {artist}.",
        )
        add_video_to_playlist_with_retry(youtube_client, artist_playlist_id, state.video_id)

    if genre:
        genre_playlist_id = get_or_create_playlist(
            youtube_client, f"genre:{genre}", f"{genre} - Play Along Videos",
            f"Every {genre} play-along lyrics & chords video on this channel.",
        )
        add_video_to_playlist_with_retry(youtube_client, genre_playlist_id, state.video_id)

    if not state.engagement_comment_posted:
        already_pending = any(c.video_id == state.video_id for c in load_pending_comments())
        if not already_pending:
            comment_text = draft_engagement_comment(anthropic_client, title)
            add_pending_comment(PendingComment(video_id=state.video_id, song_title=title, draft_text=comment_text))
