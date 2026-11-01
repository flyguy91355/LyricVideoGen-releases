"""Our own copy of lrclib.net's lyrics database (owner, 2026-10-31: lrclib's outage sent a whole Batch to NetEase's wrong
songs -- "or we make our own"). lrclib publishes its whole database as one SQLite file (https://lrclib.net/db-dumps; 49 GB
gzipped, 268 GB unpacked, on 10-30). Kept at ~/PlayAlongVideoProductionLyrics/lrclib.sqlite3 (a link to a big drive is
fine), it is asked first (fetch_lyrics._fetch_lrclib_hit; owner, 2026-11-01), lrclib.net only for a song it lacks: an
outage no longer stops a Batch. No file there = lrclib.net only. A lookup reads ~300 rows, about 5 s on the owner's media
drive.

Records come back shaped like lrclib's own API answers, so the edition vote that picks among them is the same code."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path

DB_NAME = "lrclib.sqlite3"
AFTER_EVERY_NAME = chr(0xFFFF)     # sorts after every name that starts the same way
MAX_RECORDS = 150          # per title spelling, nearest the song's length first: "Yesterday" by The Beatles has 1,434

# lrclib's own name folding (server/src/utils.rs prepare_input), which its name_lower/artist_name_lower columns hold.
_TO_SPACE = re.compile(r"[`~!@#$%^&*()_|+\-=?;:\",.<>{}\[\]\\/\x00\n]")
_TO_REMOVE = re.compile("['" + chr(0x2019) + "]")     # ' and the curly one


def lyrics_dir() -> Path:
    return Path.home() / "PlayAlongVideoProductionLyrics"


def local_db() -> Path | None:
    path = lyrics_dir() / DB_NAME
    return path if path.is_file() else None


def prepare(text: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    folded = _TO_REMOVE.sub("", _TO_SPACE.sub(" ", folded)).lower()
    return " ".join(folded.split())


def find_records(db: Path, titles: list[str], artist: str, duration: float = 0.0) -> list[dict]:
    """The tracks whose name starts with one of `titles` (lrclib-folded) by an artist whose name holds `artist` or is
    held in it -- the MAX_RECORDS nearest `duration` per title -- with their current lyrics. Raises sqlite3.Error when
    the file cannot be read."""
    wanted_artist = prepare(artist)
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        con.row_factory = sqlite3.Row
        records: dict[int, dict] = {}
        for name in dict.fromkeys(prepare(t) for t in titles):
            if not name:
                continue
            # Picked from lrclib's (name_lower, artist_name_lower, duration) index alone; only the picked rows and their
            # lyrics are read from the big tables (each a disk seek on the owner's media drive).
            pick = ("SELECT id FROM tracks INDEXED BY idx_tracks_metadata_no_album "
                    "WHERE name_lower >= ? AND name_lower < ?")
            params: list = [name, name + AFTER_EVERY_NAME]
            if wanted_artist:
                pick += " AND (instr(artist_name_lower, ?) > 0 OR instr(?, artist_name_lower) > 0)"
                params += [wanted_artist, wanted_artist]
            pick += f" ORDER BY abs(coalesce(duration, 0) - ?) LIMIT {MAX_RECORDS}"
            params.append(duration)
            query = ("SELECT t.id, t.name, t.artist_name, t.album_name, t.duration, l.plain_lyrics, l.synced_lyrics, "
                     f"l.instrumental FROM ({pick}) AS m JOIN tracks t ON t.id = m.id "
                     "LEFT JOIN lyrics l ON l.id = t.last_lyrics_id")
            for row in con.execute(query, params):
                records[row["id"]] = {
                    "id": row["id"], "name": row["name"], "trackName": row["name"], "artistName": row["artist_name"],
                    "albumName": row["album_name"], "duration": row["duration"],
                    "instrumental": bool(row["instrumental"]),
                    "plainLyrics": row["plain_lyrics"], "syncedLyrics": row["synced_lyrics"],
                }
        return list(records.values())
    finally:
        con.close()
