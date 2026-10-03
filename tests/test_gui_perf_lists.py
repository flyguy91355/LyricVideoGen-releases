"""Issue #7 ("the program's GUI runs very very slow, long time to start and to x out, the pages open very slowly"): the
song lists, the review panel, the comment drafts, the Settings popup and the close button, driven on a REAL (withdrawn)
LyricVideoGUI over a throwaway work/ root. Nothing here touches the network or the real home directory."""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import lyricvideo.gui as gui
from lyricvideo.gui import LyricVideoGUI
from lyricvideo.key_decision import KeyDecision, save_decision
from lyricvideo.models import Song, save_song
from lyricvideo.pipeline import slugify
from lyricvideo.settings import Settings
from lyricvideo.timing_gate import use_pass_share_from
from lyricvideo.youtube_comment_state import PendingReply

ctk = pytest.importorskip("customtkinter")


# --- a throwaway library -------------------------------------------------------------------------------------------------

def _make_song(work: Path, slug: str, *, video=True, key="confirmed", concern="", uploaded=False) -> Path:
    song_dir = work / slug
    song_dir.mkdir(parents=True, exist_ok=True)
    title = slug.replace("-", " ").title()
    save_song(Song(title=title, audio_path=str(song_dir / "a.mp3"), lyrics_accuracy_concern=concern),
              song_dir / "lyrics_timed.json")
    if video:
        (song_dir / f"{slugify(title)}.mp4").write_bytes(b"video")
    if key == "confirmed":
        save_decision(song_dir, KeyDecision(status="confirmed", key="C major", source="agreed", chord_key="C major"))
    elif key == "review":
        save_decision(song_dir, KeyDecision(status="review", chord_key="D major", published_key="G major"))
    if uploaded:
        (song_dir / "youtube_state.json").write_text(
            '{"video_id": "v1", "uploaded_at": "2026-09-01T00:00:00+00:00", "title": "t"}', encoding="utf-8")
    return song_dir


def _make_easy_variant(parent_dir: Path) -> Path:
    easy = parent_dir / "easychords"
    easy.mkdir()
    title = "Hard Song EasyChords"
    save_song(Song(title=title, audio_path="a.mp3"), easy / "lyrics_timed.json")
    (easy / f"{slugify(title)}.mp4").write_bytes(b"video")
    (easy / "easy_chord_capo.json").write_text(
        '{"capo_fret": 3, "shape_key": "G major", "original_key": "Bb major", "original_title": "Hard Song"}',
        encoding="utf-8")
    return easy


# --- a real GUI, isolated ------------------------------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Everything the GUI would read or write outside tmp_path, pointed at fakes."""
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(Settings, "load", staticmethod(lambda path=None: Settings()))
    monkeypatch.setattr(gui, "load_last_batch_folder", lambda: "")
    monkeypatch.setattr(gui, "load_dismissed", lambda list_name: set())
    monkeypatch.setattr(gui, "dismiss_song", lambda list_name, slug: None)
    monkeypatch.setattr(gui, "load_pending_replies", lambda: [])
    monkeypatch.setattr(gui, "load_pending_comments", lambda: [])
    monkeypatch.setattr(gui, "check_for_update", lambda *a, **k: None)
    monkeypatch.setattr(gui.youtube_auth, "load_credentials", lambda *a, **k: None)
    monkeypatch.setattr(gui, "load_quota_blocked_until", lambda: None)
    monkeypatch.setattr("lyricvideo.cleared_log.LOG_FILE", tmp_path / "cleared.json")
    monkeypatch.setattr(LyricVideoGUI, "_check_api_keys", lambda self: None)
    monkeypatch.setattr(LyricVideoGUI, "_refresh_youtube_status", lambda self: None)   # its thread needs a mainloop
    monkeypatch.setattr(LyricVideoGUI, "_start_update_check", lambda self: None)
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    return tmp_path / "work"


