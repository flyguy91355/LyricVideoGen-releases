"""Issue #7 review (gui-behavior, wave 2): the YouTube comment check and the Approve buttons -- EASY CHORD videos are
checked (F043-F045), a check costs ~1 quota unit per 50 videos instead of 2 per video (F016/F043), each comment is kept
once it is drafted (F046/F119), two checks never overlap (F118), the channel's own comment is never answered (F116), and
Approve never posts the same public text twice and says when YouTube is not connected (F038/F039/F115). No network:
every client is a fake; every file lives in the per-test home or tmp_path."""

import threading
from types import SimpleNamespace

import pytest

from lyricvideo import gui
from lyricvideo.gui import LyricVideoGUI
from lyricvideo.settings import Settings
from lyricvideo.youtube_comment_state import (
    MAX_DRAFT_FAILURES, PendingComment, PendingReply, load_pending_replies, load_seen_comment_ids,
)
from lyricvideo.youtube_state import YoutubeState, load_youtube_state, save_youtube_state


@pytest.fixture(autouse=True)
def _connected(monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(gui, "load_quota_blocked_until", lambda: None)
    monkeypatch.setattr(gui.youtube_auth, "load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr(gui, "build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr(gui.anthropic, "Anthropic", lambda: "fake-anthropic-client")
    yield
    for lock in (gui._COMMENT_CHECK_LOCK, gui._TICK_LOCK, gui._UPLOAD_RUN_LOCK):
        assert not lock.locked(), "a lock was left held"


def _uploaded(tmp_path, slug, video_id):
    folder = tmp_path / "work" / slug
    folder.mkdir(parents=True, exist_ok=True)
    save_youtube_state(folder, YoutubeState(video_id=video_id, uploaded_at="2026-09-01T00:00:00", title=f"Title {slug}"))
    return folder


def _comment(comment_id, video_id, text="nice one", **extra):
    return SimpleNamespace(comment_id=comment_id, video_id=video_id, author="fan", text=text, **extra)


def _stub():
    changed = []
    return SimpleNamespace(settings=Settings(youtube_quota_retry_hours=6), changed=changed,
                           root=SimpleNamespace(after=lambda delay, cb, *a: cb(*a)),
                           _on_pending_replies_changed=lambda: changed.append(True))


def test_an_easy_chord_video_is_checked_for_comments_too(tmp_path, monkeypatch):
    """F043-F045: only top-level work/* folders were walked, so a viewer's "wrong capo" on an EASY CHORD video never
    reached the reply queue."""
    _uploaded(tmp_path, "some-song", "vid-a")
    _uploaded(tmp_path, "some-song/easychords", "vid-b")
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {vid: (True, 1) for vid in ids})
    read = []
    monkeypatch.setattr(gui, "list_new_comments", lambda client, video_id, seen: read.append(video_id) or [])

    LyricVideoGUI._check_youtube_comments_worker(_stub())

    assert sorted(read) == ["vid-a", "vid-b"]


def test_a_queued_reply_remembers_which_video_its_comment_is_on(tmp_path, monkeypatch):
    """owner, 2026-10-03: "i dont know what song or video the comments are comming from" -- a drafted reply now
    carries the uploaded video's own title, not just its opaque video_id, so the GUI can show it."""
    _uploaded(tmp_path, "some-song", "vid-a")
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {vid: (True, 1) for vid in ids})
    monkeypatch.setattr(gui, "list_new_comments", lambda client, video_id, seen: [_comment("c1", video_id)])
    monkeypatch.setattr(gui, "draft_comment_reply", lambda client, text, title: ("thanks!", False))

    LyricVideoGUI._check_youtube_comments_worker(_stub())

    [reply] = load_pending_replies()
    assert reply.video_title == "Title some-song"


def test_a_video_whose_comment_count_has_not_changed_costs_no_comment_read(tmp_path, monkeypatch):
    """F016/F043: every check read every public video's comments (plus a status call each) -- hundreds of quota units
    every 20 minutes. Now only a video whose count moved since its last complete check is read."""
    _uploaded(tmp_path, "song-a", "vid-a")
    _uploaded(tmp_path, "song-b", "vid-b")
    counts = {"vid-a": 3, "vid-b": 0}
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {vid: (True, counts[vid]) for vid in ids})
    read = []
    monkeypatch.setattr(gui, "list_new_comments", lambda client, video_id, seen: read.append(video_id) or [])
    monkeypatch.setattr(gui, "draft_comment_reply", lambda client, text, title: ("thanks!", False))

    LyricVideoGUI._check_youtube_comments_worker(_stub())       # first check: reads the one with comments
    LyricVideoGUI._check_youtube_comments_worker(_stub())       # nothing changed: reads nothing
    counts["vid-a"] = 4
    LyricVideoGUI._check_youtube_comments_worker(_stub())       # a new comment arrived: reads that video only

    assert read == ["vid-a", "vid-a"]
    assert gui._load_comment_counts() == {"vid-a": 4}


