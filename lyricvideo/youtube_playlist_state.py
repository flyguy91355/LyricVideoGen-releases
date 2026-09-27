"""Pure persistence for channel-wide YouTube organization: cached playlist
IDs (so a playlist is never re-created or re-searched-for once it exists)
and the shared, growing genre vocabulary Claude classifies songs into. See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .youtube_state import atomic_write_text, read_json_state

# Held across every load-modify-save (issue #7 review, F143/F144): organize_video runs on upload worker threads that can
# overlap, and an unlocked read-modify-write let one save wipe another's playlist id. Writes are atomic; an unreadable
# cache is moved aside to *.corrupt-<time> (never silently overwritten) and youtube_playlists.get_or_create_playlist
# re-finds existing playlists by title on a cache miss, so a lost cache no longer creates duplicate public playlists.
_LOCK = threading.RLock()

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
PLAYLISTS_FILE = CREDENTIALS_DIR / "youtube_playlists.json"
GENRES_FILE = CREDENTIALS_DIR / "youtube_genres.json"

DEFAULT_GENRES = [
    "Classic Rock", "Southern Rock", "Hard Rock / Glam", "Progressive Rock",
    "Alternative / Grunge", "Singer-Songwriter / Folk Rock", "Pop Rock / Soft Rock",
    "60s Pop Rock / British Invasion", "Punk", "Country", "Pop", "Hip-Hop / Rap",
    "R&B / Soul", "Metal", "Blues", "Folk / Americana", "Jazz", "Electronic / Dance",
    "Latin", "Reggae", "Christian / Gospel",
]


def load_playlist_ids(path: Path = PLAYLISTS_FILE) -> dict[str, str]:
    with _LOCK:
        data = read_json_state(path, {}, dict)
    return {str(k): str(v) for k, v in data.items() if v}


def save_playlist_id(key: str, playlist_id: str, path: Path = PLAYLISTS_FILE) -> None:
    with _LOCK:
        ids = load_playlist_ids(path)
        ids[key] = playlist_id
        atomic_write_text(path, json.dumps(ids))


def remove_playlist_ids(keys: list[str], path: Path = PLAYLISTS_FILE) -> None:
    """Drops cached ids (e.g. broken playlists a cleanup script retired) without touching the rest."""
    with _LOCK:
        ids = load_playlist_ids(path)
        if any(k in ids for k in keys):
            for key in keys:
                ids.pop(key, None)
            atomic_write_text(path, json.dumps(ids))


def load_genres(path: Path = GENRES_FILE) -> list[str]:
    with _LOCK:
        data = read_json_state(path, None, list)
    if data is None:
        return list(DEFAULT_GENRES)
    return [str(g) for g in data if isinstance(g, str) and g.strip()]


def add_genre_if_new(genre: str, path: Path = GENRES_FILE) -> None:
    """Adds a genre to the shared list; a blank genre is ignored (issue #7 review, F085: a reply with no GENRE: line
    used to add "" to the list, which then showed up as a bare "- " line in every later classification prompt)."""
    genre = (genre or "").strip()
    if not genre:
        return
    with _LOCK:
        genres = load_genres(path)
        if genre in genres:
            return
        genres.append(genre)
        atomic_write_text(path, json.dumps(genres))
