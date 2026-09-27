import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo.models import ChordTrack, LyricLine, Song, Word, save_song


def _make_owner_song(work_dir, lines_words, source="owner", title="T"):
    """A song directory with a real lyrics_timed.json and a real (empty) vocal-stem file at exactly the path
    load_redo_inputs()/whisper_text_for() resolve for audio_path="a.mp3" (no local audio copy exists, so
    load_redo_inputs falls back to the bare "a.mp3" -- .stem == "a")."""
    work_dir.mkdir(parents=True)
    lines = [LyricLine(words=[Word(w, s, e) for w, s, e in words]) for words in lines_words]
    save_song(Song(title=title, audio_path="a.mp3", lines=lines, lyrics_source=source, chord_track=ChordTrack()),
              work_dir / "lyrics_timed.json")
    vocals_path = work_dir / "htdemucs" / "a" / "vocals.wav"
    vocals_path.parent.mkdir(parents=True)
    vocals_path.touch()


def test_find_owner_lyrics_songs_finds_only_owner_sourced_songs(tmp_path):
    from scripts.revalidate_hotwords import find_owner_lyrics_songs

    _make_owner_song(tmp_path / "work" / "owner-song", [[("hello", 0.0, 0.4)]])
    _make_owner_song(tmp_path / "work" / "fetched-song", [[("hello", 0.0, 0.4)]], source="lrclib")

    found = find_owner_lyrics_songs(tmp_path / "work")

    assert [p.name for p in found] == ["owner-song"]


def test_find_fetched_lyrics_songs_finds_only_non_owner_sourced_songs_with_word_timings(tmp_path):
    from scripts.revalidate_hotwords import find_fetched_lyrics_songs

    _make_owner_song(tmp_path / "work" / "owner-song", [[("hello", 0.0, 0.4)]])
    _make_owner_song(tmp_path / "work" / "fetched-song", [[("hello", 0.0, 0.4)]], source="lrclib")

    found = find_fetched_lyrics_songs(tmp_path / "work")

    assert [p.name for p in found] == ["fetched-song"]


class _FakeModel:
    """transcribe() returns exactly the given (word, start, end) triples as one segment's word timings."""

    def __init__(self, words):
        self.words = words

    def transcribe(self, path, **kwargs):
        words = [SimpleNamespace(word=w, start=s, end=e) for w, s, e in self.words]
        seg = SimpleNamespace(start=self.words[0][1], end=self.words[-1][2], text=" ".join(w for w, _, _ in self.words), words=words)
        return [seg], SimpleNamespace(language="en")


def _write_transcript(work_dir, words):
    """The song's EXISTING transcript.json (built without hotwords, in real use) -- revalidate_one's "before" score
    comes from this file, exactly as it would for a real already-processed song (whisper_text_for's own docstring:
    every song this script scans already has one)."""
    (work_dir / "transcript.json").write_text(json.dumps({
        "model": "medium", "vocals_bytes": 1, "language_requested": "en", "language": "en", "text": "",
        "word_timestamps": True, "hotwords": "", "segments": [],
        "words": [{"word": w, "start": s, "end": e} for w, s, e in words],
    }), encoding="utf-8")


def test_revalidate_one_reports_no_regression_when_the_score_holds_or_improves(tmp_path):
    from scripts.revalidate_hotwords import revalidate_one

    song_dir = tmp_path / "some-song"
    # 8 identical two-word lines, 8 s apart, so no line hears its neighbour -- enough judged lines to score.
    lines_words = [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)] for i in range(8)]
    _make_owner_song(song_dir, lines_words)
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)                                     # "before": already agrees perfectly
    fake = _FakeModel(flat)                                               # "after" (fresh, with hotwords): the same

    result = revalidate_one(song_dir, model=fake)

    assert result["kind"] == "owner"
    assert result["before_share"] == 1.0 and result["after_share"] == 1.0
    assert result["regressed"] is False


def test_revalidate_one_flags_a_real_regression(tmp_path):
    from scripts.revalidate_hotwords import revalidate_one

    song_dir = tmp_path / "some-song"
    lines_words = [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)] for i in range(8)]
    _make_owner_song(song_dir, lines_words)
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)                                     # "before": already agrees perfectly
    # "after" (fresh, worse): line 0's own two words are heard as different text at the SAME times, PLUS one more
    # nearby wrong word -- timing_gate.MIN_HEARD_NEARBY (3) requires at least 3 heard tokens near a line before it
    # is scored OUT rather than excused as "unjudged" (too little heard to say); two alone left it unjudged, which
    # this test caught (found running the plan's own version of this fixture, 2026-09-27). Every other line
    # unchanged.
    worse_words = [("wrong", 0.0, 0.4), ("words", 0.5, 0.9), ("here", 1.0, 1.4)] + flat[2:]
    worse = _FakeModel(worse_words)

    result = revalidate_one(song_dir, model=worse)

    assert result["before_share"] == 1.0
    assert result["regressed"] is True and result["after_share"] == pytest.approx(7 / 8)