def test_once_a_day_every_video_with_comments_is_read_even_if_its_count_did_not_move(tmp_path, monkeypatch):
    """A safety net: YouTube's count can move before the comment itself can be listed; a daily read never misses it."""
    from datetime import datetime, timedelta

    _uploaded(tmp_path, "song-a", "vid-a")
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {"vid-a": (True, 3)})
    read = []
    monkeypatch.setattr(gui, "list_new_comments", lambda client, video_id, seen: read.append(video_id) or [])
    gui._save_comment_counts({"vid-a": 3}, datetime.now().astimezone() - timedelta(hours=1))

    LyricVideoGUI._check_youtube_comments_worker(_stub())
    assert read == []                                           # checked an hour ago, count unchanged

    gui._save_comment_counts({"vid-a": 3}, datetime.now().astimezone() - timedelta(hours=25))
    LyricVideoGUI._check_youtube_comments_worker(_stub())
    LyricVideoGUI._check_youtube_comments_worker(_stub())       # and the next check is cheap again

    assert read == ["vid-a"]


class _CountingClient:
    """A fake YouTube client counting videos.list calls and answering from `videos`."""

    def __init__(self, videos):
        self.videos_by_id, self.requests = videos, []

    def videos(self):
        return self

    def list(self, part, id, maxResults):
        ids = id.split(",")
        self.requests.append(ids)
        items = [dict(self.videos_by_id[v], id=v) for v in ids if v in self.videos_by_id]
        return SimpleNamespace(execute=lambda: {"items": items})


def test_the_public_status_and_comment_counts_are_read_50_videos_per_call():
    videos = {f"v{i}": {"status": {"privacyStatus": "public"}, "statistics": {"commentCount": str(i % 3)}}
              for i in range(120)}
    videos["v1"] = {"status": {"privacyStatus": "private"}, "statistics": {"commentCount": "5"}}
    videos["v2"] = {"status": {"privacyStatus": "public"}, "statistics": {}}      # comments off: no commentCount
    videos["v4"] = {"status": {"privacyStatus": "public"}}                       # no statistics at all: unknown
    client = _CountingClient(videos)

    stats = gui._video_comment_stats(client, [f"v{i}" for i in range(120)] + ["v0", "gone"])

    assert [len(ids) for ids in client.requests] == [50, 50, 21]               # 3 calls (duplicates dropped), not 120
    assert stats["v0"] == (True, 0) and stats["v5"] == (True, 2)
    assert stats["v1"] == (False, 5) and stats["v2"] == (True, 0) and stats["v4"] == (True, None)
    assert "gone" not in stats                                                   # deleted on YouTube


def test_a_failed_draft_keeps_the_comments_already_queued_and_is_retried_next_check(tmp_path, monkeypatch):
    """F046/F119: seen ids were saved only after the whole scan, and one Claude failure aborted it -- so every later check
    drafted (and paid for) the same comments again and queued duplicates."""
    _uploaded(tmp_path, "song-a", "vid-a")
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {"vid-a": (True, 2)})
    comments = [_comment("c1", "vid-a", "first"), _comment("c2", "vid-a", "second")]
    monkeypatch.setattr(gui, "list_new_comments",
                        lambda client, video_id, seen: [c for c in comments if c.comment_id not in seen])
    drafted, fail = [], {"second"}

    def draft(client, text, title):
        drafted.append(text)
        if text in fail:
            raise RuntimeError("overloaded")
        return f"re: {text}", False

    monkeypatch.setattr(gui, "draft_comment_reply", draft)

    LyricVideoGUI._check_youtube_comments_worker(_stub())
    assert [r.comment_id for r in load_pending_replies()] == ["c1"]
    assert load_seen_comment_ids() == {"c1"}
    assert gui._load_comment_counts() == {}                    # incomplete: the video is read again next time

    fail.clear()
    LyricVideoGUI._check_youtube_comments_worker(_stub())

    assert drafted == ["first", "second", "second"]            # c1 was never drafted twice
    assert [r.comment_id for r in load_pending_replies()] == ["c1", "c2"]
    assert load_seen_comment_ids() == {"c1", "c2"}


def test_a_comment_whose_draft_keeps_failing_is_queued_blank_for_the_owner(tmp_path, monkeypatch):
    _uploaded(tmp_path, "song-a", "vid-a")
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {"vid-a": (True, 1)})
    monkeypatch.setattr(gui, "list_new_comments",
                        lambda client, video_id, seen: [] if "c1" in seen else [_comment("c1", "vid-a")])
    tries = []
    monkeypatch.setattr(gui, "draft_comment_reply",
                        lambda *a: tries.append(1) or (_ for _ in ()).throw(RuntimeError("down")))

    for _ in range(MAX_DRAFT_FAILURES + 1):
        LyricVideoGUI._check_youtube_comments_worker(_stub())

    assert len(tries) == MAX_DRAFT_FAILURES                    # no more paid tries after that
    replies = load_pending_replies()
    assert [(r.comment_id, r.draft_reply, r.is_error_report) for r in replies] == [("c1", "", False)]
    assert "c1" in load_seen_comment_ids()


