from pathlib import Path

import pytest

from lyricvideo.identify import (
    SongInfo,
    artist_consensus,
    extract_metadata,
    parse_filename,
    rank_musicbrainz,
)


def test_song_info_search_titles_dedupes_case_insensitively():
    info = SongInfo(path=Path("x.mp3"), title="Eye In The Sky", artist="APP",
                     alt_titles=["eye in the sky", "Sirius / Eye in the Sky"])
    assert info.search_titles == ["Eye In The Sky", "Sirius / Eye in the Sky"]


def test_song_info_search_titles_skips_blank_entries():
    info = SongInfo(path=Path("x.mp3"), title="Angie", artist="", alt_titles=["", "  "])
    assert info.search_titles == ["Angie"]


def test_parse_filename_splits_artist_and_title():
    artist, title = parse_filename("Rolling Stones - Angie")
    assert artist == "Rolling Stones"
    assert title == "Angie"


def test_parse_filename_no_separator_returns_empty_artist():
    artist, title = parse_filename("angie")
    assert artist == ""
    assert title == "angie"


def test_parse_filename_handles_track_number_prefix_style():
    artist, title = parse_filename("01. Alan Parsons Project - Eye in the Sky (Official)")
    assert "Alan Parsons Project" in artist
    assert "Eye in the Sky" in title


def test_artist_consensus_picks_the_majority_artist():
    results = [
        {"artistName": "The Alan Parsons Project", "syncedLyrics": "..."},
        {"artistName": "The Alan Parsons Project", "syncedLyrics": "..."},
        {"artistName": "Some Cover Band", "plainLyrics": "..."},
    ]
    assert artist_consensus(results) == "The Alan Parsons Project"


def test_artist_consensus_returns_none_with_no_agreement():
    # A single plain-lyrics (unsynced) record only carries weight 1 -- below the
    # min_votes=2 threshold. (A single SYNCED record carries weight 2 and would
    # legitimately pass on its own -- that's a different, correctly-answered case.)
    results = [{"artistName": "Band A", "plainLyrics": "x"}]
    assert artist_consensus(results, min_votes=2) is None


def test_rank_musicbrainz_prefers_closer_duration():
    recordings = [
        {"score": 90, "length": 300000, "artist-credit": [{"name": "Band"}], "releases": [1]},
        {"score": 90, "length": 391000, "artist-credit": [{"name": "Band"}], "releases": [1]},
    ]
    result = rank_musicbrainz(recordings, duration=390.0)
    assert result is not None
    artist, title = result
    assert artist == "Band"


def test_rank_musicbrainz_returns_none_on_empty_input():
    assert rank_musicbrainz([], duration=200.0) is None


def test_extract_metadata_falls_back_to_filename_when_no_tags(tmp_path, monkeypatch):
    # A real (silent) audio file so mutagen/probe_duration don't error out, but with
    # no ID3 tags at all, so this exercises the filename-parsing fallback path.
    import subprocess

    from lyricvideo.audio_decode import find_ffmpeg

    audio_path = tmp_path / "Rolling Stones - Angie.mp3"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "2", str(audio_path)],
        check=True,
    )
    # No network lookups should be needed since the filename already has an artist.
    monkeypatch.setattr("lyricvideo.identify.lrclib_artist_for_title", lambda title: None)
    monkeypatch.setattr("lyricvideo.identify.musicbrainz_lookup", lambda *a, **k: None)

    info = extract_metadata(audio_path)

    assert info.artist == "Rolling Stones"
    assert info.title == "Angie"
    assert info.source == "filename"
