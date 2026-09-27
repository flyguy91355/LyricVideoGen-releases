"""Issue #7 review (gui-behavior, wave 2): the single-song form (a new audio pick replaces the old auto-filled title and
work folder -- F031/F032; a folder holding a different recording is not taken over -- F027/F034), the Batch "already done"
prompt can be cancelled (F112), and the Update dialog (F107-F111). Real song lyrics never appear here; titles are
made up."""

import json
from types import SimpleNamespace

import pytest

from lyricvideo import gui
from lyricvideo.gui import LyricVideoGUI
from lyricvideo.settings import Settings


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _RecordingThread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args = target, args

    def start(self):
        _RecordingThread.started.append(self.args)


def _form_stub(tmp_path, title="", audio=""):
    stub = SimpleNamespace(
        _running=False, _identified_artist="", _auto_filled_title="",
        title_var=_Var(title), audio_var=_Var(audio), work_dir_var=_Var(""),
        youtube_title_preview_var=_Var(""), root=SimpleNamespace(after=lambda delay, cb, *a: cb(*a)),
    )
    # The real trace: a title change points the work folder at work/<slug of the title>.
    stub.title_var.set = lambda value: (setattr(stub.title_var, "value", value),
                                        LyricVideoGUI._on_title_changed(stub))
    stub._update_youtube_title_preview = lambda: LyricVideoGUI._update_youtube_title_preview(stub)
    stub._identify_worker = lambda path: None
    stub._apply_identified_title = lambda *a, **k: LyricVideoGUI._apply_identified_title(stub, *a, **k)
    return stub


def test_picking_another_file_replaces_the_previous_files_auto_filled_title_and_folder(tmp_path, monkeypatch):
    """F031/F032: after "Crazy Moon" (auto-filled) the owner browsed to another file; the old title and work/crazy-moon
    stayed, so Generate overwrote that song's folder -- its video, upload record, key and edited lyrics."""
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    _RecordingThread.started = []
    monkeypatch.setattr(gui.threading, "Thread", _RecordingThread)
    stub = _form_stub(tmp_path, audio="first.mp3")
    LyricVideoGUI._on_audio_selected(stub, "first.mp3")
    LyricVideoGUI._apply_identified_title(stub, "Crazy Moon", "Moonpaper Quartet", "first.mp3")
    assert stub.work_dir_var.get().endswith("crazy-moon")

    stub.audio_var.set("second.mp3")
    LyricVideoGUI._on_audio_selected(stub, "second.mp3")

    assert stub.title_var.get() == "" and stub.work_dir_var.get() == "" and stub._identified_artist == ""
    assert _RecordingThread.started == [("first.mp3",), ("second.mp3",)]     # the new file is identified


