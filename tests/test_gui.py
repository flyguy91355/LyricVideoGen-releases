from lyricvideo.gui import _maybe_upload_to_youtube, _slugify, _split_log_text
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


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Wish You Were Here") == "wish-you-were-here"


def test_slugify_strips_punctuation():
    assert _slugify("Turn The Page (Live!)") == "turn-the-page-live"


def test_slugify_collapses_whitespace_and_trims_hyphens():
    assert _slugify("  Some   Song  ") == "some-song"


def test_slugify_falls_back_on_empty_title():
    assert _slugify("") == "untitled-song"
    assert _slugify("   ") == "untitled-song"


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
