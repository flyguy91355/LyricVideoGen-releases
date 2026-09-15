from lyricvideo.gui import (
    PROJECT_ROOT, _default_work_dir_from_audio, _maybe_upload_to_youtube, _retry_pending_uploads,
    _slugify, _split_log_text,
)
from lyricvideo.settings import Settings
from lyricvideo.youtube_state import YoutubeState, save_youtube_state


def test_maybe_upload_to_youtube_skips_when_auto_upload_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.youtube_auth.load_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("should not check credentials when disabled")),
    )
    settings = Settings(youtube_auto_upload=False)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_skips_when_not_connected(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: None)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: calls.append(True) or (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise

    assert calls == []


def test_maybe_upload_to_youtube_skips_when_already_uploaded_and_still_live(tmp_path, monkeypatch):
    save_youtube_state(tmp_path, YoutubeState(video_id="abc", uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.video_exists", lambda client, video_id: True)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not re-upload a still-live video")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_re_uploads_when_saved_video_was_deleted_on_youtube(tmp_path, monkeypatch):
    """Real incident 2026-09-10: the owner deleted "Come As You Are" directly
    on YouTube after a redo; the stale local youtube_state.json kept the
    song permanently stuck as "already uploaded" with no way to recover
    short of manually deleting the JSON file. video_exists() now catches
    this and lets the real upload proceed."""
    save_youtube_state(tmp_path, YoutubeState(video_id="deleted-id", uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr("lyricvideo.gui.video_exists", lambda client, video_id: False)
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [tmp_path]


def test_maybe_upload_to_youtube_skips_when_verification_itself_fails(tmp_path, monkeypatch):
    """A network hiccup while checking video_exists must fail CLOSED (skip,
    not upload) -- never risk a duplicate video because a status check
    happened to time out."""
    save_youtube_state(tmp_path, YoutubeState(video_id="abc", uploaded_at="2026-01-01T00:00:00", title="t"))
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")

    def _raise(client, video_id):
        raise RuntimeError("network hiccup")

    monkeypatch.setattr("lyricvideo.gui.video_exists", _raise)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload when verification failed")),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_maybe_upload_to_youtube_calls_schedule_upload_when_eligible(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(
            (youtube_client, anthropic_client, work_dir, settings)
        ),
    )
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)

    assert calls == [("fake-youtube-client", "fake-anthropic-client", tmp_path, settings)]


def test_maybe_upload_to_youtube_never_raises_on_upload_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", _raise)
    settings = Settings(youtube_auto_upload=True)

    _maybe_upload_to_youtube(tmp_path, settings)  # must not raise


def test_retry_pending_uploads_uploads_every_pending_song(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )
    settings = Settings()

    results = _retry_pending_uploads(tmp_path, settings)

    assert calls == [tmp_path / "song-a", tmp_path / "song-b"]
    assert results == {"succeeded": ["song-a", "song-b"], "failed": []}


def test_retry_pending_uploads_continues_after_a_single_song_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a", "song-b"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")

    def upload(youtube_client, anthropic_client, work_dir, settings):
        if work_dir.name == "song-a":
            raise RuntimeError("uploadLimitExceeded")

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", upload)
    settings = Settings()

    results = _retry_pending_uploads(tmp_path, settings)

    assert results == {
        "succeeded": ["song-b"],
        "failed": [("song-a", "RuntimeError: uploadLimitExceeded")],
    }


def test_retry_pending_uploads_raises_when_not_connected(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.list_pending_uploads", lambda work_root: ["song-a"])
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: None)
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )

    try:
        _retry_pending_uploads(tmp_path, Settings())
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_retry_pending_uploads_only_attempts_the_given_slugs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.gui.list_pending_uploads",
        lambda work_root: (_ for _ in ()).throw(AssertionError("should not list all pending songs")),
    )
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")
    calls = []
    monkeypatch.setattr(
        "lyricvideo.gui.schedule_upload",
        lambda youtube_client, anthropic_client, work_dir, settings: calls.append(work_dir),
    )

    results = _retry_pending_uploads(tmp_path, Settings(), slugs=["song-a"])

    assert calls == [tmp_path / "song-a"]
    assert results == {"succeeded": ["song-a"], "failed": []}


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Wish You Were Here") == "wish-you-were-here"


def test_slugify_strips_punctuation():
    assert _slugify("Turn The Page (Live!)") == "turn-the-page-live"


def test_slugify_collapses_whitespace_and_trims_hyphens():
    assert _slugify("  Some   Song  ") == "some-song"


def test_slugify_falls_back_on_empty_title():
    assert _slugify("") == "untitled-song"
    assert _slugify("   ") == "untitled-song"


