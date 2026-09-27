"""owner_whisper.py: the owner's corrections to specific WHISPER-heard lines, layered over the raw transcript --
confirmed against real evidence (Boris the Spider, 2026-09-27): the fetched/edited lyrics were exactly right while
Whisper genuinely mis-transcribed or skipped several passages, scoring correctly-timed lines as "out of sync"
purely because Whisper's own guess at the words was wrong there."""

import json

import pytest

from lyricvideo.anchors import HeardWord
from lyricvideo.models import ChordTrack, LyricLine, Song, Word, save_song
from lyricvideo.owner_whisper import (
    OWNER_WHISPER_FILE, corrected_heard_words, owner_whisper_corrections, save_owner_whisper_line,
)


def _write_song(work_dir, lines_words: list[list[tuple[str, float, float]]]) -> None:
    lines = [LyricLine(words=[Word(w, s, e) for w, s, e in words], start_time=words[0][1], end_time=words[-1][2])
              for words in lines_words]
    save_song(Song(title="T", audio_path="a.mp3", lines=lines, chord_track=ChordTrack()), work_dir / "lyrics_timed.json")


def _write_transcript(work_dir, words: list[tuple[str, float, float]]) -> None:
    (work_dir / "transcript.json").write_text(json.dumps({
        "model": "medium", "vocals_bytes": 1, "language_requested": "en", "language": "en", "text": "",
        "word_timestamps": True, "words": [{"word": w, "start": s, "end": e} for w, s, e in words], "segments": [],
    }), encoding="utf-8")


LINE_0 = [("look", 0.0, 0.3), ("he's", 0.3, 0.5), ("crawling", 0.5, 0.9), ("up", 0.9, 1.0), ("my", 1.0, 1.1), ("wall", 1.1, 1.4)]
LINE_1 = [("there", 10.0, 10.2), ("he", 10.2, 10.3), ("is", 10.3, 10.4), ("wrapped", 10.4, 10.7), ("in", 10.7, 10.8), ("a", 10.8, 10.85), ("ball", 10.85, 11.1)]


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / "boris-the-spider"
    d.mkdir()
    _write_song(d, [LINE_0, LINE_1])
    # Whisper genuinely garbled/skipped line 1 (real incident): nothing recognizable near 10-11s, only line 0's own
    # (correctly heard) words plus unrelated noise far away.
    _write_transcript(d, [("look", 0.05, 0.3), ("he's", 0.3, 0.5), ("calling", 0.5, 0.9), ("up", 0.9, 1.0),
                           ("my", 1.0, 1.1), ("wall", 1.1, 1.4), ("b", 20.0, 20.05), ("b", 20.05, 20.1)])
    return d


def test_no_correction_file_leaves_the_raw_heard_words_unchanged(work_dir):
    heard = corrected_heard_words(work_dir)
    assert [hw.word for hw in heard] == ["look", "he's", "calling", "up", "my", "wall", "b", "b"]


def test_a_saved_correction_adds_synthetic_words_across_that_lines_own_placed_span_without_deleting_the_raw_ones(work_dir):
    save_owner_whisper_line(work_dir, 1, "there he is wrapped in a ball", "there he is wrapped in a ball")

    heard = corrected_heard_words(work_dir)

    raw = [hw for hw in heard if hw.word in ("look", "he's", "calling", "up", "my", "wall", "b")]
    assert len(raw) == 8  # every raw Whisper word is still there, untouched
    added = [hw for hw in heard if hw.word not in ("look", "he's", "calling", "up", "my", "wall", "b")]
    assert [hw.word for hw in added] == ["there", "he", "is", "wrapped", "in", "a", "ball"]
    # spread evenly across the LINE's OWN placed span (10.0-11.1), in order, never outside it
    starts = [hw.start for hw in added]
    assert starts == sorted(starts)
    assert 10.0 <= starts[0] and starts[-1] <= 11.1


def test_the_correction_makes_a_previously_unmatched_line_score_in_sync(work_dir):
    """The real point of the feature: a line Whisper never heard scores OUT before the correction, IN after --
    proves the synthetic timestamps actually land inside the tolerance timing_gate itself checks against."""
    from lyricvideo.timing_gate import TOLERANCE_SECONDS, heard_text_near_line
    from lyricvideo.models import load_song

    song = load_song(work_dir / "lyrics_timed.json")
    before = heard_text_near_line(song.lines[1].words, corrected_heard_words(work_dir))
    assert before == ""  # nothing of line 1's own words heard nearby (the stray "b b" is far outside its own +-SEARCH_SECONDS window)

    save_owner_whisper_line(work_dir, 1, "there he is wrapped in a ball", "there he is wrapped in a ball")

    after = corrected_heard_words(work_dir)
    for word, (start, _end) in zip(["there", "he", "is", "wrapped", "in", "a", "ball"], [(w.start_time, w.end_time) for w in song.lines[1].words]):
        match = min((abs(start - hw.start) for hw in after if hw.word == word), default=None)
        assert match is not None and match <= TOLERANCE_SECONDS


