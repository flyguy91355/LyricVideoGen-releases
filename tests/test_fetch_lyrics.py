
from lyricvideo.fetch_lyrics import (
    artist_matches,
    choose_lyrics_candidate,
    fetch_lyric_lines,
    fetch_lyric_lines_verified,
    parse_lrc,
    plain_to_lines,
    title_variants,
)


class _FakeAnthropicClient:
    """Not actually called via the real API here -- tests monkeypatch
    check_lyric_accuracy itself, so this just needs to exist as a
    recognizable placeholder object passed through."""


def test_parse_lrc_extracts_timed_lines():
    text = "[00:01.00]Hello darkness\n[00:03.50]My old friend\n"
    lines = parse_lrc(text, duration=10.0)
    assert [l.text for l in lines] == ["Hello darkness", "My old friend"]
    assert lines[0].start == 1.0
    assert lines[1].start == 3.5


def test_parse_lrc_multiple_timestamps_per_line():
    text = "[00:01.00][00:05.00]Chorus line\n"
    lines = parse_lrc(text, duration=10.0)
    assert [l.start for l in lines] == [1.0, 5.0]


def test_parse_lrc_last_line_ends_at_duration_when_within_max_hold():
    text = "[00:01.00]Only line\n"
    lines = parse_lrc(text, duration=8.0)
    assert lines[0].end == 8.0


def test_parse_lrc_last_line_capped_at_max_line_hold_even_with_more_duration_left():
    # A real design choice (unchanged from LyricChord): no single line lingers on
    # screen forever just because it's the last one and the song still has a long
    # tail -- it's capped at MAX_LINE_HOLD (10.0s) seconds after its own start.
    text = "[00:01.00]Only line\n"
    lines = parse_lrc(text, duration=20.0)
    assert lines[0].end == 11.0


def test_plain_to_lines_spreads_evenly_between_intro_and_outro():
    text = "one\ntwo\nthree\n"
    lines = plain_to_lines(text, duration=100.0)
    assert [l.text for l in lines] == ["one", "two", "three"]
    assert lines[0].start == 8.0        # duration * 0.08
    assert lines[-1].end == 94.0        # duration * 0.94


def test_plain_to_lines_empty_text_returns_empty():
    assert plain_to_lines("", duration=100.0) == []


def test_title_variants_splits_on_slash():
    variants = title_variants("Sirius / Eye in the Sky")
    assert "Sirius / Eye in the Sky" in variants
    assert "Sirius Eye in the Sky" in variants


def test_title_variants_dedupes_case_insensitively():
    variants = title_variants("Angie")
    assert variants.count("Angie") == 1


def test_artist_matches_handles_the_prefix():
    assert artist_matches("The Alan Parsons Project", "Alan Parsons Project") is True


def test_artist_matches_false_for_unrelated_artists():
    assert artist_matches("Rolling Stones", "The Beatles") is False


def test_artist_matches_empty_wanted_always_matches():
    assert artist_matches("Anyone", "") is True


def test_choose_lyrics_candidate_prefers_the_larger_matching_cluster():
    items = [
        {"id": 1, "syncedLyrics": "[00:00.00]a\n[00:05.00]b\n", "duration": 100},
        {"id": 2, "syncedLyrics": "[00:00.00]a\n[00:05.00]b\n", "duration": 100},
        {"id": 3, "syncedLyrics": "[00:10.00]a\n[00:15.00]b\n", "duration": 100},
    ]
    chosen, note = choose_lyrics_candidate(items, file_duration=100.0)
    assert chosen is not None
    assert chosen["id"] in (1, 2)  # the 2-record cluster outvotes the lone record
    assert "2 of 3" in note


def test_choose_lyrics_candidate_returns_none_note_when_nothing_usable():
    chosen, note = choose_lyrics_candidate([], file_duration=100.0)
    assert chosen is None
    assert note == "no usable records"


