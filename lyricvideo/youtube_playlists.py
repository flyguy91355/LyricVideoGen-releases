"""Orchestrates channel organization for one already-uploaded video:
All/Artist/Genre playlist membership, plus queuing its drafted engagement
comment for owner review. Called automatically right after every
schedule_upload() (see gui.py's two upload call sites) and by
scripts/backfill_channel_organization.py for the videos that predate this
feature. See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

from .chord_theory import NOTES_SHARP, is_easy_key, key_name, load_easy_chord_capo_marker, ordered_unique_chords
from .key_decision import load_decision, load_owner_key
from .key_estimate import parse_key
from .models import load_song
from .youtube import (
    add_video_to_playlist, create_playlist, find_own_playlist_by_title, find_playlist_by_id, get_playlist_description,
    remove_video_from_playlist,
)
from .youtube_comment_state import PendingComment, add_pending_comment, load_pending_comments
from .youtube_metadata import classify_genre, draft_engagement_comment
from .youtube_playlist_state import add_genre_if_new, load_genres, load_playlist_ids, save_playlist_id
from .youtube_state import atomic_write_text, load_youtube_state

_PLAYLIST_PROPAGATION_RETRIES = 3
_PLAYLIST_PROPAGATION_DELAY_SECONDS = 2.0

# get_or_create_playlist's check -> create -> save runs under this, so two overlapping organize_video calls (an upload
# worker and the 20-minute retry, or Upload Selected) can never both miss the same new artist/genre and create it twice.
_PLAYLIST_LOCK = threading.RLock()

ALL_PLAYLIST_KEY = "all"
ALL_PLAYLIST_TITLE = "Play Along Videos - All"
ALL_PLAYLIST_DESCRIPTION = "Every play-along lyrics & chords video on this channel."


def _join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def _easy_key_wording() -> str:
    """The keys chord_theory.is_easy_key accepts, in words -- built FROM is_easy_key so the public playlist text can never
    drift from the rule again (issue #7 review, F145: it still promised F and B keys, and every minor key, after the
    rule narrowed to the open-chord shapes on 2026-09-23)."""
    majors = [n for n in NOTES_SHARP if is_easy_key(f"{n} major")]
    minors = sorted(n for n in NOTES_SHARP if is_easy_key(f"{n} minor"))
    return f"{_join_names(majors)} major; {_join_names(minors)} minor"


# Owner, 2026-09-23: "i want all the videos that have easy chords already in the same playlist ... create a
# playlist called EASY CHORD Play Along Song." One fixed playlist (not per-key, unlike Artist/Genre) -- every
# song in an open-chord-friendly key (chord_theory.is_easy_key) goes in the same list.
EASY_CHORD_PLAYLIST_KEY = "easy_chord"
EASY_CHORD_PLAYLIST_TITLE = "EASY CHORD Play Along Songs"
EASY_CHORD_PLAYLIST_DESCRIPTION = (
    f"Play-along videos in an open-chord key ({_easy_key_wording()}) -- no barre-chord key."
)

# Owner, 2026-09-23: "lets do 2 more.. 3 chord songs and 4 chord songs" -- same one-fixed-playlist pattern as
# EASY CHORD, keyed on the song's own chord count (pipeline.ordered_unique_chords, now homed in
# chord_theory.py) -- AND, per the owner's follow-up ("3 and 4 chord still has to have the easy chord rule"),
# the song's key must also be an easy (open-chord) one; chord count alone doesn't make a song easy to play.
THREE_CHORD_PLAYLIST_KEY = "three_chord"
THREE_CHORD_PLAYLIST_TITLE = "3 CHORD Play Along Songs"
THREE_CHORD_PLAYLIST_DESCRIPTION = (
    f"Play-along videos built from just 3 chords, in an easy open-chord key ({_easy_key_wording()})."
)
FOUR_CHORD_PLAYLIST_KEY = "four_chord"
FOUR_CHORD_PLAYLIST_TITLE = "4 CHORD Play Along Songs"
FOUR_CHORD_PLAYLIST_DESCRIPTION = (
    f"Play-along videos built from just 4 chords, in an easy open-chord key ({_easy_key_wording()})."
)

_CHORD_COUNT_PLAYLISTS = {
    3: (THREE_CHORD_PLAYLIST_KEY, THREE_CHORD_PLAYLIST_TITLE, THREE_CHORD_PLAYLIST_DESCRIPTION),
    4: (FOUR_CHORD_PLAYLIST_KEY, FOUR_CHORD_PLAYLIST_TITLE, FOUR_CHORD_PLAYLIST_DESCRIPTION),
}

# The fixed playlists whose membership depends on the song's key -- (cache key, title, description). Reconciled both
# ways by organize_video (added when the key qualifies, removed when it no longer does).
EASY_LIKE_PLAYLISTS = [
    (EASY_CHORD_PLAYLIST_KEY, EASY_CHORD_PLAYLIST_TITLE, EASY_CHORD_PLAYLIST_DESCRIPTION),
    (THREE_CHORD_PLAYLIST_KEY, THREE_CHORD_PLAYLIST_TITLE, THREE_CHORD_PLAYLIST_DESCRIPTION),
    (FOUR_CHORD_PLAYLIST_KEY, FOUR_CHORD_PLAYLIST_TITLE, FOUR_CHORD_PLAYLIST_DESCRIPTION),
]


def get_or_create_playlist(youtube_client, key: str, title: str, description: str) -> str:
    """Checks the local cache first, self-healing if the cached playlist
    was deleted directly in Studio, and only creates fresh when the channel
    really has no such playlist -- never re-creates a playlist that's still
    real just because this process hasn't seen it before. A cache miss (a
    new artist/genre, or a cache that was lost or moved aside as corrupt)
    first looks for one of the channel's own playlists with this exact
    title (issue #7 review, F143/F144: a lost cache used to create a
    second public copy of every playlist)."""
    with _PLAYLIST_LOCK:
        cached_id = load_playlist_ids().get(key)
        if cached_id and find_playlist_by_id(youtube_client, cached_id):
            return cached_id
        playlist_id = find_own_playlist_by_title(youtube_client, title)
        if playlist_id is None:
            playlist_id = create_playlist(youtube_client, title, description)
        save_playlist_id(key, playlist_id)
        return playlist_id


def stale_playlist_descriptions(youtube_client) -> list[tuple[str, str, str, str, str]]:
    """(cache key, playlist id, title, current description, wanted description) for each fixed All/EASY/3-/4-CHORD
    playlist that exists on the channel with a description other than the one this app now writes (issue #7 review,
    F145: the EASY CHORD playlist created 2026-09-23 still promises F- and B-key songs). get_or_create_playlist only
    sets a description when it CREATES a playlist, so an existing one keeps its old text until this is applied --
    by scripts/backfill_channel_organization.py --fix-playlist-descriptions, on the owner's say-so (public content)."""
    cached = load_playlist_ids()
    stale = []
    for key, title, wanted in [(ALL_PLAYLIST_KEY, ALL_PLAYLIST_TITLE, ALL_PLAYLIST_DESCRIPTION), *EASY_LIKE_PLAYLISTS]:
        playlist_id = cached.get(key)
        if not playlist_id:
            continue
        current = get_playlist_description(youtube_client, playlist_id)
        if current is not None and current.strip() != wanted:
            stale.append((key, playlist_id, title, current, wanted))
    return stale


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
# A small, curated exception list of real acts whose own name contains a comma, compared in a normalized form
# ("and" == "&", case and spacing ignored -- "Crosby, Stills, Nash and Young" is the same band); anything not in
# it still splits, matching credit-metadata convention for actual collaborations.
_MULTI_COMMA_BAND_NAMES = [
    "Crosby, Stills, Nash & Young", "Crosby, Stills & Nash", "Emerson, Lake & Palmer",
    "Blood, Sweat & Tears", "Earth, Wind & Fire", "Peter, Paul & Mary", "Tyler, the Creator",
]
_AMPERSAND_WORD = re.compile(r"\s+(?:&|and)\s+", re.IGNORECASE)
_CREDIT_JOIN = re.compile(r"\s+&\s+")           # MusicBrainz's joinphrase before a collaboration's last artist