def test_a_second_check_while_one_is_running_does_nothing(monkeypatch):
    """F118: Check Now during the tick's check drafted every new comment twice."""
    monkeypatch.setattr(gui.youtube_auth, "load_credentials", lambda: pytest.fail("the second check must not start"))
    assert gui._COMMENT_CHECK_LOCK.acquire(blocking=False)
    try:
        LyricVideoGUI._check_youtube_comments_worker(_stub())
    finally:
        gui._COMMENT_CHECK_LOCK.release()


def test_check_now_while_a_check_runs_says_so_instead_of_starting_another(monkeypatch):
    monkeypatch.setattr(gui.threading, "Thread", lambda *a, **k: pytest.fail("no second check"))
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message: shown.append(title))
    assert gui._COMMENT_CHECK_LOCK.acquire(blocking=False)
    try:
        LyricVideoGUI._on_check_youtube_comments(SimpleNamespace(_confirm_quota_override_if_blocked=lambda: True))
    finally:
        gui._COMMENT_CHECK_LOCK.release()
    assert shown == ["Checking comments"]


def test_the_channels_own_comments_are_never_drafted_a_reply(tmp_path, monkeypatch):
    """F116: a comment the channel itself wrote (e.g. from YouTube Studio) came back as a "new" viewer comment."""
    _uploaded(tmp_path, "song-a", "vid-a")
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {"vid-a": (True, 2)})
    monkeypatch.setattr(gui, "_own_channel_id", lambda client: "UC-me")
    monkeypatch.setattr(gui, "list_new_comments", lambda client, video_id, seen: [
        _comment("mine", "vid-a", author_channel_id="UC-me"), _comment("theirs", "vid-a", author_channel_id="UC-fan"),
    ])
    drafted = []
    monkeypatch.setattr(gui, "draft_comment_reply", lambda client, text, title: drafted.append(text) or ("hi", False))

    LyricVideoGUI._check_youtube_comments_worker(_stub())

    assert [r.comment_id for r in load_pending_replies()] == ["theirs"] and len(drafted) == 1
    assert {"mine", "theirs"} <= load_seen_comment_ids()


def test_approving_an_easy_chord_videos_engagement_comment_marks_that_video(tmp_path):
    """F044: the posted flag was only looked for in top-level folders, so an EASY video's stayed False."""
    parent = _uploaded(tmp_path, "some-song", "vid-a")
    easy = _uploaded(tmp_path, "some-song/easychords", "vid-b")

    gui._mark_engagement_comment_posted("vid-b")

    assert load_youtube_state(easy).engagement_comment_posted is True
    assert load_youtube_state(parent).engagement_comment_posted is False


# --- Approve: never twice, and never silently ------------------------------------------------------------------------

def _reply(comment_id="c1"):
    return PendingReply(comment_id=comment_id, video_id="vid-a", author="fan", comment_text="wrong chord?",
                        draft_reply="thanks!", is_error_report=True)


def _approve_stub(**attrs):
    attrs.setdefault("settings", Settings())
    attrs.setdefault("_confirm_quota_override_if_blocked", lambda: True)
    attrs.setdefault("root", SimpleNamespace(after=lambda delay, cb, *a: cb(*a)))
    attrs.setdefault("_render_pending_replies", lambda: None)
    attrs.setdefault("_invalidate_pending_comments", lambda: None)
    attrs.setdefault("_refresh_youtube_status", lambda: None)
    return SimpleNamespace(**attrs)


_TEXT_BOX = SimpleNamespace(get=lambda start, end: "Thanks, fixed!\n")


def test_a_second_approve_while_the_reply_is_posting_does_not_post_it_again(monkeypatch):
    """F038/F039: on a slow link nothing changed for a second or two, the owner clicked again, and the viewer got the
    same reply twice."""
    gate, entered, posted = threading.Event(), threading.Event(), []

    def slow_post(client, comment_id, text):
        posted.append(comment_id)
        entered.set()
        assert gate.wait(10)

    monkeypatch.setattr(gui, "post_reply", slow_post)
    monkeypatch.setattr(gui, "remove_pending_reply", lambda comment_id: None)
    busy = []
    view = SimpleNamespace(set_busy=lambda item_id, value: busy.append((item_id, value)))
    stub = _approve_stub(replies_view=view)
    threads = []
    real_thread = threading.Thread
    monkeypatch.setattr(gui.threading, "Thread",
                        lambda *a, **k: threads.append(real_thread(*a, **k)) or threads[-1])

    LyricVideoGUI._on_approve_reply(stub, _reply(), _TEXT_BOX)
    assert entered.wait(5)
    LyricVideoGUI._on_approve_reply(stub, _reply(), _TEXT_BOX)   # the impatient second click
    gate.set()
    for t in threads:
        t.join(10)
    LyricVideoGUI._on_approve_reply(stub, _reply(), _TEXT_BOX)   # and once it is posted, never again

    assert posted == ["c1"] and len(threads) == 1
    assert busy[0] == ("c1", True) and busy[-1] == ("c1", False)


