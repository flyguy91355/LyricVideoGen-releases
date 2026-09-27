"""Issue #7 review (gui-behavior, wave 2): one upload run at a time and never the same song twice (F007/F008/F010/F011/
F017/F018/F049), an unreadable youtube_state.json fails closed (F106), and quitting / relaunching asks while an upload
runs. No real network: every YouTube/Claude call is a fake, and every path is under tmp_path."""

import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from lyricvideo import gui
from lyricvideo.gui import LyricVideoGUI, _maybe_upload_to_youtube, _retry_pending_uploads
from lyricvideo.settings import Settings
from lyricvideo.youtube_schedule import AlreadyUploaded
from lyricvideo.youtube_state import STATE_FILENAME, YoutubeState, save_youtube_state


@pytest.fixture(autouse=True)
def _connected_without_limits(monkeypatch):
    monkeypatch.setattr(gui, "load_quota_blocked_until", lambda: None)
    monkeypatch.setattr(gui, "key_state", lambda work_dir: "confirmed")
    monkeypatch.setattr(gui.youtube_auth, "load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr(gui, "build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr(gui.anthropic, "Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr(gui, "organize_video", lambda *a, **k: None)
    count = [0]
    monkeypatch.setattr(gui, "load_uploads_today", lambda: count[0])
    monkeypatch.setattr(gui, "record_upload", lambda: count.__setitem__(0, count[0] + 1))
    yield count
    # A test that fails half-way must never leave the next one facing a held lock (a real "already running" dialog).
    for lock in (gui._UPLOAD_RUN_LOCK, gui._UPLOAD_SLOT_LOCK, gui._TICK_LOCK, gui._COMMENT_CHECK_LOCK):
        assert not lock.locked(), "a lock was left held"


def _uploading_fake(calls: list, gate: threading.Event | None = None, entered: threading.Event | None = None):
    """A schedule_upload stand-in that behaves like the real one where it matters: it takes a while, then records the
    upload in youtube_state.json (and honours only_if_not_uploaded the way schedule_upload does)."""
    def fake(youtube_client, anthropic_client, work_dir, settings, now=None, *, only_if_not_uploaded=False):
        if only_if_not_uploaded and (work_dir / STATE_FILENAME).exists():
            raise AlreadyUploaded(work_dir.name)
        calls.append(work_dir.name)
        if entered is not None:
            entered.set()
        if gate is not None:
            assert gate.wait(10)
        work_dir.mkdir(parents=True, exist_ok=True)
        save_youtube_state(work_dir, YoutubeState(video_id=f"id-{work_dir.name}", uploaded_at="2026-09-27", title="t"))
        return f"id-{work_dir.name}"
    return fake


def test_two_overlapping_pending_runs_upload_each_song_exactly_once(tmp_path, monkeypatch):
    """F008/F017: a manual Upload Selected and the 20-minute auto-retry both walked the pending list while youtube_state.json
    was only written after each multi-minute upload -- every remaining song went up twice."""
    calls, gate, entered = [], threading.Event(), threading.Event()
    monkeypatch.setattr(gui, "schedule_upload", _uploading_fake(calls, gate, entered))
    settings = Settings(youtube_max_uploads_per_day=20)
    results = {}

    first = threading.Thread(target=lambda: results.setdefault(1, _retry_pending_uploads(
        tmp_path, settings, ["song-a", "song-b"], force=True)))
    first.start()
    assert entered.wait(5)                                     # the first run is mid-upload on song-a
    second = threading.Thread(target=lambda: results.setdefault(2, _retry_pending_uploads(
        tmp_path, settings, ["song-a", "song-b"])))
    second.start()
    time.sleep(0.2)
    gate.set()
    first.join(10)
    second.join(10)

    assert sorted(calls) == ["song-a", "song-b"]               # each song uploaded once, whatever the interleaving
    uploaded = results[1]["succeeded"] + results[2]["succeeded"]
    skipped = results[1].get("already_uploaded", []) + results[2].get("already_uploaded", [])
    assert sorted(uploaded) == ["song-a", "song-b"] and sorted(skipped) == ["song-a", "song-b"]
    assert results[1]["failed"] == results[2]["failed"] == []


def test_a_pending_run_never_resends_a_song_with_an_unreadable_upload_record(tmp_path, monkeypatch):
    """F106: an empty or cut-off youtube_state.json still means the song WAS uploaded."""
    (tmp_path / "song-a").mkdir()
    (tmp_path / "song-a" / STATE_FILENAME).write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(gui, "schedule_upload", _uploading_fake(calls))

    results = _retry_pending_uploads(tmp_path, Settings(), ["song-a"])

    assert calls == []
    assert results["already_uploaded"] == ["song-a"] and results["succeeded"] == [] and results["failed"] == []


def test_already_uploaded_from_schedule_upload_is_a_skip_not_a_failure_and_is_not_counted(tmp_path, monkeypatch, _connected_without_limits):
    def racing_upload(*a, only_if_not_uploaded=False, **k):
        raise AlreadyUploaded("song-a was already uploaded")      # another run finished it while this one waited its turn

    monkeypatch.setattr(gui, "schedule_upload", racing_upload)

    results = _retry_pending_uploads(tmp_path, Settings(), ["song-a"])

    assert results == {"succeeded": [], "failed": [], "deferred": [], "already_uploaded": ["song-a"]}
    assert _connected_without_limits[0] == 0                   # nothing counted against today's cap


def test_the_owners_confirmed_duplicate_upload_is_still_sent(tmp_path, monkeypatch):
    """The single-song Upload button's deliberate re-upload (after "create a duplicate video?") is kept."""
    save_youtube_state(tmp_path / "song-a", YoutubeState(video_id="old", uploaded_at="2026-09-01", title="t"))
    seen = []

    def upload(youtube_client, anthropic_client, work_dir, settings, **kw):
        seen.append((work_dir.name, kw))

    monkeypatch.setattr(gui, "schedule_upload", upload)

    results = _retry_pending_uploads(tmp_path, Settings(), ["song-a"], force=True, only_if_not_uploaded=False)

    assert seen == [("song-a", {"only_if_not_uploaded": False})] and results["succeeded"] == ["song-a"]


def test_two_uploads_can_never_both_take_the_last_slot_of_todays_cap(tmp_path, monkeypatch, _connected_without_limits):
    """F018: the cap check and record_upload were a read-then-write: two runs could both see "1 left" and both upload."""
    calls = []
    monkeypatch.setattr(gui, "schedule_upload", _uploading_fake(calls))
    real = gui.schedule_upload

    def slow_upload(*a, **k):
        time.sleep(0.15)
        return real(*a, **k)

    monkeypatch.setattr(gui, "schedule_upload", slow_upload)
    _connected_without_limits[0] = 6
    settings = Settings(youtube_max_uploads_per_day=7)
    results = []
    threads = [threading.Thread(target=lambda s=slug: results.append(_retry_pending_uploads(tmp_path, settings, [s])))
               for slug in ("song-a", "song-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(calls) == 1 and _connected_without_limits[0] == 7
    assert sorted(len(r["deferred"]) for r in results) == [0, 1]


def test_auto_upload_does_not_upload_a_song_whose_upload_record_is_unreadable(tmp_path, monkeypatch):
    """F106: _maybe_upload_to_youtube treated an unreadable youtube_state.json as "never uploaded" and put a second copy
    on the channel after a Redo or Batch."""
    (tmp_path / STATE_FILENAME).write_text("{\"video_id\": \"abc\", \"upl", encoding="utf-8")    # cut off mid-write
    monkeypatch.setattr(gui, "schedule_upload", lambda *a, **k: pytest.fail("must not upload"))
    monkeypatch.setattr(gui, "build", lambda *a, **k: pytest.fail("no client needed to decide this"))

    _maybe_upload_to_youtube(tmp_path, Settings(youtube_auto_upload=True))


def test_auto_upload_of_a_never_uploaded_song_asks_schedule_upload_to_refuse_a_duplicate(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(gui, "schedule_upload", lambda y, a, work_dir, s, **kw: seen.append(kw))

    _maybe_upload_to_youtube(tmp_path, Settings(youtube_auto_upload=True))

    assert seen == [{"only_if_not_uploaded": True}]


def test_auto_upload_skips_quietly_when_another_run_uploaded_it_first(tmp_path, monkeypatch, _connected_without_limits):
    monkeypatch.setattr(gui, "schedule_upload", lambda *a, **k: (_ for _ in ()).throw(AlreadyUploaded("x")))
    monkeypatch.setattr(gui, "organize_video", lambda *a, **k: pytest.fail("the other run organizes it"))

    _maybe_upload_to_youtube(tmp_path, Settings(youtube_auto_upload=True))

    assert _connected_without_limits[0] == 0


# --- the GUI side: one run at a time --------------------------------------------------------------------------------

class _RecordingThread:
    """threading.Thread stand-in that records its target without running it (the run stays "in progress")."""
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target

    def start(self):
        _RecordingThread.started.append(self.target)


def _stub(**attrs):
    states = []
    button = SimpleNamespace(configure=lambda **kw: states.append(kw))
    attrs.setdefault("settings", Settings(youtube_auto_upload=True))
    attrs.setdefault("_running", False)
    attrs.setdefault("_confirm_quota_override_if_blocked", lambda: True)
    attrs.setdefault("retry_upload_button", button)
    attrs.setdefault("upload_selected_button", button)
    attrs.setdefault("root", SimpleNamespace(after=lambda delay, cb, *a: None))
    stub = SimpleNamespace(_button_states=states, **attrs)
    stub._start_retry_upload = lambda slugs, **kw: LyricVideoGUI._start_retry_upload(stub, slugs, **kw)
    return stub


def test_upload_anyway_clicked_twice_starts_one_upload_run(monkeypatch):
    """F010/F011: Upload Anyway gave no feedback for minutes and every click started another upload of the same song."""
    _RecordingThread.started = []
    monkeypatch.setattr(gui.threading, "Thread", _RecordingThread)
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message: shown.append(title))
    stub = _stub()
    try:
        LyricVideoGUI._on_upload_anyway_flagged(stub, "song-a")
        LyricVideoGUI._on_upload_anyway_flagged(stub, "song-a")

        assert len(_RecordingThread.started) == 1
        assert shown == ["Upload already running"]
        assert {"state": "disabled"} in stub._button_states
    finally:
        gui._UPLOAD_RUN_LOCK.release()                          # the worker that would release it never ran


def test_the_20_minute_retry_leaves_the_pending_list_alone_while_a_manual_run_is_uploading(monkeypatch):
    monkeypatch.setattr(gui, "list_pending_uploads", lambda root: pytest.fail("must not even list pending songs"))
    monkeypatch.setattr(gui, "_retry_pending_uploads", lambda *a, **k: pytest.fail("must not upload"))
    assert gui._UPLOAD_RUN_LOCK.acquire(blocking=False)
    try:
        LyricVideoGUI._retry_pending_uploads_if_due(_stub())
    finally:
        gui._UPLOAD_RUN_LOCK.release()


def test_a_manual_run_and_the_tick_together_upload_each_song_once(tmp_path, monkeypatch):
    """The F008 scenario end to end: Upload Selected is mid-upload on song-a when the 20-minute tick fires."""
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    work = tmp_path / "work"
    calls, gate, entered = [], threading.Event(), threading.Event()
    monkeypatch.setattr(gui, "schedule_upload", _uploading_fake(calls, gate, entered))
    monkeypatch.setattr(gui, "list_pending_uploads",
                        lambda root: [s for s in ("song-a", "song-b") if not (work / s / STATE_FILENAME).exists()])
    monkeypatch.setattr(gui, "list_flagged_songs", lambda root: [])
    monkeypatch.setattr(gui, "load_dismissed", lambda name: set())
    done = threading.Event()
    stub = _stub(settings=Settings(youtube_auto_upload=True, youtube_max_uploads_per_day=20),
                 _on_retry_upload_done=lambda results: done.set(),
                 _refresh_retry_upload_options=lambda: None)
    stub.root = SimpleNamespace(after=lambda delay, cb, *a: cb(*a))

    LyricVideoGUI._start_retry_upload(stub, ["song-a", "song-b"])
    assert entered.wait(5)
    LyricVideoGUI._retry_pending_uploads_if_due(stub)          # the tick: skipped while the manual run holds the pending list
    gate.set()
    assert done.wait(10)

    assert calls == ["song-a", "song-b"]
    assert not gui._UPLOAD_RUN_LOCK.locked()


def test_refreshing_the_lists_during_a_run_does_not_turn_the_upload_buttons_back_on():
    """F007: every Batch song / tick end re-enabled Upload and Upload Selected while a manual run was still going."""
    stub = _stub(**{name: (lambda: None) for name in (
        "_invalidate_upload_list", "_invalidate_pending_list", "_invalidate_flagged_list")})
    assert gui._UPLOAD_RUN_LOCK.acquire(blocking=False)
    try:
        LyricVideoGUI._refresh_retry_upload_options(stub)
        assert stub._button_states == []
    finally:
        gui._UPLOAD_RUN_LOCK.release()
    LyricVideoGUI._refresh_retry_upload_options(stub)
    assert stub._button_states == [{"state": "normal"}, {"state": "normal"}]


def test_a_tick_that_finds_the_previous_one_still_running_is_skipped():
    """F049: a tick still uploading a backlog after 20 minutes had the next one start the same songs again."""
    stub = _stub(_youtube_status_text=lambda: pytest.fail("the whole tick is skipped"))
    assert gui._TICK_LOCK.acquire(blocking=False)
    try:
        LyricVideoGUI._youtube_periodic_tick(stub)
    finally:
        gui._TICK_LOCK.release()


def test_a_song_the_auto_retry_failed_waits_before_the_next_automatic_try(monkeypatch):
    monkeypatch.setattr(gui, "list_pending_uploads", lambda root: ["song-a", "song-b"])
    monkeypatch.setattr(gui, "list_flagged_songs", lambda root: [])
    monkeypatch.setattr(gui, "load_dismissed", lambda name: set())
    tried = []

    def retry(work_root, settings, slugs, **kw):
        tried.append(list(slugs))
        return {"succeeded": [], "failed": [("song-a", "MetadataGenError: no description")], "deferred": []}

    monkeypatch.setattr(gui, "_retry_pending_uploads", retry)
    stub = _stub(_refresh_retry_upload_options=lambda: None)

    LyricVideoGUI._retry_pending_uploads_if_due(stub)
    LyricVideoGUI._retry_pending_uploads_if_due(stub)
    stub._auto_retry_not_before["song-a"] = datetime.now().astimezone() - timedelta(minutes=1)   # the wait is over
    LyricVideoGUI._retry_pending_uploads_if_due(stub)

    assert tried == [["song-a", "song-b"], ["song-b"], ["song-a", "song-b"]]


# --- quitting and relaunching while something runs -------------------------------------------------------------------

def test_closing_while_an_upload_runs_asks_first(monkeypatch):
    monkeypatch.setattr(gui, "upload_in_progress", lambda: True)
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, message, **kw: asked.append(message) or False)
    shut = []
    stub = _stub(_update_apply_in_progress=False, _shut_down=lambda: shut.append(True))
    stub._busy_reasons = lambda: LyricVideoGUI._busy_reasons(stub)
    stub._confirm_quit_if_busy = lambda parent=None: LyricVideoGUI._confirm_quit_if_busy(stub, parent)

    LyricVideoGUI._on_close_window(stub)

    assert shut == [] and "YouTube upload" in asked[0]


def test_closing_with_nothing_running_closes_at_once(monkeypatch):
    monkeypatch.setattr(gui, "upload_in_progress", lambda: False)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: pytest.fail("nothing to ask"))
    shut = []
    stub = _stub(_update_apply_in_progress=False, _shut_down=lambda: shut.append(True))
    stub._busy_reasons = lambda: LyricVideoGUI._busy_reasons(stub)
    stub._confirm_quit_if_busy = lambda parent=None: LyricVideoGUI._confirm_quit_if_busy(stub, parent)

    LyricVideoGUI._on_close_window(stub)

    assert shut == [True]
