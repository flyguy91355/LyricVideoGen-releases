"""Issue #7 ("the pages opens very slowly"): the song lists (Upload to YouTube, Pending, Flagged, EASY backfill) parsed every
song's lyrics_timed.json three or four times plus its transcript and a full sync check on EVERY open, wrote timing holds back
into song files from the Tk thread and the 20-minute tick at once, and one unreadable lyrics_timed.json broke every list for
every song. These pin the fixes: one parse per version of a file, read-only lists, one bad file skipped. Song text is invented."""

import json
import os
import threading

import pytest

from lyricvideo import pipeline, timing_gate
from lyricvideo.anchors import HeardWord
from lyricvideo.models import ChordTrack, LyricLine, Song, Word, load_song, save_song
from lyricvideo.pipeline import (
    list_easy_chord_backfill_candidates, list_flagged_songs, list_pending_uploads, list_rendered_songs,
    list_uploadable_songs, needs_review, song_video_path,
)
from lyricvideo.timing_gate import use_pass_share_from

LINES = [
    "the kettle hums a quiet morning tune",
    "and paper boats go sailing past the moon",
    "a copper key beneath the garden stone",
    "the orchard keeps the secrets we have known",
    "our footsteps fade along the chalky shore",
    "a sparrow taps its rhythm on the door",
    "the lamplight pools upon the willow bark",
    "we trace the rivers painted in the dark",
    "a fiddle tune drifts over barley rows",
    "and every lantern flickers as it goes",
]
WORDS = [line.split() for line in LINES]
FLAT = [w for words in WORDS for w in words]
LINE_START = [10.0 + 8.0 * k for k in range(len(LINES))]
TRUE = [LINE_START[k] + 0.4 * i for k, words in enumerate(WORDS) for i in range(len(words))]
HEARD = [HeardWord(w, t, t + 0.35) for w, t in zip(FLAT, TRUE)]


@pytest.fixture(autouse=True)
def _default_bar_and_logs(tmp_path, monkeypatch):
    from lyricvideo import cleared_log
    monkeypatch.setattr(cleared_log, "LOG_FILE", tmp_path / "cleared.json")
    use_pass_share_from(None)
    yield
    use_pass_share_from(None)


def placed(shift_by_line=None):
    shift_by_line = shift_by_line or {}
    out = []
    for k, words in enumerate(WORDS):
        for _ in words:
            s = TRUE[len(out)] + shift_by_line.get(k, 0.0)
            out.append((s, s + 0.3))
    return out


def _song(times, title="T", concern="", key="C major"):
    lines, n = [], 0
    for words in WORDS:
        lines.append(LyricLine(words=[Word(w, times[n + i][0], times[n + i][1]) for i, w in enumerate(words)]))
        n += len(words)
    return Song(title=title, audio_path="a.mp3", lines=lines, chord_track=ChordTrack(key=key), lyrics_accuracy_concern=concern)


def _rendered(song_dir, times, title="T", concern="", key="C major", transcript=True):
    """A rendered song with a settled key, as every song made since 2026-09-26 has."""
    from lyricvideo.key_decision import KeyDecision, save_decision
    song_dir.mkdir(parents=True)
    save_song(_song(times, title, concern, key), song_dir / "lyrics_timed.json")
    if transcript:
        (song_dir / "transcript.json").write_text(json.dumps({"words": [
            {"word": h.word, "start": h.start, "end": h.end} for h in HEARD]}), encoding="utf-8")
    save_decision(song_dir, KeyDecision(status="confirmed", key=key, source="agreed", chord_key=key))
    (song_dir / f"{pipeline.slugify(title)}.mp4").write_bytes(b"video")
    # What an EASY CHORD version is built from (pipeline.easy_chord_build_problem): the song's title and its audio copy.
    (song_dir / "song_info.json").write_text(json.dumps({"title": title, "artist": "a", "duration": 90.0}), encoding="utf-8")
    (song_dir / "a.mp3").write_bytes(b"audio")