@pytest.fixture
def app(isolated):
    try:
        root = _new_ctk_root(ctk)
    except Exception as e:
        pytest.skip(f"no display available for a real window ({type(e).__name__}: {e})")
    root.withdraw()
    application = LyricVideoGUI(root)
    try:
        yield application
    finally:
        application._closing = True
        worker = application._list_worker_thread
        if worker is not None:
            worker.join(30)
        try:
            root.destroy()
        except Exception:
            pass
        use_pass_share_from(None)
        # Collect this window's Tk objects HERE, on the main thread: left for later, Python may collect them on the next
        # test's list-scan thread, and a Tcl call from there while a new window is being created can break Tk's startup.
        del application, root
        gc.collect()


def _pump(app, timeout=30.0):
    """Runs the Tk loop until every requested list scan has been shown."""
    deadline = time.time() + timeout
    app.root.update()
    while app._list_loads_busy():
        assert time.time() < deadline, "a list scan never finished"
        time.sleep(0.005)
        app.root.update()
    app.root.update()


def _toggle(app, title):
    stack = [app.root]
    while stack:
        widget = stack.pop()
        if isinstance(widget, ctk.CTkButton):
            try:
                text = widget.cget("text")
            except Exception:
                text = ""
            if text in (f"▶ {title}", f"▼ {title}"):
                return widget
        stack.extend(widget.winfo_children())
    raise LookupError(title)


def _open(app, title):
    _toggle(app, title).invoke()
    _pump(app)


def _count_widgets(widget) -> int:
    return 1 + sum(_count_widgets(child) for child in widget.winfo_children())


# --- song lists: a constant number of widgets, filled from a background scan --------------------------------------------

def test_opening_a_long_song_list_builds_no_widgets_per_song(app, isolated):
    """F009/F012/F041: the Redo list used to build ~11 CustomTkinter widgets per song (5,000+ for the owner's ~466 songs:
    tens of seconds to open, and on Windows enough native windows to crash the app). A Treeview draws every row itself."""
    for i in range(300):
        _make_song(isolated, f"song-{i:03d}", video=False, key=None)
    before = _count_widgets(app.root)

    _open(app, "Redo an Existing Song")

    view = app._song_views["redo"]
    assert len(view.tree.get_children()) == 300 and view.slugs()[0] == "song-000"
    assert _count_widgets(app.root) == before          # opening added no widgets at all, whatever the song count


def test_the_scans_behind_every_list_run_off_the_tk_thread(app, isolated, monkeypatch):
    """F040/F050: the list_* walks read every song's files; on the Tk thread they froze the window for seconds per list,
    and again after every Batch song while a list was open."""
    _make_song(isolated, "pending-song")
    _make_song(isolated, "flagged-song", concern="Lyrics could not be confirmed against the vocal.")
    threads = []
    for name in ("list_redoable_songs", "list_uploadable_songs", "list_rendered_songs", "list_pending_uploads",
                 "list_easy_chord_backfill_candidates", "list_flagged_songs"):
        real = getattr(gui, name)

        def spy(*args, _real=real, **kwargs):
            threads.append(threading.current_thread() is threading.main_thread())
            return _real(*args, **kwargs)

        monkeypatch.setattr(gui, name, spy)

    for title in ("Redo an Existing Song", "Upload to YouTube", "Pending YouTube Uploads",
                  "Generate EASY CHORD Versions (existing songs)", "Flagged for Lyrics Review"):
        _open(app, title)
    calls_after_opening = len(threads)
    gui._dispatch_queue_message(app, "batch_item_done", None)   # what the Batch sends after every song
    _pump(app)

    assert calls_after_opening >= 5 and len(threads) > calls_after_opening
    assert not any(threads), "a song-list scan ran on the Tk thread"
    assert app._song_views["pending"].slugs() == ["pending-song"]
    assert app._song_views["flagged"].slugs() == ["flagged-song"]