def test_revalidate_one_hints_a_fetched_song_from_its_own_saved_lines(tmp_path):
    """Decoupled design (owner + review, 2026-09-27): the real pipeline no longer hints the audio CHECK with a
    pre-fetched candidate at all -- it hints a SEPARATE, later re-transcription with whatever candidate was
    actually ACCEPTED, which for an already-saved song is simply that song's own saved lyric lines. So
    revalidate_one's hint-building is the SAME for an owner-edited song and a fetched-and-accepted one: no
    network lookup, no fetch_lyric_lines call at all -- just the song's own current text."""
    from scripts.revalidate_hotwords import revalidate_one

    song_dir = tmp_path / "some-song"
    lines_words = [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)] for i in range(8)]
    _make_owner_song(song_dir, lines_words, source="lrclib")
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)
    fake = _FakeModel(flat)

    result = revalidate_one(song_dir, model=fake)

    assert result["kind"] == "fetched"
    assert result["before_share"] == 1.0 and result["after_share"] == 1.0


def test_revalidate_one_applies_the_owners_saved_corrections_to_the_after_score_too(tmp_path):
    """Real bug found in review, 2026-09-27: the "before" score used corrected_heard_words() (raw Whisper words
    PLUS any saved owner_whisper.json corrections), but the "after" score used raw heard words only -- an
    apples-to-oranges comparison that could mask a real regression, or manufacture a fake one, on any song with a
    saved correction (a real case: Boris the Spider). Production applies the SAME corrections to a fresh
    transcript's words too (owner_whisper.add_corrections(), used by pipeline._align_lyrics's own re-score) --
    revalidate_one must match that, not compare a corrected "before" against an uncorrected "after"."""
    from scripts.revalidate_hotwords import revalidate_one

    song_dir = tmp_path / "some-song"
    # Line 0 is "Boris the spider" at real times; the fresh "after" transcript mishears it as "wrong wrong wrong"
    # (garbage, unrelated to what is really sung) -- exactly like the real Boris the Spider incident, where an
    # owner correction exists for a line Whisper cannot itself recognize. Lines 1-7 stay perfectly heard so there
    # are enough judged lines to score (MIN_JUDGED_LINES = 8).
    lines_words = [[("boris", 0.0, 0.4), ("the", 0.5, 0.7), ("spider", 0.8, 1.2)]]
    lines_words += [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)]
                    for i in range(1, 8)]
    _make_owner_song(song_dir, lines_words)
    (song_dir / "whisper_owner.json").write_text(json.dumps({
        "0": {"text": "Boris the spider", "line_text": "boris the spider"},
    }), encoding="utf-8")
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)                                     # "before": raw + the saved correction
    after_words = [("wrong", 0.0, 0.4), ("wrong", 0.5, 0.7), ("wrong", 0.8, 1.2)] + flat[3:]
    fake = _FakeModel(after_words)                                        # "after": same garbled miss, corrected

    result = revalidate_one(song_dir, model=fake)

    assert result["before_share"] == 1.0                                  # correction confirms line 0 too
    assert result["after_share"] == 1.0                                   # the SAME correction must apply here
    assert result["regressed"] is False


def test_revalidate_one_flags_when_a_song_scoreable_before_cannot_be_scored_after(tmp_path):
    """The validation script must not silently "pass" a song it could not actually re-score (e.g. hinting
    caused so few lines to be heard that too few are judged, MIN_JUDGED_LINES) as a false negative for "no
    regression" -- this exact case is its own reportable status, not indistinguishable from "no change"."""
    from scripts.revalidate_hotwords import revalidate_one

    song_dir = tmp_path / "some-song"
    lines_words = [[("hello", 8.0 * i, 8.0 * i + 0.4), ("there", 8.0 * i + 0.5, 8.0 * i + 0.9)] for i in range(8)]
    _make_owner_song(song_dir, lines_words)
    flat = [w for line in lines_words for w in line]
    _write_transcript(song_dir, flat)                                     # "before": scoreable, 1.0
    fake = _FakeModel([("um", 0.0, 0.2)])                                 # "after": almost nothing heard anywhere

    result = revalidate_one(song_dir, model=fake)

    assert result["before_share"] == 1.0
    assert result["after_share"] is None
    assert result["unscorable_after"] is True
    assert result["regressed"] is False                                  # "regressed" stays strictly score-drop