def test_a_correction_for_a_line_whose_text_has_since_changed_is_ignored(work_dir):
    save_owner_whisper_line(work_dir, 1, "there he is wrapped in a ball", "This line text is now stale")

    heard = corrected_heard_words(work_dir)

    assert [hw.word for hw in heard] == ["look", "he's", "calling", "up", "my", "wall", "b", "b"]  # unchanged


def test_a_correction_out_of_range_of_the_current_lines_is_ignored(work_dir):
    save_owner_whisper_line(work_dir, 7, "some text", "some text")

    heard = corrected_heard_words(work_dir)

    assert [hw.word for hw in heard] == ["look", "he's", "calling", "up", "my", "wall", "b", "b"]


def test_saving_a_second_rows_correction_does_not_clobber_the_first(work_dir):
    save_owner_whisper_line(work_dir, 0, "look he's crawling up my wall", "look he's crawling up my wall")
    save_owner_whisper_line(work_dir, 1, "there he is wrapped in a ball", "there he is wrapped in a ball")

    saved = owner_whisper_corrections(work_dir)

    assert saved[0] == "look he's crawling up my wall"
    assert saved[1] == "there he is wrapped in a ball"


def test_saving_a_blank_correction_clears_it_back_to_raw_whisper(work_dir):
    save_owner_whisper_line(work_dir, 1, "there he is wrapped in a ball", "there he is wrapped in a ball")

    save_owner_whisper_line(work_dir, 1, "", "There he is wrapped in a ball")

    assert 1 not in owner_whisper_corrections(work_dir)
    heard = corrected_heard_words(work_dir)
    assert [hw.word for hw in heard] == ["look", "he's", "calling", "up", "my", "wall", "b", "b"]


def test_owner_whisper_corrections_is_empty_with_no_file(work_dir):
    assert owner_whisper_corrections(work_dir) == {}


def test_a_single_word_line_still_gets_a_correction_at_that_words_own_time(tmp_path):
    d = tmp_path / "some-song"
    d.mkdir()
    _write_song(d, [[("wall", 1.0, 1.4)]])
    _write_transcript(d, [])

    save_owner_whisper_line(d, 0, "wall", "wall")

    heard = corrected_heard_words(d)
    assert len(heard) == 1 and heard[0].word == "wall" and 1.0 <= heard[0].start <= 1.4


def test_multiple_spaces_in_a_correction_do_not_produce_empty_words(tmp_path):
    d = tmp_path / "some-song"
    d.mkdir()
    _write_song(d, [[("a", 0.0, 0.2), ("b", 0.2, 0.4)]])
    _write_transcript(d, [])

    save_owner_whisper_line(d, 0, "a   b", "a b")

    heard = corrected_heard_words(d)
    assert [hw.word for hw in heard] == ["a", "b"]


def test_the_stored_file_shape_survives_a_read_after_write(work_dir):
    save_owner_whisper_line(work_dir, 1, "there he is wrapped in a ball", "there he is wrapped in a ball")

    data = json.loads((work_dir / OWNER_WHISPER_FILE).read_text(encoding="utf-8"))

    assert data["1"]["text"] == "there he is wrapped in a ball"
    assert data["1"]["line_text"] == "there he is wrapped in a ball"


# --- add_corrections: for a caller mid-alignment (pipeline._align_lyrics) that has a winning candidate's own final
# per-word times but nothing saved to disk yet -- real incident, 2026-09-27: a Redo re-ran _align_lyrics from scratch
# and reproduced the IDENTICAL "out of sync" concern even after the owner saved a correction, because only the
# read-only re-judge (timing_gate) and the display popup consulted it -- never the concern a fresh render computes.

def test_add_corrections_reads_the_same_whisper_owner_json_as_corrected_heard_words(tmp_path):
    from lyricvideo.owner_whisper import add_corrections

    d = tmp_path / "some-song"
    d.mkdir()
    save_owner_whisper_line(d, 1, "my friend today", "my friend today")
    heard = [HeardWord("garbled", 10.0, 10.3)]
    line_words = [["hello", "there"], ["my", "friend", "today"]]
    times = [(0.0, 0.4), (0.5, 0.9), (10.0, 10.3), (10.4, 10.7), (10.8, 11.1)]

    out = add_corrections(d, heard, line_words, times)

    raw = [hw for hw in out if hw.word == "garbled"]
    added = [hw for hw in out if hw.word != "garbled"]
    assert len(raw) == 1
    assert [hw.word for hw in added] == ["my", "friend", "today"]
    # 3 correction tokens for 3 real words: each lands at THAT word's own real time (position-matched), not spread
    # evenly across the line's overall span.
    assert [hw.start for hw in added] == [pytest.approx(t[0]) for t in times[2:5]]