def _normalized_artist(name: str) -> str:
    return " ".join(_AMPERSAND_WORD.sub(" & ", name.strip().lower()).split())


_MULTI_COMMA_BAND_KEYS = frozenset(_normalized_artist(name) for name in _MULTI_COMMA_BAND_NAMES)


def _split_artists(artist_field: str) -> list[str]:
    """The separate artists in an artist credit. MusicBrainz joins a collaboration's names with ", " and a final " & "
    (identify._artist_credit), so "A, B & C" is three artists, not "A" and a made-up "B & C" (issue #7 review, F146).
    The final " & " is only split off when there is a comma list at all (a bare "Simon & Garfunkel" stays one act),
    never before "the ..." ("Tom Petty & the Heartbreakers" stays whole), and never at a written-out "and" (too often
    part of a real act's name). identify.py saving the individual names ("artists" in song_info.json) beats this."""
    stripped = artist_field.strip()
    if not stripped:
        return []
    if _normalized_artist(stripped) in _MULTI_COMMA_BAND_KEYS:
        return [stripped]
    parts = [a.strip() for a in stripped.split(",") if a.strip()]
    if len(parts) >= 2:
        pieces = _CREDIT_JOIN.split(parts[-1], maxsplit=1)
        if len(pieces) == 2 and all(p.strip() for p in pieces) and not pieces[1].strip().lower().startswith("the "):
            parts = parts[:-1] + [pieces[0].strip(), pieces[1].strip()]
    return parts


