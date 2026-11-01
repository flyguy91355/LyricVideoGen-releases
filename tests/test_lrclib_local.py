"""Our own copy of lrclib's database (owner, 2026-10-31: its outage sent a Batch to NetEase's wrong songs)."""

import sqlite3

import pytest

from lyricvideo import lrclib_local
from lyricvideo.fetch_lyrics import LyricsSourceDown, _fetch_lrclib_hit
from lyricvideo.lrclib_local import find_records, prepare

NEBRASKA = "[00:10.00]I saw her standing on her front lawn\n[00:14.00]Just a twirlin' her baton"


def _copy(path, tracks):
    """A database laid out like lrclib's dump (its tracks and lyrics tables, as in the 10-30 dump's schema)."""
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE lyrics (id INTEGER PRIMARY KEY AUTOINCREMENT, plain_lyrics TEXT, synced_lyrics TEXT, track_id INTEGER,"
        " has_plain_lyrics BOOLEAN, has_synced_lyrics BOOLEAN, instrumental BOOLEAN, source TEXT, created_at DATETIME,"
        " updated_at DATETIME);"
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, name_lower TEXT, artist_name TEXT,"
        " artist_name_lower TEXT, album_name TEXT, album_name_lower TEXT, duration FLOAT, last_lyrics_id INTEGER,"
        " created_at DATETIME, updated_at DATETIME);"
        "CREATE INDEX idx_tracks_metadata_no_album ON tracks (name_lower, artist_name_lower, duration);"
    )
    for n, (name, artist, duration, synced) in enumerate(tracks, start=1):
        con.execute("INSERT INTO lyrics (id, synced_lyrics, track_id, instrumental) VALUES (?, ?, ?, 0)", (n, synced, n))
        con.execute("INSERT INTO tracks (id, name, name_lower, artist_name, artist_name_lower, album_name, duration,"
                    " last_lyrics_id) VALUES (?, ?, ?, ?, ?, 'Album', ?, ?)",
                    (n, name, prepare(name), artist, prepare(artist), duration, n))
    con.commit()
    con.close()
    return path


@pytest.fixture
def copy(tmp_path, monkeypatch):
    monkeypatch.setattr(lrclib_local, "lyrics_dir", lambda: tmp_path)
    return tmp_path / lrclib_local.DB_NAME


def _lrclib_down(monkeypatch):
    """lrclib.net answers 502 to everything; any wait between retries is recorded, not slept."""
    waited = []

    class _Busy:
        status_code = 502

    monkeypatch.setattr("lyricvideo.fetch_lyrics.requests.get", lambda *a, **k: _Busy())
    monkeypatch.setattr("lyricvideo.fetch_lyrics.time.sleep", waited.append)
    return waited


def test_names_are_folded_the_way_lrclib_folds_them():
    assert prepare("I'm Ready") == "im ready"
    assert prepare("Guilty Conscience (feat. Dr. Dre)") == "guilty conscience feat dr dre"
    assert prepare("Beyoncé – Halo") == "beyonce – halo"


def test_records_are_found_by_title_start_and_either_way_round_artist(copy):
    _copy(copy, [("Nebraska", "Bruce Springsteen", 272.0, NEBRASKA),
                 ("Nebraska - 2023 Remaster", "Bruce Springsteen", 271.0, NEBRASKA),
                 ("Nebraska", "Another Band", 200.0, "[00:01.00]Not this one"),
                 ("Guilty Conscience", "Eminem", 199.0, "[00:01.00]Meet Eddie")])

    assert {r["trackName"] for r in find_records(copy, ["Nebraska"], "Bruce Springsteen")} == {
        "Nebraska", "Nebraska - 2023 Remaster"}
    found = find_records(copy, ["Guilty Conscience"], "Eminem feat. Dr. Dre")
    assert [(r["artistName"], r["duration"], r["syncedLyrics"]) for r in found] == [("Eminem", 199.0, "[00:01.00]Meet Eddie")]


def test_while_lrclib_net_answers_its_records_are_used_not_the_copy(copy, tmp_path, monkeypatch):
    """The copy returns many more records per song, so its vote can pick another transcription: the owner's songs were
    vetted on lrclib.net's picks."""
    _copy(copy, [("Nebraska", "Bruce Springsteen", 272.0, "[00:01.00]The copy's transcription")])

    class _Answer:
        status_code = 200

        def json(self):
            return [{"id": 9, "trackName": "Nebraska", "artistName": "Bruce Springsteen", "duration": 272.0,
                     "syncedLyrics": NEBRASKA}]

    monkeypatch.setattr("lyricvideo.fetch_lyrics.requests.get", lambda *a, **k: _Answer())

    assert _fetch_lrclib_hit(tmp_path / "none.mp3", "Nebraska", "Bruce Springsteen", 272.0, []) == (NEBRASKA, True, 272.0)


def test_while_lrclib_net_is_down_the_copy_answers_without_waiting_on_retries(copy, tmp_path, monkeypatch):
    _copy(copy, [("Nebraska", "Bruce Springsteen", 272.0, NEBRASKA)])
    waited = _lrclib_down(monkeypatch)

    assert _fetch_lrclib_hit(tmp_path / "none.mp3", "Nebraska", "Bruce Springsteen", 272.0, []) == (NEBRASKA, True, 272.0)
    assert waited == []


def test_with_the_copy_a_song_it_lacks_is_not_on_lrclib_even_while_lrclib_net_is_down(copy, tmp_path, monkeypatch):
    _copy(copy, [("Nebraska", "Bruce Springsteen", 272.0, NEBRASKA)])
    _lrclib_down(monkeypatch)

    assert _fetch_lrclib_hit(tmp_path / "none.mp3", "Brand New Song", "Bruce Springsteen", 200.0, []) is None


def test_without_the_copy_lrclib_net_being_down_stops_the_song(copy, tmp_path, monkeypatch):
    waited = _lrclib_down(monkeypatch)

    with pytest.raises(LyricsSourceDown):
        _fetch_lrclib_hit(tmp_path / "none.mp3", "Nebraska", "Bruce Springsteen", 272.0, [])
    assert waited                                   # with nothing to fall back on, a busy lrclib.net is retried


def test_an_unreadable_copy_falls_back_to_lrclib_net(copy, tmp_path, monkeypatch):
    copy.write_bytes(b"not a database")
    _lrclib_down(monkeypatch)

    with pytest.raises(LyricsSourceDown):
        _fetch_lrclib_hit(tmp_path / "none.mp3", "Nebraska", "Bruce Springsteen", 272.0, [])
