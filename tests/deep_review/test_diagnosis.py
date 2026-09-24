"""deep_review.diagnosis: why is a set-aside song actually failing, before spending any research effort on
it? Four outcomes: it already passes now (a stale concern, or the bar moved), the source audio itself is
unusable, the lyrics text looks wrong for the recording, or the lyrics look right and it's purely an
alignment/timing-precision problem (out of scope for a lyrics fix -- see TODO.md's aligner experiment).
Song text is invented."""

import json

import pytest

from lyricvideo.models import LyricLine, Song, Word, save_song

from deep_review.diagnosis import Category, diagnose_song

LINES = [
    "the river runs beside the mill",
    "and morning fog lies on the hill",
    "a lantern swings above the door",
    "i wait for you like i waited",
    "your letters crossed the sea",
    "each candle burned until the dawn",
    "we carved our names into the gate",
    "no train will carry us away",
]
WORDS = [line.split() for line in LINES]
LINE_START = [10.0 + 8.0 * k for k in range(len(LINES))]   # 8s apart: SEARCH_SECONDS (2.5s) never bridges lines


def _line(k: int, shift: float = 0.0) -> LyricLine:
    """Lyric line k, its words placed `shift` seconds away from where they're actually sung."""
    words = [
        Word(w, LINE_START[k] + 0.4 * i + shift, LINE_START[k] + 0.4 * i + 0.3 + shift)
        for i, w in enumerate(WORDS[k])
    ]
    return LyricLine(words=words)


def _heard_words(k: int, texts: list[str] | None = None) -> list[dict]:
    """What Whisper actually heard around line k's own true time -- by default, its own (correct) words."""
    texts = texts if texts is not None else WORDS[k]
    return [
        {"word": w, "start": LINE_START[k] + 0.4 * i, "end": LINE_START[k] + 0.4 * i + 0.3}
        for i, w in enumerate(texts)
    ]


def _write_song(work_dir, lines, concern=""):
    song = Song(title="Test Song", audio_path="song.mp3", lines=lines, lyrics_accuracy_concern=concern)
    save_song(song, work_dir / "lyrics_timed.json")


def _write_transcript(work_dir, words):
    (work_dir / "transcript.json").write_text(
        json.dumps({"text": " ".join(w["word"] for w in words), "words": words}), encoding="utf-8",
    )


def test_a_song_with_no_recorded_concern_already_passes(tmp_path):
    _write_song(tmp_path, [_line(k) for k in range(8)])

    result = diagnose_song(tmp_path)

    assert result.category == Category.ALREADY_PASSES


def test_a_concern_about_damaged_source_audio_is_not_fixable_by_lyrics_work(tmp_path):
    concern = "SET ASIDE FOR REVIEW -- damaged source audio: '10 Song.m4a' is a partial file."
    _write_song(tmp_path, [_line(k) for k in range(8)], concern=concern)

    result = diagnose_song(tmp_path)

    assert result.category == Category.DAMAGED_AUDIO


def test_a_stale_concern_the_current_gate_would_actually_pass_is_cleared_not_researched(tmp_path):
    """The exact 2026-09-21 bug: an old-wording concern the fix never retroactively cleared."""
    concern = "SET ASIDE FOR REVIEW -- only 94% of the words start within half a second of where they are sung."
    lines = [_line(k) for k in range(8)]   # every line placed exactly on the singing: passes today
    _write_song(tmp_path, lines, concern=concern)
    _write_transcript(tmp_path, [w for k in range(8) for w in _heard_words(k)])

    result = diagnose_song(tmp_path)

    assert result.category == Category.ALREADY_PASSES


def test_a_non_gate_concern_is_diagnosed_as_lyrics_wrong_without_needing_a_transcript(tmp_path):
    """A lyric_audio_match-style concern ("Only X% of these lyrics match what is sung") already IS the
    text-correctness diagnosis -- no need to re-derive it from scratch."""
    concern = "Only 58% of these lyrics match what is sung; lines 1-3, 5 could not be matched."
    _write_song(tmp_path, [_line(k) for k in range(8)], concern=concern)

    result = diagnose_song(tmp_path)

    assert result.category == Category.LYRICS_WRONG


def test_out_of_sync_lines_whose_actual_words_still_match_are_alignment_only(tmp_path):
    """Lines 7-8 are placed 0.8s off (fails the 0.5s tolerance) but Whisper hears the SAME words nearby
    (within the wider 2.5s search) -- correct lyrics, just imprecise alignment. Not this program's job."""
    lines = [_line(k) for k in range(6)] + [_line(6, shift=0.8), _line(7, shift=0.8)]
    concern = (
        "SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only 75% of the lines start "
        "within half a second of where they are sung (90% are needed); lines 7, 8 are off."
    )
    _write_song(tmp_path, lines, concern=concern)
    heard = [w for k in range(8) for w in _heard_words(k)]   # Whisper hears every line's TRUE (correct) words
    _write_transcript(tmp_path, heard)

    result = diagnose_song(tmp_path)

    assert result.category == Category.ALIGNMENT_ONLY


def test_out_of_sync_lines_whose_actual_words_are_different_are_lyrics_wrong(tmp_path):
    """Lines 7-8 are placed right where the singing is, but what's actually sung there is completely
    different words -- the lyric file itself is wrong for this recording."""
    lines = [_line(k) for k in range(8)]
    concern = (
        "SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only 75% of the lines start "
        "within half a second of where they are sung (90% are needed); lines 7, 8 are off."
    )
    _write_song(tmp_path, lines, concern=concern)
    heard = [w for k in range(6) for w in _heard_words(k)]
    heard += _heard_words(6, ["neon", "signs", "flash", "downtown"])
    heard += _heard_words(7, ["dancing", "underneath", "the", "streetlights"])
    _write_transcript(tmp_path, heard)

    result = diagnose_song(tmp_path)

    assert result.category == Category.LYRICS_WRONG
    assert result.mismatched_lines == [7, 8]


def test_a_song_with_no_transcript_and_a_gate_concern_is_unknown(tmp_path):
    concern = (
        "SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only 75% of the lines start "
        "within half a second of where they are sung (90% are needed); lines 7, 8 are off."
    )
    _write_song(tmp_path, [_line(k) for k in range(8)], concern=concern)

    result = diagnose_song(tmp_path)

    assert result.category == Category.UNKNOWN


def test_a_missing_lyrics_timed_json_is_unknown(tmp_path):
    result = diagnose_song(tmp_path)

    assert result.category == Category.UNKNOWN