def test_default_work_dir_from_audio_uses_the_audio_filenames_own_stem():
    """Real incident, 2026-09-13: Generate clicked before (or despite) the
    GUI's own background title-identification finishing left both the title
    and work-directory fields blank, blocking Generate with no way to
    proceed short of typing something into Title -- since there's no Browse
    button for the work-directory field itself. This fallback (wired into
    _on_generate) means a blank work directory never blocks Generate."""
    result = _default_work_dir_from_audio("/some/path/01 My Song.mp3")
    assert result == str(PROJECT_ROOT / "work" / "01-my-song")


def test_default_work_dir_from_audio_matches_title_based_slug_convention():
    """Uses the exact same slugify() call _on_title_changed already uses for
    a real identified title, just fed the filename stem instead."""
    assert _default_work_dir_from_audio("/x/Some Song.mp3") == str(
        PROJECT_ROOT / "work" / _slugify("Some Song")
    )


def test_split_log_text_plain_text_with_no_newline_stays_pending():
    pending, to_commit = _split_log_text("", "loading")
    assert pending == "loading"
    assert to_commit == ""


def test_split_log_text_newline_commits_the_line():
    pending, to_commit = _split_log_text("", "done\n")
    assert pending == ""
    assert to_commit == "done\n"


def test_split_log_text_accumulates_across_calls_until_newline():
    pending, to_commit = _split_log_text("load", "ing")
    assert pending == "loading"
    assert to_commit == ""
    pending, to_commit = _split_log_text(pending, "...\n")
    assert pending == ""
    assert to_commit == "loading...\n"


def test_split_log_text_carriage_return_discards_pending_line():
    # tqdm-style: prints a whole new percentage after \r, not an appendix to
    # the old one -- \r must throw away whatever was pending, not keep it.
    pending, to_commit = _split_log_text("50%", "\r75%")
    assert pending == "75%"
    assert to_commit == ""


def test_split_log_text_progress_bar_burst_collapses_to_one_committed_line():
    # A realistic tqdm burst: many \r-separated updates in one chunk, then a
    # final newline -- only the LAST update should ever get committed.
    text = "\r10%|#|\r50%|#####|\r100%|##########|\n"
    pending, to_commit = _split_log_text("", text)
    assert pending == ""
    assert to_commit == "100%|##########|\n"


def test_split_log_text_multiple_real_lines_all_committed():
    pending, to_commit = _split_log_text("", "line one\nline two\n")
    assert pending == ""
    assert to_commit == "line one\nline two\n"


def test_split_log_text_empty_chunk_is_a_noop():
    pending, to_commit = _split_log_text("partial", "")
    assert pending == "partial"
    assert to_commit == ""


# --- GUI worker error paths ------------------------------------------------
# These drive real LyricVideoGUI methods against a plain stub `self` (no Tk
# window) with threading.Thread swapped for a synchronous stand-in, so the
# callbacks a worker schedules via root.after() can be asserted on directly.

from types import SimpleNamespace  # noqa: E402

from lyricvideo.gui import LyricVideoGUI  # noqa: E402
from lyricvideo.youtube_comment_state import PendingReply  # noqa: E402