def test_add_corrections_ignores_a_row_whose_line_text_does_not_match(tmp_path):
    from lyricvideo.owner_whisper import add_corrections

    d = tmp_path / "some-song"
    d.mkdir()
    save_owner_whisper_line(d, 1, "my friend today", "a different line entirely")
    line_words = [["hello", "there"], ["my", "friend", "today"]]
    times = [(0.0, 0.4), (0.5, 0.9), (10.0, 10.3), (10.4, 10.7), (10.8, 11.1)]

    out = add_corrections(d, [], line_words, times)

    assert out == []


def test_add_corrections_returns_heard_unchanged_with_no_correction_file(tmp_path):
    from lyricvideo.owner_whisper import add_corrections

    d = tmp_path / "some-song"
    d.mkdir()
    heard = [HeardWord("hello", 0.0, 0.4)]

    assert add_corrections(d, heard, [["hello"]], [(0.0, 0.4)]) == heard


def test_add_corrections_skips_a_blank_line_the_same_way_row_numbering_does(tmp_path):
    """line_words can include a blank line (zero words) that whisper_lines_for/current_lyric_line_texts skip when
    numbering rows for the GUI -- row numbers here must line up with THAT numbering, not a raw position in
    line_words, or a correction saved via the popup would land on the wrong line."""
    from lyricvideo.owner_whisper import add_corrections

    d = tmp_path / "some-song"
    d.mkdir()
    save_owner_whisper_line(d, 1, "second real line", "second real line")   # row 1 = the SECOND non-blank line
    line_words = [["first"], [], ["second", "real", "line"]]                # a blank line sits between them
    times = [(0.0, 0.3), (10.0, 10.3), (10.4, 10.7), (10.8, 11.1)]           # flat, one per WORD -- the blank line has none

    out = add_corrections(d, [], line_words, times)

    assert [hw.word for hw in out] == ["second", "real", "line"]
    # 3 correction tokens for 3 real words: position-matched to the line's own real per-word times (indices 1-3
    # in `times`, since the blank line before it contributes no entries).
    assert [hw.start for hw in out] == [pytest.approx(t[0]) for t in times[1:4]]


def test_add_corrections_places_each_word_at_its_own_real_words_time_when_the_count_matches(tmp_path):
    """Real incident, 2026-09-27: spreading a correction EVENLY across a line's overall span drifted an individual
    word's synthetic position more than half a second from where the aligner actually placed THAT word, on a line
    whose real per-word pacing is uneven (some words sung longer than others) -- so the line still failed the sync
    check even with a correct, fully-saved correction. When the correction has the SAME word count as the real
    aligned line, each corrected word must land at that SPECIFIC word's own real time, not an interpolated guess."""
    from lyricvideo.owner_whisper import add_corrections

    d = tmp_path / "some-song"
    d.mkdir()
    save_owner_whisper_line(d, 0, "He's come to a sticky end", "He's come to a sticky end")
    line_words = [["He's", "come", "to", "a", "sticky", "end"]]
    # Deliberately UNEVEN real pacing (not what a naive even-spread across [117.31, 121.61] would predict):
    times = [(117.31, 117.6), (117.7, 117.9), (118.0, 118.1), (118.15, 118.2), (118.3, 120.9), (121.3, 121.61)]

    out = add_corrections(d, [], line_words, times)

    assert [hw.word for hw in out] == ["He's", "come", "to", "a", "sticky", "end"]
    for hw, (real_start, _real_end) in zip(out, times):
        assert hw.start == pytest.approx(real_start)


def test_add_corrections_falls_back_to_spreading_across_the_span_when_word_counts_differ(tmp_path):
    from lyricvideo.owner_whisper import add_corrections

    d = tmp_path / "some-song"
    d.mkdir()
    save_owner_whisper_line(d, 0, "extra words here now", "original line")   # 4 tokens, but the line has 2 words
    line_words = [["original", "line"]]
    times = [(10.0, 10.3), (10.4, 10.9)]

    out = add_corrections(d, [], line_words, times)

    assert [hw.word for hw in out] == ["extra", "words", "here", "now"]
    assert out[0].start == pytest.approx(10.0) and out[-1].start == pytest.approx(10.9)
