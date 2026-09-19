"""redo_log: a permanent record of every song that was redone, so the ones already on YouTube can be replaced there
(and the ones that are not simply keep the new local file). Written automatically by every redo path."""

import json
from datetime import datetime

from lyricvideo.redo_log import (
    mark_replaced_on_youtube, note_redo_finished, note_redo_started, redone_songs, youtube_replacements_pending,
)
from lyricvideo.youtube_state import YoutubeState, save_youtube_state


def song_dir(tmp_path, slug, uploaded=False):
    d = tmp_path / "work" / slug
    d.mkdir(parents=True)
    if uploaded:
        save_youtube_state(d, YoutubeState(video_id=f"vid-{slug}", uploaded_at="2026-09-01T00:00:00", title=slug))
    return d


def test_starting_a_redo_records_whether_the_song_is_already_on_youtube(tmp_path):
    log = tmp_path / "log.json"
    up, local = song_dir(tmp_path, "uploaded-song", uploaded=True), song_dir(tmp_path, "local-song")

    note_redo_started(up, up / "redo_backup_1", path=log)
    note_redo_started(local, local / "redo_backup_1", path=log)

    records = {r["slug"]: r for r in redone_songs(path=log)}
    assert records["uploaded-song"]["on_youtube"] is True and records["uploaded-song"]["video_id"] == "vid-uploaded-song"
    assert records["local-song"]["on_youtube"] is False and records["local-song"]["video_id"] is None
    assert records["local-song"]["status"] == "started"


def test_finishing_a_redo_marks_it_done_and_keeps_the_concern(tmp_path):
    log = tmp_path / "log.json"
    d = song_dir(tmp_path, "s", uploaded=True)
    note_redo_started(d, d / "b", path=log)

    note_redo_finished(d, concern="SET ASIDE FOR REVIEW -- timing could not be fixed", path=log)

    record = redone_songs(path=log)[0]
    assert record["status"] == "done" and record["finished_at"]
    assert record["set_aside"] is True and "timing" in record["concern"]


def test_finishing_a_song_that_was_never_redone_records_nothing(tmp_path):
    log = tmp_path / "log.json"

    note_redo_finished(song_dir(tmp_path, "fresh"), path=log)

    assert redone_songs(path=log) == []


def test_only_finished_redos_of_songs_on_youtube_still_need_replacing_there(tmp_path):
    log = tmp_path / "log.json"
    up_done, up_running, local_done = (song_dir(tmp_path, "up-done", True), song_dir(tmp_path, "up-running", True), song_dir(tmp_path, "local-done"))
    for d in (up_done, up_running, local_done):
        note_redo_started(d, d / "b", path=log)
    note_redo_finished(up_done, path=log)
    note_redo_finished(local_done, path=log)

    pending = youtube_replacements_pending(path=log)

    assert [r["slug"] for r in pending] == ["up-done"]      # a redo still running is not ready; a local-only song has nothing to replace


def test_a_song_replaced_on_youtube_drops_off_the_list(tmp_path):
    log = tmp_path / "log.json"
    d = song_dir(tmp_path, "s", uploaded=True)
    note_redo_started(d, d / "b", path=log)
    note_redo_finished(d, path=log)

    mark_replaced_on_youtube("s", path=log)

    assert youtube_replacements_pending(path=log) == []
    assert redone_songs(path=log)[0]["replaced_on_youtube"] is True


def test_redoing_a_song_twice_keeps_both_records_and_only_the_latest_counts(tmp_path):
    log = tmp_path / "log.json"
    d = song_dir(tmp_path, "s", uploaded=True)
    note_redo_started(d, d / "b1", path=log, now=datetime(2026, 9, 19, 10, 0))
    note_redo_finished(d, path=log, now=datetime(2026, 9, 19, 10, 30))
    mark_replaced_on_youtube("s", path=log)
    note_redo_started(d, d / "b2", path=log, now=datetime(2026, 9, 19, 12, 0))
    note_redo_finished(d, path=log, now=datetime(2026, 9, 19, 12, 30))

    assert len(redone_songs(path=log)) == 2
    assert [r["slug"] for r in youtube_replacements_pending(path=log)] == ["s"]   # the newer redo is not replaced yet


def test_a_missing_or_corrupt_log_reads_as_empty_and_never_raises(tmp_path):
    log = tmp_path / "log.json"

    assert redone_songs(path=log) == []
    log.write_text("{not json", encoding="utf-8")
    assert redone_songs(path=log) == []
    note_redo_started(song_dir(tmp_path, "s"), tmp_path / "b", path=log)      # starts a fresh, valid log
    assert len(redone_songs(path=log)) == 1
    json.loads(log.read_text(encoding="utf-8"))