def test_fetch_lyric_lines_uses_sidecar_lrc_before_any_network_call(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")  # never actually decoded; sidecar wins first
    (tmp_path / "song.lrc").write_text(
        "[00:00.00]Hello darkness\n[00:03.00]My old friend\n", encoding="utf-8",
    )

    def fail_if_called(*a, **k):
        raise AssertionError("network lookup should not run when a sidecar exists")

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", fail_if_called)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", fail_if_called)

    lines = fetch_lyric_lines(audio_path, title="Anything", artist="Anyone", duration=10.0)

    assert lines == ["Hello darkness", "My old friend"]


def test_fetch_lyric_lines_returns_empty_when_nothing_found(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", lambda *a, **k: None)

    assert fetch_lyric_lines(audio_path, title="Unknown", artist="Unknown", duration=10.0) == []


def test_fetch_lyric_lines_keeps_plain_lyrics_even_when_the_duration_is_unknown(tmp_path, monkeypatch):
    """Plain (unsynced) lyrics used to be routed through plain_to_lines(),
    which fabricates evenly-spread timing and returns NOTHING for duration
    0 -- so a file whose length couldn't be probed silently lost lyrics the
    provider had actually found. Only the text matters here; timing always
    comes from forced alignment later (found by code review, 2026-09-14)."""
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics._fetch_lrclib_hit",
        lambda *a, **k: ("Hello darkness\n\n  My old friend  \n", False, 0.0),
    )
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", lambda *a, **k: None)

    assert fetch_lyric_lines(audio_path, title="Sound", artist="S&G", duration=0.0) == [
        "Hello darkness", "My old friend",
    ]


def test_fetch_lyric_lines_plain_sidecar_txt_works_without_a_duration(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    (tmp_path / "song.txt").write_text("Line one\nLine two\n", encoding="utf-8")

    def fail_if_called(*a, **k):
        raise AssertionError("network lookup should not run when a sidecar exists")

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", fail_if_called)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", fail_if_called)

    assert fetch_lyric_lines(audio_path, title="T", artist="A", duration=0.0) == ["Line one", "Line two"]


def test_fetch_lyric_lines_verified_returns_the_first_source_that_passes(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    (tmp_path / "song.lrc").write_text("[00:00.00]Imagine there's no heaven\n", encoding="utf-8")
    monkeypatch.setattr("lyricvideo.fetch_lyrics.check_lyric_accuracy", lambda *a, **k: (True, ""))

    def fail_if_called(*a, **k):
        raise AssertionError("should not need lrclib/syncedlyrics when the sidecar already passed")

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", fail_if_called)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", fail_if_called)

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "Imagine", "John Lennon", 10.0, [], _FakeAnthropicClient(),
    )

    assert lines == ["Imagine there's no heaven"]
    assert source == "sidecar"
    assert concern == ""


def test_fetch_lyric_lines_verified_tries_the_next_source_when_the_first_fails_the_check(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics._fetch_lrclib_hit",
        lambda *a, **k: ("Ceci n'est pas une chanson", False, 0.0),
    )
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit",
        lambda title, artist, providers=None: ("Imagine there's no heaven", False, 0.0),
    )

    def fake_check(client, title, artist, lines, model="claude-sonnet-5"):
        if "Ceci n'est pas une chanson" in lines:
            return False, "This text is in French, not English."
        return True, ""

    monkeypatch.setattr("lyricvideo.fetch_lyrics.check_lyric_accuracy", fake_check)

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "Imagine", "John Lennon", 10.0, [], _FakeAnthropicClient(),
    )

    assert lines == ["Imagine there's no heaven"]
    assert source == "Musixmatch"
    assert concern == ""