def _count_parses(monkeypatch):
    counts = {"load_song": 0, "transcript": 0}

    def counting_load(path):
        counts["load_song"] += 1
        return load_song(path)

    real_words = timing_gate.load_transcript_words

    def counting_words(work_dir):
        counts["transcript"] += 1
        return real_words(work_dir)

    monkeypatch.setattr(pipeline, "load_song", counting_load)
    monkeypatch.setattr(timing_gate, "load_song", counting_load)
    monkeypatch.setattr(timing_gate, "load_transcript_words", counting_words)
    return counts


def _work(tmp_path):
    root = tmp_path / "work"
    _rendered(root / "alpha", placed())
    _rendered(root / "bravo", placed({1: 1.0, 6: -1.0}))                   # 80%: fails the 90% bar
    _rendered(root / "charlie", placed({4: 1.0}))                          # exactly 90%
    _rendered(root / "delta", placed(), concern="Only 60% of these lyrics match what is sung.")
    _rendered(root / "echo", placed(), key="Eb major")
    (root / "echo" / "youtube_state.json").write_text("{}", encoding="utf-8")
    return root


def test_every_list_parses_each_song_file_at_most_once_and_a_repeat_parses_nothing(tmp_path, monkeypatch):
    root = _work(tmp_path)
    counts = _count_parses(monkeypatch)

    first = {
        "flagged": list_flagged_songs(root, include_uploaded=True), "upload": list_uploadable_songs(root),
        "pending": list_pending_uploads(root), "rendered": list_rendered_songs(root),
        "easy": list_easy_chord_backfill_candidates(root), "tick": list_flagged_songs(root),
    }
    assert counts["load_song"] <= 5 and counts["transcript"] <= 5          # one parse per song folder, across ALL lists

    before = dict(counts)
    again = {
        "flagged": list_flagged_songs(root, include_uploaded=True), "upload": list_uploadable_songs(root),
        "pending": list_pending_uploads(root), "rendered": list_rendered_songs(root),
        "easy": list_easy_chord_backfill_candidates(root), "tick": list_flagged_songs(root),
    }
    assert counts == before                                                # nothing changed on disk: nothing re-parsed
    assert again == first
    assert first["upload"] == ["alpha", "charlie", "echo"]
    assert first["pending"] == ["alpha", "charlie"]
    assert first["flagged"] == ["bravo", "delta"]
    assert first["easy"] == ["echo"]


def test_a_changed_file_is_parsed_again_and_only_that_one(tmp_path, monkeypatch):
    root = _work(tmp_path)
    counts = _count_parses(monkeypatch)
    list_uploadable_songs(root)
    before = counts["load_song"]

    song = load_song(root / "alpha" / "lyrics_timed.json")
    song.chord_track = ChordTrack(key="Ab major")                         # a key correction rewrites the file
    save_song(song, root / "alpha" / "lyrics_timed.json")

    assert list_easy_chord_backfill_candidates(root) == ["alpha", "echo"]
    assert counts["load_song"] == before + 1


def test_moving_the_bar_re_judges_every_song_without_parsing_anything_again(tmp_path, monkeypatch):
    root = _work(tmp_path)
    counts = _count_parses(monkeypatch)
    use_pass_share_from(lambda: 0.90)
    assert list_pending_uploads(root) == ["alpha", "charlie"]
    before = dict(counts)

    use_pass_share_from(lambda: 0.95)
    assert list_pending_uploads(root) == ["alpha"]
    assert "charlie" in list_flagged_songs(root)
    use_pass_share_from(lambda: 0.80)
    assert list_pending_uploads(root) == ["alpha", "bravo", "charlie"]
    assert counts == before