def test_a_title_the_owner_typed_is_kept_when_another_file_is_picked(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    _RecordingThread.started = []
    monkeypatch.setattr(gui.threading, "Thread", _RecordingThread)
    stub = _form_stub(tmp_path, title="My Own Title", audio="second.mp3")
    stub._identified_artist = "The Previous File's Band"

    LyricVideoGUI._on_audio_selected(stub, "second.mp3")

    assert stub.title_var.get() == "My Own Title"
    # The previous file's artist never sticks to the new one (it feeds the upload title and the folder check); the new
    # file is still identified, for its artist only.
    assert stub._identified_artist == "" and _RecordingThread.started == [("second.mp3",)]
    LyricVideoGUI._apply_identified_title(stub, "Tagged Title", "Second Band", "second.mp3")
    assert stub.title_var.get() == "My Own Title" and stub._identified_artist == "Second Band"


def test_a_slow_lookup_for_the_previous_file_is_never_applied_to_the_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    stub = _form_stub(tmp_path, audio="second.mp3")

    LyricVideoGUI._apply_identified_title(stub, "Old Song", "Old Band", "first.mp3")

    assert stub.title_var.get() == "" and stub._identified_artist == ""


# --- Generate into a folder that holds another recording (F027/F034) -----------------------------------------------------

def _existing_song(folder, title, artist, audio_name):
    folder.mkdir(parents=True)
    (folder / "song_info.json").write_text(json.dumps({"title": title, "artist": artist}), encoding="utf-8")
    (folder / "lyrics_timed.json").write_text(json.dumps({"title": title, "audio_path": f"/music/{audio_name}"}),
                                              encoding="utf-8")


def _generate_stub(tmp_path, audio, artist):
    started = []
    button = SimpleNamespace(configure=lambda **kw: None)
    stub = SimpleNamespace(
        _running=False, _identified_artist=artist, title_var=_Var("Paper Boat"), audio_var=_Var(str(audio)),
        work_dir_var=_Var(str(tmp_path / "work" / "paper-boat")), generate_button=button, redo_button=button,
        batch_button=button, status_var=_Var(), progress_bar=SimpleNamespace(set=lambda v: None),
        _clear_log=lambda: None, _run_worker=lambda *a, **k: None, _poll_queue=lambda: None,
        root=SimpleNamespace(after=lambda *a: None), started=started,
    )
    stub._settle_generate_work_dir = lambda *a: LyricVideoGUI._settle_generate_work_dir(stub, *a)
    return stub


@pytest.fixture
def _threads(monkeypatch):
    _RecordingThread.started = []
    monkeypatch.setattr(gui.threading, "Thread", _RecordingThread)
    return _RecordingThread.started


def test_generate_asks_before_taking_over_a_folder_that_holds_another_recording(tmp_path, monkeypatch, _threads):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    _existing_song(tmp_path / "work" / "paper-boat", "Paper Boat", "First Band", "first band - paper boat.mp3")
    audio = tmp_path / "second band - paper boat.mp3"
    audio.write_bytes(b"x")
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda title, message: asked.append(message) or True)
    monkeypatch.setattr(gui, "backup_song_outputs", lambda *a: pytest.fail("the other song's folder is left alone"))
    stub = _generate_stub(tmp_path, audio, "Second Band")

    LyricVideoGUI._on_generate(stub)

    assert "First Band" in asked[0]
    assert _threads[0][1] == tmp_path / "work" / "paper-boat-second-band"        # its own folder
    assert stub.work_dir_var.get() == str(tmp_path / "work" / "paper-boat-second-band")


def test_a_new_folder_that_already_holds_this_recording_is_backed_up_before_generate(tmp_path, monkeypatch, _threads):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    _existing_song(tmp_path / "work" / "paper-boat", "Paper Boat", "First Band", "a.mp3")
    _existing_song(tmp_path / "work" / "paper-boat-second-band", "Paper Boat", "Second Band", "b.mp3")
    audio = tmp_path / "b.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda title, message: True)
    backed_up = []
    monkeypatch.setattr(gui, "backup_song_outputs", lambda folder, slug: backed_up.append((folder.name, slug)))
    stub = _generate_stub(tmp_path, audio, "Second Band")

    LyricVideoGUI._on_generate(stub)

    assert backed_up == [("paper-boat-second-band", "paper-boat")]
    assert _threads[0][1] == tmp_path / "work" / "paper-boat-second-band"


def test_a_folder_holding_this_very_audio_file_is_this_song_whatever_the_artist_lookup_says(tmp_path, monkeypatch,
                                                                                            _threads):
    """A tagless file's artist comes from an online lookup that can answer differently next time: the folder holding
    this file's own copy (same name, same size) is still this song -- no question, no second folder."""
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    folder = tmp_path / "work" / "paper-boat"
    _existing_song(folder, "Paper Boat", "First Band", "paper boat.mp3")
    audio = tmp_path / "paper boat.mp3"
    audio.write_bytes(b"same bytes")
    (folder / "paper boat.mp3").write_bytes(b"same bytes")
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda *a: pytest.fail("the same recording needs no question"))
    monkeypatch.setattr(gui, "backup_song_outputs", lambda folder, slug: None)
    stub = _generate_stub(tmp_path, audio, "Some Other Name The Lookup Gave")

    LyricVideoGUI._on_generate(stub)

    assert _threads[0][1] == folder


def test_generate_cancelled_at_the_folder_question_starts_nothing(tmp_path, monkeypatch, _threads):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    _existing_song(tmp_path / "work" / "paper-boat", "Paper Boat", "First Band", "a.mp3")
    audio = tmp_path / "b.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda title, message: None)
    stub = _generate_stub(tmp_path, audio, "Second Band")

    LyricVideoGUI._on_generate(stub)

    assert _threads == [] and stub._running is False