def test_approve_works_again_after_a_post_that_failed(monkeypatch):
    attempts = []

    def post(client, comment_id, text):
        attempts.append(comment_id)
        if len(attempts) == 1:
            raise RuntimeError("backend error")

    monkeypatch.setattr(gui, "post_reply", post)
    monkeypatch.setattr(gui, "remove_pending_reply", lambda comment_id: None)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(gui.threading, "Thread", _Immediate)
    stub = _approve_stub()

    LyricVideoGUI._on_approve_reply(stub, _reply(), _TEXT_BOX)
    LyricVideoGUI._on_approve_reply(stub, _reply(), _TEXT_BOX)

    assert attempts == ["c1", "c1"]


class _Immediate:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


@pytest.mark.parametrize("kind", ["reply", "comment"])
def test_approve_while_not_connected_says_so_and_posts_nothing(monkeypatch, kind):
    """F115: with the 7-day token expired, Approve silently did nothing -- so the owner clicked again and again."""
    monkeypatch.setattr(gui.youtube_auth, "load_credentials", lambda: None)
    monkeypatch.setattr(gui, "post_reply", lambda *a: pytest.fail("nothing may be posted"))
    monkeypatch.setattr(gui, "post_top_level_comment", lambda *a: pytest.fail("nothing may be posted"))
    monkeypatch.setattr(gui.threading, "Thread", _Immediate)
    errors, refreshed = [], []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, message: errors.append(title))
    stub = _approve_stub(_refresh_youtube_status=lambda: refreshed.append(True))

    if kind == "reply":
        LyricVideoGUI._on_approve_reply(stub, _reply(), _TEXT_BOX)
    else:
        LyricVideoGUI._on_approve_comment(stub, PendingComment("vid-a", "Song", "draft"), _TEXT_BOX)

    assert errors == ["Not connected to YouTube"] and refreshed == [True]
    assert stub._posting_keys == set()                         # released: Approve works once reconnected


def test_a_second_approve_of_an_engagement_comment_while_it_posts_does_not_post_it_again(monkeypatch):
    posted = []
    monkeypatch.setattr(gui, "is_video_public", lambda client, video_id: True)
    monkeypatch.setattr(gui, "post_top_level_comment", lambda client, video_id, text: posted.append(video_id) or "t1")
    monkeypatch.setattr(gui, "remove_pending_comment", lambda video_id: None)
    monkeypatch.setattr(gui, "_mark_engagement_comment_posted", lambda video_id: None)
    monkeypatch.setattr(gui, "mark_comment_seen", lambda comment_id: None)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)
    started = []

    class _Held:                                              # the first post is still in flight when the owner clicks again
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(gui.threading, "Thread", _Held)
    stub = _approve_stub()
    comment = PendingComment("vid-a", "Song", "Which part is hardest?")

    LyricVideoGUI._on_approve_comment(stub, comment, _TEXT_BOX)
    LyricVideoGUI._on_approve_comment(stub, comment, _TEXT_BOX)
    started[0]()                                               # the first (and only) post goes through

    assert len(started) == 1 and posted == ["vid-a"]


def test_the_draft_list_turns_approve_off_for_a_draft_being_posted():
    ctk = pytest.importorskip("customtkinter")
    try:
        root = ctk.CTk()
    except Exception as e:
        pytest.skip(f"no display available for a real window ({type(e).__name__}: {e})")
    try:
        root.withdraw()
        view = gui._DraftQueueView(root, on_approve=lambda item, box: None, on_dismiss=lambda item: None)
        view.set_items([("a", "A", "fan", "text", "heading", "draft"), ("b", "B", "fan", "text", "heading", "draft")])
        view.select("a")
        assert view.approve_button.cget("state") == "normal"

        view.set_busy("a", True)
        assert view.approve_button.cget("state") == "disabled" and view.dismiss_button.cget("state") == "disabled"
        view.select("b")
        assert view.approve_button.cget("state") == "normal"
        view.select("a")
        assert view.approve_button.cget("state") == "disabled"          # still posting
        view.set_busy("a", False)
        assert view.approve_button.cget("state") == "normal"
    finally:
        root.destroy()