def test_a_burst_of_refreshes_of_an_open_list_costs_one_extra_scan_and_shows_the_newest(app, isolated, monkeypatch):
    _make_song(isolated, "first-song")
    _open(app, "Pending YouTube Uploads")
    gate, entered, calls = threading.Event(), threading.Event(), []
    real = gui.list_pending_uploads

    def slow_scan(work_root):
        calls.append(1)
        entered.set()
        gate.wait(10)
        return real(work_root)

    monkeypatch.setattr(gui, "list_pending_uploads", slow_scan)
    app._invalidate_pending_list()                 # starts a scan, which is now stuck on the gate
    assert entered.wait(10)
    _make_song(isolated, "second-song")
    for _ in range(5):                             # e.g. several Batch songs finishing while it runs
        app._invalidate_pending_list()
    t0 = time.perf_counter()
    app.root.update()                              # the window keeps responding meanwhile
    assert time.perf_counter() - t0 < 1.0
    gate.set()
    _pump(app)

    assert len(calls) == 2                         # the stuck scan + ONE for the whole burst
    assert app._song_views["pending"].slugs() == ["first-song", "second-song"]


def test_the_redo_list_follows_its_variable_with_one_trace_not_one_per_song(app, isolated):
    """F114: every CTkRadioButton added its own trace on the shared StringVar, so one click redrew all ~470 rows."""
    for slug in ("a-song", "b-song", "c-song"):
        _make_song(isolated, slug, video=False, key=None)
    _open(app, "Redo an Existing Song")
    view = app._song_views["redo"]

    assert len(app.redo_song_var.trace_info()) == 1
    view.select("b-song")
    app.root.update()
    assert app.redo_song_var.get() == "b-song"     # highlighting a row picks it for Redo
    app.redo_song_var.set("c-song")                # (Redo from the Flagged panel sets it this way)
    assert view.selected() == "c-song"


def test_remove_drops_just_that_row_without_rescanning(app, isolated, monkeypatch):
    for slug in ("a-song", "b-song", "c-song"):
        _make_song(isolated, slug, video=False, key=None)
    _open(app, "Redo an Existing Song")
    monkeypatch.setattr(gui, "list_redoable_songs", lambda work_root: pytest.fail("Remove must not rescan"))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    removed = []
    monkeypatch.setattr(gui, "dismiss_song", lambda list_name, slug: removed.append((list_name, slug)))
    view = app._song_views["redo"]
    view.select("b-song")

    app._with_highlighted("redo", lambda slug: app._on_remove_song("redo", slug))
    _pump(app)

    assert removed == [("redo", "b-song")] and view.slugs() == ["a-song", "c-song"]
    assert app.redo_song_var.get() == ""           # the removed song is no longer picked for Redo


def test_the_pending_checklist_starts_all_ticked_and_keeps_the_owners_unticks_across_a_refresh(app, isolated, monkeypatch):
    for slug in ("a-song", "b-song", "c-song"):
        _make_song(isolated, slug)
    _open(app, "Pending YouTube Uploads")
    view = app._song_views["pending"]
    assert view.checked() == ["a-song", "b-song", "c-song"]             # everything starts ticked, as before

    app._pending_upload_vars["b-song"].set(False)                        # the owner unticks one
    _make_song(isolated, "d-song")
    app._refresh_retry_upload_options()                                  # e.g. a Batch song just finished
    _pump(app)
    assert view.checked() == ["a-song", "c-song", "d-song"]             # kept; the new song arrives ticked

    started = []
    app._start_retry_upload = lambda slugs: started.append(slugs)
    app._on_upload_selected_pending()
    assert started == [["a-song", "c-song", "d-song"]]

    app.pending_select_all_var.set(False)
    app._on_toggle_pending_select_all()
    assert view.checked() == []


# --- Flagged for Lyrics Review: one list + one detail pane; EASY CHORD versions are never run through the pipeline -------