def test_generate_again_over_the_same_song_backs_it_up_first(tmp_path, monkeypatch, _threads):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    _existing_song(tmp_path / "work" / "paper-boat", "Paper Boat", "First Band", "a.mp3")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda *a: pytest.fail("the same song needs no question"))
    backed_up = []
    monkeypatch.setattr(gui, "backup_song_outputs", lambda folder, slug: backed_up.append((folder.name, slug)))
    stub = _generate_stub(tmp_path, audio, "First Band")

    LyricVideoGUI._on_generate(stub)

    assert backed_up == [("paper-boat", "paper-boat")] and _threads[0][1] == tmp_path / "work" / "paper-boat"


# --- Batch: "already done" can be cancelled (F112) -----------------------------------------------------------------------

def test_dismissing_the_already_done_prompt_cancels_the_batch_instead_of_regenerating_everything(monkeypatch):
    """F112: Esc / the window's X on a yes/no prompt answered "No" = regenerate every finished song, for hours."""
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda title, message: None)
    states = []
    button = SimpleNamespace(configure=lambda **kw: states.append(kw))
    stub = SimpleNamespace(_running=True, generate_button=button, redo_button=button, batch_button=button,
                           status_var=_Var("Scanning folder..."),
                           _start_batch_run=lambda items: pytest.fail("the batch must not start"))
    items = [SimpleNamespace(already_done=True), SimpleNamespace(already_done=False)]

    LyricVideoGUI._on_batch_resolved(stub, items)

    assert stub._running is False and stub.status_var.get() == "Ready"
    assert states == [{"state": "normal"}] * 3


@pytest.mark.parametrize("answer, expected", [(True, 1), (False, 2)])
def test_the_already_done_prompt_still_skips_or_regenerates(monkeypatch, answer, expected):
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda title, message: answer)
    started = []
    stub = SimpleNamespace(_running=True, _start_batch_run=lambda items: started.append(items))
    items = [SimpleNamespace(already_done=True), SimpleNamespace(already_done=False)]

    LyricVideoGUI._on_batch_resolved(stub, items)

    assert len(started[0]) == expected


# --- the Update dialog (F107-F111) ---------------------------------------------------------------------------------------

def _update_stub(**attrs):
    after = []
    attrs.setdefault("_closing", False)
    attrs.setdefault("_update_dialog_window", None)
    attrs.setdefault("_update_apply_in_progress", False)
    attrs.setdefault("_running", False)
    stub = SimpleNamespace(root=SimpleNamespace(after=lambda delay, cb, *a: after.append(cb)), after=after, **attrs)
    stub._update_dialog_alive = lambda: LyricVideoGUI._update_dialog_alive(stub)
    stub._handle_update_message = lambda kind, payload: LyricVideoGUI._handle_update_message(stub, kind, payload)
    stub._show_apply_result = lambda *a, **k: LyricVideoGUI._show_apply_result(stub, *a, **k)
    stub._busy_reasons = lambda: LyricVideoGUI._busy_reasons(stub)
    stub._confirm_quit_if_busy = lambda parent=None: LyricVideoGUI._confirm_quit_if_busy(stub, parent)
    return stub


def test_the_update_poll_keeps_going_when_a_handler_fails(monkeypatch):
    """F107/F108: one handler raising (e.g. drawing Relaunch Now into a dialog closed meanwhile) ended the poll for the
    rest of the session -- no later result was ever shown."""
    import queue

    handled = []
    stub = _update_stub(_update_queue=queue.Queue(), _poll_update_queue="POLL-AGAIN")
    stub._on_apply_update_done = lambda tag: (_ for _ in ()).throw(RuntimeError("bad window path name"))
    stub._on_apply_update_error = lambda message: handled.append(message)
    stub._update_queue.put(("apply_done", "v9.9.9"))
    stub._update_queue.put(("apply_error", "pip failed"))

    LyricVideoGUI._poll_update_queue(stub)

    assert handled == ["pip failed"] and stub.after == ["POLL-AGAIN"]     # the rest still handled, and it polls again