class _ImmediateThread:
    """Stands in for threading.Thread: runs the target synchronously on
    start(), so a worker's after()-scheduled callbacks fire before the test
    asserts."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _ImmediateRoot:
    """A root whose after() runs the callback immediately -- the exact call
    site where the deferred error-dialog lambdas below used to raise."""

    def after(self, _delay, callback, *args):
        callback(*args)


def _gui_stub(**attrs):
    attrs.setdefault("settings", Settings())
    return SimpleNamespace(root=_ImmediateRoot(), **attrs)


def _must_not_run(*args, **kwargs):
    raise AssertionError("this must not be reached")


def test_connect_youtube_failure_shows_the_error_dialog(monkeypatch):
    """Real latent bug (static analysis, 2026-09-14): the worker's except
    block handed `e` to a lambda that root.after() ran later -- but Python
    unbinds `e` the moment the except block ends, so that lambda raised
    NameError inside the Tk event loop and the owner never saw why the
    connect failed. Same bug in the manual-upload and Approve workers."""
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)

    def failing_connect(path):
        raise RuntimeError("consent screen closed")

    monkeypatch.setattr("lyricvideo.gui.youtube_auth.connect", failing_connect)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub(settings=Settings(youtube_client_secrets_path="secrets.json"))
    LyricVideoGUI._on_connect_youtube(stub)

    assert shown == [("Could not connect to YouTube", "RuntimeError: consent screen closed")]


def test_connect_youtube_success_refreshes_the_status_label(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.connect", lambda path: "credentials")
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", _must_not_run)
    refreshed = []

    stub = _gui_stub(
        settings=Settings(youtube_client_secrets_path="secrets.json"),
        _refresh_youtube_status=lambda: refreshed.append(True),
    )
    LyricVideoGUI._on_connect_youtube(stub)

    assert refreshed == [True]


def test_manual_upload_failure_shows_the_error_dialog(monkeypatch, tmp_path):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")
    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-anthropic-client")

    def failing_upload(*args, **kwargs):
        raise RuntimeError("uploadLimitExceeded")

    monkeypatch.setattr("lyricvideo.gui.schedule_upload", failing_upload)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))
    button_states = []
    refreshed_for = []

    stub = _gui_stub(
        _last_work_dir=tmp_path,
        upload_button=SimpleNamespace(configure=lambda **kw: button_states.append(kw)),
        _update_upload_button_state=refreshed_for.append,
    )
    LyricVideoGUI._on_manual_upload(stub)

    assert shown == [("Upload failed", "RuntimeError: uploadLimitExceeded")]
    assert button_states == [{"state": "disabled"}]
    assert refreshed_for == [tmp_path]  # the finally-block refresh still ran


def test_approve_reply_failure_shows_the_error_dialog_and_keeps_the_draft(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("lyricvideo.gui.youtube_auth.load_credentials", lambda: "fake-credentials")
    monkeypatch.setattr("lyricvideo.gui.build", lambda *a, **k: "fake-youtube-client")

    def failing_post(client, comment_id, text):
        raise RuntimeError("commentsDisabled")

    monkeypatch.setattr("lyricvideo.gui.post_reply", failing_post)
    monkeypatch.setattr("lyricvideo.gui.remove_pending_reply", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    reply = PendingReply(
        comment_id="c1", video_id="v1", author="fan", comment_text="wrong chord at 1:02?",
        draft_reply="Thanks!", is_error_report=True,
    )
    text_box = SimpleNamespace(get=lambda start, end: "Thanks for the heads-up!\n")
    LyricVideoGUI._on_approve_reply(_gui_stub(), reply, text_box)

    assert shown == [("Could not post reply", "RuntimeError: commentsDisabled")]


def test_generate_refuses_a_missing_audio_file_before_starting_anything(monkeypatch, tmp_path):
    """A typo'd or moved path used to surface only minutes later as an
    obscure Demucs/ffmpeg failure deep in the log."""
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)

    stub = _gui_stub(
        _running=False,
        title_var=SimpleNamespace(get=lambda: "Song"),
        audio_var=SimpleNamespace(get=lambda: str(tmp_path / "gone.mp3")),
        work_dir_var=SimpleNamespace(get=lambda: "", set=_must_not_run),
    )
    LyricVideoGUI._on_generate(stub)

    assert [title for title, _ in shown] == ["Audio file not found"]
    assert str(tmp_path / "gone.mp3") in shown[0][1]


def test_redo_refuses_when_the_original_audio_file_is_gone(monkeypatch, tmp_path):
    """The redo re-reads the original audio (sidecar lyrics, final render);
    if the owner moved it since, fail with the path up front -- before
    backing anything up or starting a run that would die at render time."""
    monkeypatch.setattr("lyricvideo.gui.load_redo_inputs", lambda song_dir: (tmp_path / "gone.mp3", "Angie"))
    monkeypatch.setattr("lyricvideo.gui.backup_song_outputs", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", _must_not_run)
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub(_running=False, redo_song_var=SimpleNamespace(get=lambda: "angie"))
    LyricVideoGUI._on_redo(stub)

    assert [title for title, _ in shown] == ["Original audio file not found"]
    assert str(tmp_path / "gone.mp3") in shown[0][1]
    assert "Angie" in shown[0][1]


def test_retry_upload_refuses_when_nothing_is_selected(monkeypatch):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _must_not_run)
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, msg: shown.append((title, msg)))

    stub = _gui_stub(_running=False, retry_upload_song_var=SimpleNamespace(get=lambda: "  "))
    LyricVideoGUI._on_retry_upload(stub)

    assert [title for title, _ in shown] == ["No song selected"]


def test_retry_upload_all_shows_a_summary_and_refreshes_the_dropdown(monkeypatch, tmp_path):
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(
        "lyricvideo.gui._retry_pending_uploads",
        lambda work_root, settings, slugs: {"succeeded": ["song-a"], "failed": [("song-b", "RuntimeError: boom")]},
    )
    shown = []
    monkeypatch.setattr("lyricvideo.gui.messagebox.showinfo", lambda title, msg: shown.append((title, msg)))
    button_states = []
    refreshed = []

    stub = _gui_stub(
        _running=False,
        retry_upload_button=SimpleNamespace(configure=lambda **kw: button_states.append(("upload", kw))),
        retry_upload_all_button=SimpleNamespace(configure=lambda **kw: button_states.append(("upload_all", kw))),
        _refresh_retry_upload_options=lambda: refreshed.append(True),
    )
    stub._on_retry_upload_done = lambda results: LyricVideoGUI._on_retry_upload_done(stub, results)
    LyricVideoGUI._start_retry_upload(stub, None)

    assert shown == [(
        "Retry upload results",
        "Uploaded 1 song(s).\n1 failed:\n  song-b: RuntimeError: boom",
    )]
    assert ("upload", {"state": "disabled"}) in button_states
    assert ("upload_all", {"state": "disabled"}) in button_states
    assert refreshed == [True]