def test_an_easy_chord_version_in_review_offers_rebuild_instead_of_redo_render_or_set_key(app, isolated):
    """F014/F015/F042: Redo / Render Anyway / Set Key on "<song>/easychords" ran the pipeline (or saved a key) on the capo
    folder itself -- no CAPO badge, shape chords labelled with the original key, even a nested second capo version."""
    parent = _make_song(isolated, "hard-song", key="review")
    _make_easy_variant(parent)
    _open(app, "Flagged for Lyrics Review")
    view = app._song_views["flagged"]
    assert view.slugs() == ["hard-song", "hard-song/easychords"]

    view.select("hard-song/easychords")
    app.root.update()
    states = {key: b.cget("state") for key, b in app._flagged_buttons.items()}
    assert {k for k, s in states.items() if s == "normal"} == {"watch", "remove", "rebuild_easy", "mark_verified",
                                                               "upload_anyway"}
    view.select("hard-song")
    app.root.update()
    states = {key: b.cget("state") for key, b in app._flagged_buttons.items()}
    assert states["set_key"] == states["redo"] == "normal" and states["rebuild_easy"] == "disabled"


def test_every_pipeline_action_refuses_an_easy_chord_version(isolated, monkeypatch):
    easy = _make_easy_variant(_make_song(isolated, "hard-song", key="review"))
    told = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message, **kw: told.append(title))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: pytest.fail("must refuse before asking anything"))
    monkeypatch.setattr(gui.simpledialog, "askstring", lambda *a, **k: pytest.fail("must refuse before asking for a key"))
    monkeypatch.setattr(gui.threading, "Thread", lambda *a, **k: pytest.fail("must never start the pipeline"))
    stub = SimpleNamespace(_running=False, redo_song_var=SimpleNamespace(get=lambda: "hard-song/easychords"))

    LyricVideoGUI._on_set_key_flagged(stub, "hard-song/easychords")
    LyricVideoGUI._on_render_anyway_flagged(stub, "hard-song/easychords")
    LyricVideoGUI._on_redo(stub)

    assert told == ["EASY CHORD version"] * 3
    assert not (easy / "key_owner.json").exists()