def test_an_apply_result_after_the_dialog_is_gone_is_shown_plainly(monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message: shown.append(message))
    stub = _update_stub(_update_apply_in_progress=True)

    LyricVideoGUI._on_apply_update_done(stub, "v9.9.9")

    assert "v9.9.9" in shown[0] and stub._update_apply_in_progress is False


def test_apply_update_is_refused_while_a_video_is_being_made(monkeypatch):
    """F110/F111: pip install and the file copy ran under a live Batch."""
    monkeypatch.setattr(gui.threading, "Thread", lambda *a, **k: pytest.fail("no update may start"))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: pytest.fail("refused before asking"))
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message, **kw: shown.append(message))
    stub = _update_stub(_running=True)

    LyricVideoGUI._on_apply_update_clicked(stub, {"tag_name": "v9.9.9"})

    assert "Generate / Redo / Batch" in shown[0]


def test_a_second_apply_while_one_runs_is_ignored(monkeypatch):
    """F108/F109: reopening the dialog offered a second Apply -- two pip installs in the same venv at once."""
    monkeypatch.setattr(gui.threading, "Thread", lambda *a, **k: pytest.fail("no second apply"))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: pytest.fail("no second question"))

    LyricVideoGUI._on_apply_update_clicked(_update_stub(_update_apply_in_progress=True), {"tag_name": "v9.9.9"})


def test_the_banner_brings_back_the_open_update_dialog_instead_of_opening_another():
    shown_again = []
    stub = _update_stub(_available_update={"tag_name": "v9.9.9"})
    stub._update_dialog_alive = lambda: True
    stub._show_update_dialog_again = lambda: shown_again.append(True)
    stub._open_update_dialog = lambda release: pytest.fail("never a second dialog")

    LyricVideoGUI._on_update_banner_clicked(stub)

    assert shown_again == [True]


def test_relaunch_asks_first_while_a_batch_runs_and_stays_when_told_no(monkeypatch):
    """F110/F111: Relaunch Now killed a running Batch without the question the window's X asks."""
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, message, **kw: False)
    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **k: pytest.fail("must not relaunch"))
    stub = _update_stub(_running=True, _shut_down=lambda: pytest.fail("must not quit"))

    LyricVideoGUI._on_relaunch_clicked(stub)


def test_relaunch_with_nothing_running_relaunches(monkeypatch):
    monkeypatch.setattr(gui, "upload_in_progress", lambda: False)
    launched, shut = [], []
    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **k: launched.append(a))
    stub = _update_stub(_shut_down=lambda: shut.append(True))

    LyricVideoGUI._on_relaunch_clicked(stub)

    assert len(launched) == 1 and shut == [True]


def test_closing_the_update_dialog_during_an_apply_hides_it_and_its_result_brings_it_back(monkeypatch):
    ctk = pytest.importorskip("customtkinter")
    try:
        root = ctk.CTk()
    except Exception as e:
        pytest.skip(f"no display available for a real window ({type(e).__name__}: {e})")
    try:
        root.withdraw()
        app = SimpleNamespace(root=root, _update_apply_in_progress=False, _update_dialog_window=None)
        for name in ("_update_dialog_alive", "_show_update_dialog_again", "_show_apply_result", "_on_apply_update_done",
                     "_on_relaunch_clicked", "_on_apply_update_clicked"):
            setattr(app, name, (lambda n: lambda *a, **k: getattr(LyricVideoGUI, n)(app, *a, **k))(name))
        LyricVideoGUI._open_update_dialog(app, {"tag_name": "v9.9.9", "notes": "made-up notes"})
        dialog = app._update_dialog_window
        app._update_apply_in_progress = True                      # Apply was clicked; the download is running

        close = [w for w in app._update_button_frame.winfo_children() if w.cget("text") == "Close"][0]
        close.invoke()
        root.update()
        assert app._update_dialog_window is dialog and dialog.winfo_exists() and dialog.state() == "withdrawn"

        app._on_apply_update_done("v9.9.9")                        # the result arrives
        root.update()
        assert dialog.state() == "normal" and app._update_apply_in_progress is False
        assert any(w.cget("text") == "Relaunch Now" for w in app._update_button_frame.winfo_children())

        close.invoke()                                             # nothing running any more: closes for good
        assert app._update_dialog_window is None
    finally:
        root.destroy()
