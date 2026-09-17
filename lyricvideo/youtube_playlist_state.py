"""Pure persistence for channel-wide YouTube organization: cached playlist
IDs (so a playlist is never re-created or re-searched-for once it exists)
and the shared, growing genre vocabulary Claude classifies songs into. See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md."""

from __future__ import annotations

import json
from pathlib import Path

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
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_playlist_id(key: str, playlist_id: str, path: Path = PLAYLISTS_FILE) -> None:
    ids = load_playlist_ids(path)
    ids[key] = playlist_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids), encoding="utf-8")


def load_genres(path: Path = GENRES_FILE) -> list[str]:
    if not path.exists():
        return list(DEFAULT_GENRES)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(DEFAULT_GENRES)


def add_genre_if_new(genre: str, path: Path = GENRES_FILE) -> None:
    genres = load_genres(path)
    if genre in genres:
        return
    genres.append(genre)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(genres), encoding="utf-8")