def _snapshot(root):
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_no_list_ever_writes_a_song_file_even_when_the_bar_moves(tmp_path):
    root = _work(tmp_path)
    save_song(_song(placed({1: 1.0, 6: -1.0}), concern=timing_gate.check_sync(WORDS, placed({1: 1.0, 6: -1.0}), HEARD, 0.9).concern),
              root / "bravo" / "lyrics_timed.json")                          # a stored hold from an earlier render
    before = _snapshot(root)

    for bar in (0.90, 0.95, 0.80, 0.70):
        use_pass_share_from(lambda bar=bar: bar)
        list_flagged_songs(root, include_uploaded=True)
        list_flagged_songs(root)
        list_pending_uploads(root)
        list_uploadable_songs(root)
        list_easy_chord_backfill_candidates(root)
        needs_review(root / "bravo")

    assert _snapshot(root) == before                                       # read-only: not a byte, not a new file


def test_the_review_reason_follows_the_bar_although_nothing_is_written(tmp_path):
    root = _work(tmp_path)

    assert "80%" in pipeline.review_concern(root / "bravo")
    assert pipeline.review_concern(root / "delta").startswith("Only 60%")    # another check's reason, as stored
    use_pass_share_from(lambda: 0.95)
    reason = pipeline.review_concern(root / "charlie")
    assert "90%" in reason and "95% are needed" in reason
    use_pass_share_from(lambda: 0.80)
    assert pipeline.review_concern(root / "bravo") == ""


@pytest.mark.parametrize("damage", ["", '{"title": "T b-so', "[1, 2", '{"lines": []}'])
def test_one_unreadable_lyrics_timed_json_is_skipped_and_never_breaks_a_list(tmp_path, capsys, damage):
    root = _work(tmp_path)
    _rendered(root / "foxtrot", placed())
    (root / "foxtrot" / "lyrics_timed.json").write_text(damage, encoding="utf-8")

    for _ in range(3):
        assert list_rendered_songs(root) == ["alpha", "bravo", "charlie", "delta", "echo"]
        assert list_uploadable_songs(root) == ["alpha", "charlie", "echo"]
        assert list_pending_uploads(root) == ["alpha", "charlie"]
        assert list_flagged_songs(root, include_uploaded=True) == ["bravo", "delta"]
        assert list_easy_chord_backfill_candidates(root) == ["echo"]

    assert song_video_path(root / "foxtrot") is None
    assert needs_review(root / "foxtrot")                                   # fail closed: never offered for upload
    warnings = [line for line in capsys.readouterr().err.splitlines() if "foxtrot" in line]
    assert len(warnings) == 1                                              # named once, not on every list build


def test_lists_built_on_two_threads_at_once_agree_and_write_nothing(tmp_path):
    root = _work(tmp_path)
    before = _snapshot(root)
    results, errors = [], []

    def build():
        try:
            for _ in range(5):
                results.append((tuple(list_flagged_songs(root, include_uploaded=True)), tuple(list_pending_uploads(root))))
        except Exception as e:                                             # pragma: no cover -- reported below
            errors.append(e)

    threads = [threading.Thread(target=build) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [] and len(set(results)) == 1
    assert _snapshot(root) == before


def test_the_upload_list_and_the_hidden_count_scan_share_one_parse(tmp_path, monkeypatch):
    """gui._uploadable_songs lists the uploadable songs and then list_rendered_songs again for the "N hidden" note."""
    root = _work(tmp_path)
    counts = _count_parses(monkeypatch)

    uploadable = list_uploadable_songs(root)
    rendered = list_rendered_songs(root)

    assert len(rendered) - len(uploadable) == 2
    assert counts["load_song"] <= 5


def test_a_rewrite_within_one_clock_tick_is_still_noticed(tmp_path):
    """The cache is keyed by inode as well as mtime and size: every save_song is an atomic replace (a new inode)."""
    root = _work(tmp_path)
    path = root / "alpha" / "lyrics_timed.json"
    assert list_easy_chord_backfill_candidates(root) == ["echo"]
    stat = path.stat()

    song = load_song(path)
    song.chord_track = ChordTrack(key="F major")                          # a hard key spelled as long as "C major"
    save_song(song, path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))               # and the old mtime forced back
    assert path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns

    assert list_easy_chord_backfill_candidates(root) == ["alpha", "echo"]
