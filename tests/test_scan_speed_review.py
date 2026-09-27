"""Review follow-ups to the issue #7 scan-speed fixes (song text is invented)."""

import json

from lyricvideo import owner_verified
from lyricvideo.models import ChordTrack, LyricLine, Song, Word, save_song
from lyricvideo.owner_verified import mark_verified, upload_label, verification
from lyricvideo.redo_log import note_redo_started, redone_songs


def _song(key="C major", shift=0.0):
    words = "the paper lantern hums a tune".split()
    line = LyricLine(words=[Word(w, 1.0 + i + shift, 1.5 + i + shift) for i, w in enumerate(words)])
    return Song(title="T", audio_path="a.mp3", lines=[line], chord_track=ChordTrack(key=key))


def test_the_upload_row_label_parses_a_verified_song_once_per_version(tmp_path, monkeypatch):
    from lyricvideo import cleared_log
    monkeypatch.setattr(cleared_log, "LOG_FILE", tmp_path / "cleared.json")
    song_dir = tmp_path / "song"
    song_dir.mkdir()
    save_song(_song(), song_dir / "lyrics_timed.json")
    mark_verified(song_dir, automatic_share=0.8, needed=0.9)
    loads = []
    real_load = owner_verified.load_song
    monkeypatch.setattr(owner_verified, "load_song", lambda path: loads.append(path) or real_load(path))

    for _ in range(5):
        assert "verified by you" in upload_label(song_dir)
    assert len(loads) <= 1

    save_song(_song(key="G major"), song_dir / "lyrics_timed.json")        # a key fix: same timing, still verified
    assert verification(song_dir) is not None
    save_song(_song(shift=0.7), song_dir / "lyrics_timed.json")             # new timing (a Redo): lapses at once
    assert verification(song_dir) is None
    assert upload_label(song_dir) == "song"


def test_a_damaged_redo_log_is_set_aside_before_a_fresh_one_starts(tmp_path):
    log = tmp_path / "redone.json"
    log.write_text("{half a record", encoding="utf-8")
    song_dir = tmp_path / "s"
    song_dir.mkdir()

    note_redo_started(song_dir, tmp_path / "backup", path=log)

    assert len(redone_songs(path=log)) == 1
    aside = list(tmp_path.glob("redone.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == "{half a record"
    json.loads(log.read_text(encoding="utf-8"))