def test_fetch_lyric_lines_verified_tries_every_provider_individually(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.fetch_lyrics.check_lyric_accuracy", lambda *a, **k: (False, "nope"))
    calls = []

    def fake_syncedlyrics(title, artist, providers=None):
        calls.append(providers)
        return ("some text", False, 0.0)

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", fake_syncedlyrics)

    fetch_lyric_lines_verified(audio_path, "T", "A", 10.0, [], _FakeAnthropicClient())

    assert calls == [["Musixmatch"], ["NetEase"], ["Megalobiz"], ["Genius"]]


def test_fetch_lyric_lines_verified_keeps_the_first_candidate_when_nothing_passes(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: ("first attempt", False, 0.0))
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit",
        lambda title, artist, providers=None: ("later attempt", False, 0.0),
    )
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics.check_lyric_accuracy", lambda *a, **k: (False, "still looks wrong"),
    )

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 10.0, [], _FakeAnthropicClient(),
    )

    assert lines == ["first attempt"]
    assert source == "lrclib"
    assert concern == "still looks wrong"


def test_fetch_lyric_lines_verified_returns_empty_when_nothing_found_anywhere(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", lambda *a, **k: None)

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 10.0, [], _FakeAnthropicClient(),
    )

    assert lines == []
    assert source == ""
    assert concern == "No lyrics found from any source."


# --- audio verification (2026-09-19): sources pass on matching what is SUNG ---------------

_SUNG = [
    "the river runs beside the old stone mill",
    "and morning fog lies heavy on the hill",
    "a lantern swings above the wooden door",
    "i wait for you like i have waited before",
]
_WRONG_EDITION = [
    "neon signs are flashing down the avenue",
    "every stranger's face looks like a stranger's face to you",
    "dance until the sunrise turns the pavement gold",
    "nobody will ever tell the story we were told",
]


def _audio_check_for(heard_lines):
    from lyricvideo.lyric_audio_match import score_lyrics_against_transcript

    heard = " ".join(heard_lines)
    return lambda lines: score_lyrics_against_transcript(lines, heard)


def _no_claude_check(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("the Claude text check must not run when an audio check is supplied")

    monkeypatch.setattr("lyricvideo.fetch_lyrics.check_lyric_accuracy", fail)


def test_verified_fetch_with_an_audio_check_skips_a_source_that_does_not_match_the_audio(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    _no_claude_check(monkeypatch)
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: ("\n".join(_WRONG_EDITION), False, 0.0),
    )
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit",
        lambda title, artist, providers=None: ("\n".join(_SUNG), False, 0.0) if providers == ["Musixmatch"] else None,
    )

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 10.0, [], _FakeAnthropicClient(), audio_check=_audio_check_for(_SUNG),
    )

    assert lines == _SUNG
    assert source == "Musixmatch"
    assert concern == ""


def test_verified_fetch_with_an_audio_check_stops_at_the_first_source_that_matches(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    _no_claude_check(monkeypatch)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: ("\n".join(_SUNG), False, 0.0))

    def fail_if_called(*a, **k):
        raise AssertionError("no further source should be asked once one matches the audio")

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", fail_if_called)

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 10.0, [], _FakeAnthropicClient(), audio_check=_audio_check_for(_SUNG),
    )

    assert (lines, source, concern) == (_SUNG, "lrclib", "")


def test_verified_fetch_keeps_and_flags_the_best_audio_match_when_none_match(tmp_path, monkeypatch):
    """lrclib is half right (its first two lines match); Musixmatch is the wrong edition
    entirely. Nothing passes, so the better one is kept -- flagged, with the reason."""
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    _no_claude_check(monkeypatch)
    half_right = _SUNG[:2] + _WRONG_EDITION[:2]
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: ("\n".join(_WRONG_EDITION), False, 0.0))

    def providers(title, artist, providers=None):
        return ("\n".join(half_right), False, 0.0) if providers == ["Genius"] else ("\n".join(_WRONG_EDITION), False, 0.0)

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", providers)

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 10.0, [], _FakeAnthropicClient(), audio_check=_audio_check_for(_SUNG),
    )

    assert lines == half_right
    assert source == "Genius"
    assert "match what is sung" in concern
    assert "lines 3-4" in concern