def test_rebuild_easy_version_remakes_it_from_its_song(isolated, monkeypatch):
    built = []
    monkeypatch.setattr(gui, "build_capo_variant", lambda work_dir, **kw: built.append(work_dir) or work_dir / "easychords" / "x.mp4")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)

    class _Now:
        def __init__(self, target, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(gui.threading, "Thread", _Now)
    button = SimpleNamespace(configure=lambda **kw: None)
    import queue
    stub = SimpleNamespace(
        _running=False, generate_button=button, redo_button=button, batch_button=button,
        generate_easy_chord_backfill_button=button, status_var=SimpleNamespace(set=lambda v: None),
        progress_bar=SimpleNamespace(set=lambda v: None), _clear_log=lambda: None, _queue=queue.Queue(),
        root=SimpleNamespace(after=lambda *a: None), _poll_queue=lambda: None, settings=Settings(),
    )
    stub._run_easy_chord_backfill_worker = lambda slugs: LyricVideoGUI._run_easy_chord_backfill_worker(stub, slugs)

    LyricVideoGUI._on_rebuild_easy_flagged(stub, "hard-song/easychords")

    assert built == [isolated / "hard-song"] and stub._running is True


def test_a_backfill_that_builds_nothing_is_not_reported_as_built(isolated, monkeypatch):
    import queue
    monkeypatch.setattr(gui, "build_capo_variant", lambda work_dir, **kw: None)   # e.g. its key is not settled yet
    stub = SimpleNamespace(_queue=queue.Queue(), settings=Settings())

    LyricVideoGUI._run_easy_chord_backfill_worker(stub, ["hard-song"])

    messages = []
    while not stub._queue.empty():
        messages.append(stub._queue.get_nowait())
    results = dict(messages)["easy_chord_backfill_done"]
    assert results["succeeded"] == [] and results["failed"][0][0] == "hard-song"


def test_removing_a_flagged_song_drops_only_its_row(app, isolated, monkeypatch):
    for slug in ("a-song", "b-song"):
        _make_song(isolated, slug, concern="Lyrics could not be confirmed against the vocal.")
    _open(app, "Flagged for Lyrics Review")
    monkeypatch.setattr(gui, "list_flagged_songs", lambda *a, **k: pytest.fail("Remove must not rescan"))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    view = app._song_views["flagged"]
    view.select("a-song")
    app.root.update()

    app._with_flagged_song(app._on_remove_flagged)
    app.root.update()

    assert view.slugs() == ["b-song"] and app._flagged_title_label.cget("text") == ""


# --- YouTube Comments: not built at launch; a refresh never throws away the owner's edits -------------------------------

def _reply(comment_id):
    return PendingReply(comment_id=comment_id, video_id="v", author=f"fan {comment_id}", comment_text="is 1:02 right?",
                        draft_reply=f"draft {comment_id}", is_error_report=False)


def test_pending_replies_are_not_built_at_launch_and_edits_survive_every_refresh(isolated, monkeypatch):
    """F036/F037/F047/F048: every draft used to be built at launch, and every 20-minute comment check (and every other
    Approve/Dismiss) rebuilt them all from disk -- resetting whatever reply the owner was typing."""
    replies = [_reply("a"), _reply("b")]

    def remove_pending_reply(comment_id):
        replies[:] = [r for r in replies if r.comment_id != comment_id]

    monkeypatch.setattr(gui, "load_pending_replies", lambda: list(replies))
    monkeypatch.setattr(gui, "remove_pending_reply", remove_pending_reply)
    try:
        root = _new_ctk_root(ctk)
    except Exception:
        pytest.skip("no display available for a real window")
    root.withdraw()
    try:
        app = LyricVideoGUI(root)
        view = app.replies_view
        assert view.ids() == [] and app.youtube_replies_count_var.get() == "2 waiting"   # nothing built until opened

        _open(app, "YouTube Comments")
        assert view.ids() == ["a", "b"]
        view.select("a")
        view.editor.delete("1.0", "end")
        view.editor.insert("1.0", "owner edit")

        app._render_pending_replies()                       # a comment check / another Approve refreshes the list
        assert view.editor.get("1.0", "end-1c") == "owner edit"
        view.select("b")
        assert view.editor.get("1.0", "end-1c") == "draft b"
        view.select("a")
        assert view.editor.get("1.0", "end-1c") == "owner edit"   # kept per comment while looking at another one

        view.select("b")
        app._on_dismiss_reply(replies[1])
        assert view.ids() == ["a"]
        view.select("a")
        assert view.editor.get("1.0", "end-1c") == "owner edit"

        approved = []
        app._on_approve_reply = lambda reply, text_box: approved.append((reply.comment_id, text_box.get("1.0", "end").strip()))
        view.approve_button.invoke()
        assert approved == [("a", "owner edit")]              # Approve posts what the owner wrote, not the draft
    finally:
        root.destroy()
        use_pass_share_from(None)
        gc.collect()


def test_the_reply_list_shows_which_video_each_comment_is_on(isolated, monkeypatch):
    """owner, 2026-10-03: "i dont know what song or video the comments are comming from" -- both the row shown in
    the list and the heading above the editor now name the video; an older queued reply with no video_title saved
    (the field didn't exist yet) falls back to a plain placeholder instead of showing blank."""
    reply = PendingReply(comment_id="a", video_id="v", author="fan a", comment_text="is 1:02 right?",
                         draft_reply="draft a", is_error_report=False, video_title="Some Song - Artist - (Play Along)")
    old_reply = PendingReply(comment_id="b", video_id="v2", author="fan b", comment_text="great video",
                             draft_reply="draft b", is_error_report=False)   # video_title defaults to ""
    monkeypatch.setattr(gui, "load_pending_replies", lambda: [reply, old_reply])
    try:
        root = _new_ctk_root(ctk)
    except Exception:
        pytest.skip("no display available for a real window")
    root.withdraw()
    try:
        app = LyricVideoGUI(root)
        view = app.replies_view
        _open(app, "YouTube Comments")

        assert "Some Song - Artist - (Play Along)" in view.tree.item("a", "values")[1]
        view.select("a")
        assert "Some Song - Artist - (Play Along)" in view.heading.cget("text")
        view.select("b")
        assert "(unknown video)" in view.heading.cget("text")
    finally:
        root.destroy()
        gc.collect()


def test_a_comment_check_that_finds_nothing_new_does_not_touch_the_panel(monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(gui.youtube_auth, "load_credentials", lambda: "credentials")
    monkeypatch.setattr(gui, "build", lambda *a, **k: "client")
    monkeypatch.setattr(gui.anthropic, "Anthropic", lambda: "claude")
    monkeypatch.setattr(gui, "load_seen_comment_ids", lambda: set())
    scheduled = []
    stub = SimpleNamespace(settings=Settings(), root=SimpleNamespace(after=lambda delay, cb, *a: scheduled.append(cb)))

    LyricVideoGUI._check_youtube_comments_worker(stub)

    assert scheduled == []


def test_a_comment_check_that_finds_new_comments_only_adds_them(monkeypatch, tmp_path):
    from lyricvideo.youtube_state import YoutubeState, save_youtube_state

    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    song_dir = tmp_path / "work" / "some-song"
    song_dir.mkdir(parents=True)
    save_youtube_state(song_dir, YoutubeState(video_id="vid", uploaded_at="2026-09-01T00:00:00", title="Some Song"))
    monkeypatch.setattr(gui.youtube_auth, "load_credentials", lambda: "credentials")
    monkeypatch.setattr(gui, "build", lambda *a, **k: "client")
    monkeypatch.setattr(gui.anthropic, "Anthropic", lambda: "claude")
    monkeypatch.setattr(gui, "load_seen_comment_ids", lambda: set())
    monkeypatch.setattr(gui, "_video_comment_stats", lambda client, ids: {"vid": (True, 1)})    # public, 1 comment
    comment = SimpleNamespace(comment_id="c9", video_id="vid", author="fan", text="love it")
    monkeypatch.setattr(gui, "list_new_comments", lambda client, video_id, seen: [comment])
    monkeypatch.setattr(gui, "draft_comment_reply", lambda client, text, title: ("thanks!", False))
    added, seen = [], []
    monkeypatch.setattr(gui, "add_pending_reply", lambda reply: added.append(reply.comment_id) or True)
    monkeypatch.setattr(gui, "mark_comment_seen", lambda comment_id: seen.append(comment_id))
    scheduled = []
    stub = SimpleNamespace(settings=Settings(), root=SimpleNamespace(after=lambda delay, cb, *a: scheduled.append(cb)),
                           _on_pending_replies_changed="ADD-NEW-ROWS", _render_pending_replies=_fail_if_called)

    LyricVideoGUI._check_youtube_comments_worker(stub)

    assert added == ["c9"] and seen == ["c9"] and scheduled == ["ADD-NEW-ROWS"]


def _fail_if_called(*args, **kwargs):
    raise AssertionError("must not rebuild every reply row")


# --- the Settings popup ----------------------------------------------------------------------------------------------------

def test_the_settings_popup_is_built_once_and_reopens_without_rebuilding(app, monkeypatch):
    """F029: its ~500 widgets were rebuilt on every open (a second or more here, several on the owner's box)."""
    from lyricvideo import settings_panel

    built = []
    real_init = settings_panel.SettingsPanel.__init__

    def counting_init(self, *args, **kwargs):
        built.append(1)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(settings_panel.SettingsPanel, "__init__", counting_init)
    app._open_settings_window()
    dialog = app._settings_window
    app.root.tk.call(dialog.protocol("WM_DELETE_WINDOW"))    # its own X
    assert dialog.winfo_exists() and dialog.state() == "withdrawn" and not app._settings_window_shown

    app._open_settings_window()

    assert built == [1] and app._settings_window is dialog and app._settings_window_shown
    app.root.tk.call(dialog.protocol("WM_DELETE_WINDOW"))


def test_closing_settings_with_unsaved_changes_asks_over_the_popup_and_reverts(app, monkeypatch):
    """F138: a parentless confirm asked from the transient + grab_set popup can open behind it on some Linux WMs."""
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, message, **kw: asked.append(kw.get("parent")) or True)
    app._open_settings_window()
    dialog = app._settings_window
    app.settings_panel.vars["lyric_size"].set(99)
    assert app.settings.lyric_size == 99                        # the live session value follows at once

    app.root.tk.call(dialog.protocol("WM_DELETE_WINDOW"))

    assert asked == [dialog]
    assert app.settings.lyric_size == Settings().lyric_size    # reverted to what is on disk
    assert dialog.state() == "withdrawn" and not app._settings_window_shown


def test_a_reopened_settings_popup_retries_its_grab_until_the_window_manager_has_mapped_it():
    """Reshowing the withdrawn popup: on X11 grab_set() fails ("window not viewable") until the window manager maps it, which
    is usually a moment after deiconify() returns. A failed grab used to be dropped, leaving the reopened popup non-modal
    -- and the song lists only follow a moved pass mark when it closes, relying on it being modal."""
    import tkinter as tk

    class _Dialog:
        def __init__(self, failures):
            self.failures, self.grabbed, self.later = failures, 0, []

        def grab_set(self):
            if self.failures:
                self.failures -= 1
                raise tk.TclError("grab failed: window not viewable")
            self.grabbed += 1

        def after(self, _delay, callback):
            self.later.append(callback)

    stub = SimpleNamespace(_settings_window_shown=True)
    stub._grab_settings_dialog = lambda dialog, tries_left=20: LyricVideoGUI._grab_settings_dialog(stub, dialog, tries_left)
    dialog = _Dialog(failures=2)
    LyricVideoGUI._grab_settings_dialog(stub, dialog)
    while dialog.later:
        dialog.later.pop(0)()
    assert dialog.grabbed == 1

    closed = _Dialog(failures=1)
    LyricVideoGUI._grab_settings_dialog(stub, closed)
    stub._settings_window_shown = False                # closed again before the retry came round
    while closed.later:
        closed.later.pop(0)()
    assert closed.grabbed == 0


# --- closing the main window ------------------------------------------------------------------------------------------------

def test_the_close_button_hides_the_window_before_tearing_anything_down(app, isolated):
    """F033: root.destroy() walked every widget from Python with the window still up and frozen -- seconds with a list
    open (~5,000 widgets for the owner's library), far longer on his box. Now the window disappears first, and with the
    song lists as Treeviews there are only a few hundred widgets left to free."""
    for i in range(100):
        _make_song(isolated, f"song-{i:03d}", video=False, key=None)
    _open(app, "Redo an Existing Song")
    widgets = _count_widgets(app.root)
    seen = []
    real_destroy = app._destroy_root
    app._destroy_root = lambda: (seen.append(app.root.state()), real_destroy())

    app.root.tk.call(app.root.protocol("WM_DELETE_WINDOW"))

    assert seen == ["withdrawn"] and app._closing is True       # gone from the screen BEFORE the teardown starts
    assert widgets < 1000                                       # was ~11 per song, per open list
    with pytest.raises(Exception):
        app.root.winfo_exists()                                 # ...and then the whole window really was destroyed
    app._refresh_song_list("redo")                              # nothing new starts once closing
    assert not app._list_loads_busy()


def _new_ctk_root(ctk):
    """A real CTk root. On this Windows box, creating one under pytest's output capture intermittently fails once with
    "Can't find a usable init.tcl/tk.tcl" (~1 in 40 roots) and succeeds on the very next try -- so retry a couple of times
    before calling the display unavailable (a skip would hide a real regression)."""
    for attempt in range(3):
        try:
            return ctk.CTk()
        except Exception:
            if attempt == 2:
                raise