def _read_song_info(work_dir: Path) -> dict | None:
    """song_info.json's contents, or None when it is missing or unreadable -- never an empty dict that a later write
    would save over the real file (issue #7 review, F147)."""
    try:
        data = json.loads((Path(work_dir) / "song_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_genre(work_dir: Path, genre: str) -> None:
    """Caches the genre in song_info.json -- only when that file really exists and reads cleanly, merged into a fresh
    read of it so nothing else in it is lost, and written atomically. A pre-merge song without the file gets none (a
    file holding only {"genre": ...} made the next Redo fail with KeyError 'title')."""
    fresh = _read_song_info(work_dir)
    if fresh is None or not fresh.get("title"):
        return
    fresh["genre"] = genre
    atomic_write_text(Path(work_dir) / "song_info.json", json.dumps(fresh))


def _artists_for(info: dict) -> list[str]:
    """The individual artists: song_info.json's "artists" list when identify.py saved one, else the credit string split."""
    listed = info.get("artists")
    if isinstance(listed, list):
        names = [str(a).strip() for a in listed if str(a).strip()]
        if names:
            return names
    return _split_artists(str(info.get("artist") or ""))


def membership_key(work_dir: Path, song, key_override: str | None = None) -> str:
    """The key that decides EASY/3-/4-CHORD playlist membership. An EASY CHORD version is judged by its own (shape) key
    -- easy by construction. Otherwise the song's real key, most trusted first: the owner's own Set Key answer
    (key_owner.json always wins), `key_override` (e.g. the audited true key from scripts/key_notes_*.json), a confirmed
    key decision, and last the key saved with the chords (issue #7 review, F148: membership used to follow only the
    saved key, even where that is known wrong)."""
    work_dir = Path(work_dir)
    if work_dir.name == "easychords" or load_easy_chord_capo_marker(work_dir) is not None:
        return song.chord_track.key or ""
    candidates = [load_owner_key(work_dir), key_override]
    decision = load_decision(work_dir)
    if decision is not None and decision.confirmed:
        candidates.append(decision.key)
    candidates.append(song.chord_track.key)
    for candidate in candidates:
        parsed = parse_key(candidate)
        if parsed is not None:
            return key_name(parsed[0], parsed[1], True)
    return ""


def wanted_easy_like_playlists(work_dir: Path, song, key_override: str | None = None) -> set[str]:
    """Which of the EASY CHORD / 3 CHORD / 4 CHORD playlists (their cache keys) this video belongs in."""
    key = membership_key(work_dir, song, key_override)
    if not key or not is_easy_key(key):
        return set()
    wanted = {EASY_CHORD_PLAYLIST_KEY}
    # Owner, 2026-09-23: "3 and 4 chord still has to have the easy chord rule" -- chord count alone isn't
    # enough; a 3- or 4-chord song in a hard key still isn't an easy song to play.
    chord_count = len(ordered_unique_chords(song.chord_track))
    if chord_count in _CHORD_COUNT_PLAYLISTS:
        wanted.add(_CHORD_COUNT_PLAYLISTS[chord_count][0])
    return wanted


def organize_video(
    youtube_client, anthropic_client, work_dir: Path, *, key_override: str | None = None,
) -> list[str]:
    """Puts one uploaded video in its playlists and queues its engagement comment. The All, EASY/3-/4-CHORD and artist
    playlists come first; the genre (one Claude call) is looked up after them and fails soft, so a bad or missing genre
    answer never keeps a video out of every playlist (issue #7 review, F085). EASY/3-/4-CHORD membership is reconciled
    both ways: added when the key qualifies, removed from any of those it no longer qualifies for (F148). Returns the
    cache keys of the playlists it took the video OUT of (usually none)."""
    work_dir = Path(work_dir)
    removed: list[str] = []
    state = load_youtube_state(work_dir)
    if state is None:
        return removed  # never uploaded -- nothing to organize yet

    info = _read_song_info(work_dir)
    song = load_song(work_dir / "lyrics_timed.json")   # the song's key, its chord count, and genre classification below
    known = info or {}
    title = known.get("title") or song.title or state.title or work_dir.name
    artist_field = str(known.get("artist") or "")
    genre = str(known.get("genre") or "").strip()

    all_playlist_id = get_or_create_playlist(
        youtube_client, ALL_PLAYLIST_KEY, ALL_PLAYLIST_TITLE, ALL_PLAYLIST_DESCRIPTION,
    )
    add_video_to_playlist_with_retry(youtube_client, all_playlist_id, state.video_id)

    wanted = wanted_easy_like_playlists(work_dir, song, key_override)
    cached_ids = load_playlist_ids()
    for key, playlist_title, description in EASY_LIKE_PLAYLISTS:
        if key in wanted:
            playlist_id = get_or_create_playlist(youtube_client, key, playlist_title, description)
            add_video_to_playlist_with_retry(youtube_client, playlist_id, state.video_id)
        elif cached_ids.get(key):
            # Not (or no longer) an easy-key song: take it back out -- a no-op for a video that was never in it.
            if remove_video_from_playlist(youtube_client, cached_ids[key], state.video_id):
                removed.append(key)

    for artist in _artists_for(known):
        artist_playlist_id = get_or_create_playlist(
            youtube_client, f"artist:{artist}", f"{artist} - Play Along Videos",
            f"Every play-along lyrics & chords video on this channel by {artist}.",
        )
        add_video_to_playlist_with_retry(youtube_client, artist_playlist_id, state.video_id)

    if not genre:
        full_lyrics = "\n".join(line.text for line in song.lines)
        try:
            genre = classify_genre(anthropic_client, title, artist_field, full_lyrics, load_genres())
        except Exception as e:
            print(f"WARNING: could not classify a genre for {work_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)
            genre = ""
        if genre:
            add_genre_if_new(genre)
            _save_genre(work_dir, genre)

    if genre:
        genre_playlist_id = get_or_create_playlist(
            youtube_client, f"genre:{genre}", f"{genre} - Play Along Videos",
            f"Every {genre} play-along lyrics & chords video on this channel.",
        )
        add_video_to_playlist_with_retry(youtube_client, genre_playlist_id, state.video_id)

    if not state.engagement_comment_posted:
        already_pending = any(c.video_id == state.video_id for c in load_pending_comments())
        if not already_pending:
            try:
                comment_text = draft_engagement_comment(anthropic_client, title)
            except Exception as e:
                print(f"WARNING: could not draft an engagement comment for {work_dir.name}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                return removed
            add_pending_comment(PendingComment(video_id=state.video_id, song_title=title, draft_text=comment_text))
    return removed
