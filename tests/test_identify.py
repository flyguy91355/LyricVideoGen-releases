from pathlib import Path


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


# --- issue #7 review: titles that start with a number, underscore filenames, Xing-less VBR MP3 lengths ---


def test_parse_filename_keeps_a_number_that_is_part_of_the_title():
    assert parse_filename("Numberband - 19-2000") == ("Numberband", "19-2000")
    assert parse_filename("Numberband - 5.15") == ("Numberband", "5.15")
    assert parse_filename("1-800-273-8255") == ("", "1-800-273-8255")
    assert parse_filename("2-4-6-8 Motorway") == ("", "2-4-6-8 Motorway")
    assert parse_filename("99 Balloons") == ("", "99 Balloons")
    # a band named with a three-digit number is never mistaken for a track number
    assert parse_filename("747 - Some Song") == ("747", "Some Song")


def test_parse_filename_still_strips_real_track_numbers():
    assert parse_filename("01. Some Band - Some Song") == ("Some Band", "Some Song")
    assert parse_filename("01 - Some Band - Some Song") == ("Some Band", "Some Song")
    assert parse_filename("07 Some Song") == ("", "Some Song")
    assert parse_filename("3. Some Song") == ("", "Some Song")
    assert parse_filename("12) Some Song") == ("", "Some Song")
    assert parse_filename("Some Band - 04 - Some Song") == ("Some Band", "Some Song")


def test_parse_filename_underscore_separator_turns_underscores_into_spaces():
    assert parse_filename("Some_Band_-_Some_Long_Song_Name") == ("Some Band", "Some Long Song Name")
    assert parse_filename("01_-_Some_Band_-_Some_Song") == ("Some Band", "Some Song")


def test_extract_metadata_keeps_a_tag_title_that_starts_with_a_number(tmp_path, monkeypatch):
    audio_path = tmp_path / "whatever.mp3"
    for tag_title in ("2-4-6-8 Motorway", "19-2000", "5.15", "01 Numbered On Purpose", "...And Then Some"):
        monkeypatch.setattr(
            "lyricvideo.identify.read_tags", lambda path, t=tag_title: (t, "Some Band", "", 200.0),
        )
        assert extract_metadata(audio_path).title == tag_title


def test_extract_metadata_still_strips_video_noise_from_a_tag_title(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.identify.read_tags", lambda path: ("19-2000 (Official Video)", "Some Band", "", 200.0),
    )
    assert extract_metadata(tmp_path / "x.mp3").title == "19-2000"


def _ffmpeg_has_lame() -> bool:
    import subprocess

    from lyricvideo.audio_decode import FFmpegNotFound, find_ffmpeg

    try:
        out = subprocess.run([find_ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except (FFmpegNotFound, OSError):
        return False
    return "libmp3lame" in out


def _xingless_vbr_mp3(path: Path, quiet_seconds: float = 3.0, loud_seconds: float = 15.0) -> Path:
    """A VBR MP3 with NO Xing/LAME header that starts quiet: its first frame's bitrate is tiny, so a header-based
    length guess (size * 8 / first bitrate) comes out several times too long."""
    import subprocess

    from lyricvideo.audio_decode import find_ffmpeg

    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-f", "lavfi", "-i", f"anoisesrc=r=44100:color=pink:d={loud_seconds}",
         "-filter_complex", f"[0]atrim=0:{quiet_seconds}[q];[q][1]concat=n=2:v=0:a=1",
         "-c:a", "libmp3lame", "-q:a", "0", "-write_xing", "0", str(path)],
        check=True,
    )
    return path


def test_extract_metadata_measures_a_xingless_vbr_mp3_instead_of_trusting_the_header_guess(tmp_path, monkeypatch):
    import pytest

    if not _ffmpeg_has_lame():
        pytest.skip("this FFmpeg build has no libmp3lame encoder")
    from mutagen import File as MutagenFile

    audio_path = _xingless_vbr_mp3(tmp_path / "Some Band - Some Song.mp3")
    guessed = MutagenFile(str(audio_path), easy=True).info.length
    assert guessed > 25.0          # the fixture really reproduces the bad header guess (real length is 18 s)
    monkeypatch.setattr("lyricvideo.identify.lrclib_artist_for_title", lambda title: None)
    monkeypatch.setattr("lyricvideo.identify.musicbrainz_lookup", lambda *a, **k: None)

    info = extract_metadata(audio_path)

    assert abs(info.duration - 18.0) < 1.0


def test_read_tags_only_decodes_when_the_mp3_length_is_a_header_guess(tmp_path, monkeypatch):
    """A file with a real length in its header (a CBR/Info or Xing header, or any non-MP3) is never decoded."""
    import subprocess

    import pytest

    from lyricvideo.audio_decode import find_ffmpeg
    from lyricvideo.identify import read_tags

    if not _ffmpeg_has_lame():
        pytest.skip("this FFmpeg build has no libmp3lame encoder")
    audio_path = tmp_path / "with_header.mp3"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "2", str(audio_path)],
        check=True,
    )
    monkeypatch.setattr(
        "lyricvideo.identify.decoded_duration",
        lambda path: (_ for _ in ()).throw(AssertionError("must not decode a file whose header has its length")),
    )

    _title, _artist, _album, duration = read_tags(audio_path)

    assert abs(duration - 2.0) < 0.2
