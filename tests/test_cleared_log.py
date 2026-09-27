"""cleared_log keeps a permanent record of which songs were cleared for upload (passed the lyric and timing checks) and which
were pulled and why -- owner, 2026-09-20: "keep track of all the videos that have been cleared for upload ... if there bad
remove them ... they can be redone"."""

from datetime import datetime

from lyricvideo.cleared_log import cleared_songs, history, record_cleared, record_removed

WHEN = datetime(2026, 9, 20, 10, 0).astimezone()


def test_a_cleared_song_is_listed_with_when_and_why(tmp_path):
    log = tmp_path / "cleared.json"

    record_cleared("desperado", "passed lyrics 93%, timing 89%", path=log, now=WHEN)

    (entry,) = cleared_songs(log)
    assert entry["slug"] == "desperado" and "timing 89%" in entry["note"] and entry["at"].startswith("2026-09-20T10:00")


def test_a_removed_song_leaves_the_cleared_list_but_stays_in_the_history(tmp_path):
    log = tmp_path / "cleared.json"
    record_cleared("respect", "checked", path=log, now=WHEN)

    record_removed("respect", "only 46% of words precise", path=log, now=WHEN)

    assert cleared_songs(log) == []
    assert [(h["slug"], h["status"]) for h in history(log)] == [("respect", "cleared"), ("respect", "removed")]
    assert history(log)[-1]["note"] == "only 46% of words precise"


def test_a_song_cleared_again_after_a_redo_is_listed_again(tmp_path):
    log = tmp_path / "cleared.json"
    record_removed("respect", "imprecise", path=log, now=WHEN)

    record_cleared("respect", "redone: 88% precise", path=log, now=WHEN)

    assert [e["slug"] for e in cleared_songs(log)] == ["respect"]


def test_each_song_appears_once_and_the_list_is_sorted(tmp_path):
    log = tmp_path / "cleared.json"
    for slug in ("b-song", "a-song", "b-song"):
        record_cleared(slug, "ok", path=log, now=WHEN)

    assert [e["slug"] for e in cleared_songs(log)] == ["a-song", "b-song"]


def test_a_missing_or_damaged_file_is_an_empty_record(tmp_path):
    log = tmp_path / "cleared.json"
    assert cleared_songs(log) == [] and history(log) == []

    log.write_text("not json", encoding="utf-8")
    assert cleared_songs(log) == []


def test_appends_from_several_threads_at_once_are_all_kept(tmp_path):
    """Issue #7 review: the Generate/Batch worker, Mark Verified and the timing check can append at the same moment; an
    unguarded read-append-replace through one shared ".tmp" name lost entries and raised on the second replace."""
    import threading

    log = tmp_path / "cleared.json"
    errors = []

    def append_many(n):
        try:
            for i in range(50):
                record_removed(f"song-{n}-{i}", "why", path=log, now=WHEN)
        except Exception as e:                                  # pragma: no cover -- reported below
            errors.append(e)

    threads = [threading.Thread(target=append_many, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [] and len(history(log)) == 200
    assert list(tmp_path.glob("*.tmp")) == []                   # no temp file left behind


def test_a_damaged_record_is_set_aside_and_never_silently_replaced(tmp_path):
    log = tmp_path / "cleared.json"
    log.write_text("not json", encoding="utf-8")

    record_cleared("new-song", "ok", path=log, now=WHEN)

    assert [e["slug"] for e in history(log)] == ["new-song"]
    (kept,) = tmp_path.glob("cleared.json.corrupt-*")
    assert kept.read_text(encoding="utf-8") == "not json"
