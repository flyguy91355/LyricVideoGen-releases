"""Our own copy of lrclib.net's lyrics database (owner, 2026-10-31: lrclib's outage sent a whole Batch to NetEase's wrong
songs -- "or we make our own"). lrclib publishes its whole database as one SQLite file (https://lrclib.net/db-dumps; 49 GB
gzipped on 10-30). Kept at ~/PlayAlongVideoProductionLyrics/lrclib.sqlite3 (a link to a big drive is fine), it is looked
up first: a song it has never waits on lrclib's busy server, and one it lacks (added after the copy) still asks lrclib
online (fetch_lyrics._fetch_lrclib_hit). No file there = online only, as before.

Records come back shaped like lrclib's own API answers, so the edition vote that picks among them is the same code."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path

DB_NAME = "lrclib.sqlite3"
MAX_ROWS = 2000            # per title spelling: a short title ("Shake") starts thousands of track names

# lrclib's own name folding (server/src/utils.rs prepare_input), which its name_lower/artist_name_lower columns hold.
_TO_SPACE = re.compile(r"[`~!@#$%^&*()_|+\-=?;:\",.<>{}\[\]\\/\x00\n]")
_TO_REMOVE = re.compile(r"['’]")


def lyrics_dir() -> Path:
    return Path.home() / "PlayAlongVideoProductionLyrics"


def local_db() -> Path | None:
    path = lyrics_dir() / DB_NAME
    return path if path.is_file() else None


def prepare(text: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    folded = _TO_REMOVE.sub("", _TO_SPACE.sub(" ", folded)).lower()
    return " ".join(folded.split())


def find_records(db: Path, titles: list[str], artist: str) -> list[dict]:
    """Every track whose name starts with one of `titles` (lrclib-folded) by an artist whose name holds `artist` or is
    held in it, with its current lyrics. Raises sqlite3.Error when the file cannot be read."""
    wanted_artist = prepare(artist)
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        con.row_factory = sqlite3.Row
        records: dict[int, dict] = {}
        for name in dict.fromkeys(prepare(t) for t in titles):
            if not name:
                continue
            # A range on name_lower uses lrclib's (name_lower, artist_name_lower, duration) index.
            query = ("SELECT t.id, t.name, t.artist_name, t.album_name, t.duration, l.plain_lyrics, l.synced_lyrics, "
                     "l.instrumental FROM tracks t LEFT JOIN lyrics l ON l.id = t.last_lyrics_id "
                     "WHERE t.name_lower >= ? AND t.name_lower < ?")
            params: list = [name, name + "￿"]
            if wanted_artist:
                query += " AND (instr(t.artist_name_lower, ?) > 0 OR instr(?, t.artist_name_lower) > 0)"
                params += [wanted_artist, wanted_artist]
            for row in con.execute(query + f" LIMIT {MAX_ROWS}", params):
                records[row["id"]] = {
                    "id": row["id"], "name": row["name"], "trackName": row["name"], "artistName": row["artist_name"],
                    "albumName": row["album_name"], "duration": row["duration"],
                    "instrumental": bool(row["instrumental"]),
                    "plainLyrics": row["plain_lyrics"], "syncedLyrics": row["synced_lyrics"],
                }
        return list(records.values())
    finally:
        con.close()
