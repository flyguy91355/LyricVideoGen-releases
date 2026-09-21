import sys

import pytest

from lyricvideo.batch import (
    BatchItem,
    find_audio_files,
    load_last_batch_folder,
    release_memory,
    resolve_batch_items,
    resolve_existing_folder,
    save_last_batch_folder,
)


def test_load_last_batch_folder_missing_file_returns_empty(tmp_path):
    assert load_last_batch_folder(tmp_path / "no_such_state.json") == ""


def test_save_then_load_last_batch_folder_round_trips(tmp_path):
    state_file = tmp_path / "batch_state.json"

    save_last_batch_folder("/mnt/media/batch music ", state_file)

    assert load_last_batch_folder(state_file) == "/mnt/media/batch music "


def test_save_last_batch_folder_creates_parent_dir(tmp_path):
    state_file = tmp_path / "nested" / "batch_state.json"

    save_last_batch_folder("/some/folder", state_file)

    assert load_last_batch_folder(state_file) == "/some/folder"


def test_load_last_batch_folder_corrupt_file_returns_empty(tmp_path):
    state_file = tmp_path / "batch_state.json"
    state_file.write_text("not json", encoding="utf-8")

    assert load_last_batch_folder(state_file) == ""


def test_resolve_existing_folder_returns_real_dir_unchanged(tmp_path):
    real = tmp_path / "songs"
    real.mkdir()

    assert resolve_existing_folder(real) == real


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="NTFS strips trailing spaces from folder names, so the scenario cannot exist on Windows",
)
def test_resolve_existing_folder_recovers_dropped_trailing_space(tmp_path):
    (tmp_path / "batch music ").mkdir()
    dialog_returned = tmp_path / "batch music"  # trailing space missing

    assert resolve_existing_folder(dialog_returned) == tmp_path / "batch music "


def test_resolve_existing_folder_recovers_dropped_leading_space(tmp_path):
    (tmp_path / " batch music").mkdir()
    dialog_returned = tmp_path / "batch music"

    assert resolve_existing_folder(dialog_returned) == tmp_path / " batch music"


def test_resolve_existing_folder_leaves_genuinely_missing_folder_alone(tmp_path):
    missing = tmp_path / "nope"

    assert resolve_existing_folder(missing) == missing


def test_resolve_existing_folder_no_match_when_ambiguous(tmp_path):
    (tmp_path / "batch music ").mkdir()
    (tmp_path / " batch music").mkdir()
    dialog_returned = tmp_path / "batch music"

    assert resolve_existing_folder(dialog_returned) == dialog_returned


def test_find_audio_files_filters_by_extension(tmp_path):
    (tmp_path / "song1.mp3").write_bytes(b"")
    (tmp_path / "song2.wav").write_bytes(b"")
    (tmp_path / "song3.m4a").write_bytes(b"")
    (tmp_path / "song4.flac").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    (tmp_path / "cover.jpg").write_bytes(b"")

    found = find_audio_files(tmp_path)

    assert [f.name for f in found] == ["song1.mp3", "song2.wav", "song3.m4a", "song4.flac"]


def test_find_audio_files_sorted_alphabetically(tmp_path):
    (tmp_path / "zebra.mp3").write_bytes(b"")
    (tmp_path / "apple.mp3").write_bytes(b"")
    (tmp_path / "mango.mp3").write_bytes(b"")

    found = find_audio_files(tmp_path)

    assert [f.name for f in found] == ["apple.mp3", "mango.mp3", "zebra.mp3"]


def test_find_audio_files_ignores_subfolders(tmp_path):
    (tmp_path / "top.mp3").write_bytes(b"")
    subfolder = tmp_path / "subfolder"
    subfolder.mkdir()
    (subfolder / "nested.mp3").write_bytes(b"")

    found = find_audio_files(tmp_path)

    assert [f.name for f in found] == ["top.mp3"]


def test_find_audio_files_empty_folder_returns_empty_list(tmp_path):
    assert find_audio_files(tmp_path) == []


def test_resolve_batch_items_resolves_title_and_work_dir(tmp_path, monkeypatch):
    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": "Some Song"})(),
    )

    items = resolve_batch_items([audio], work_root)

    assert items == [
        BatchItem(audio_path=audio, title="Some Song", work_dir=work_root / "some-song", already_done=False)
    ]


def test_resolve_batch_items_already_done_true_when_video_exists(tmp_path, monkeypatch):
    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"
    finished_dir = work_root / "some-song"
    finished_dir.mkdir(parents=True)
    (finished_dir / "some-song.mp4").write_bytes(b"fake video")

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": "Some Song"})(),
    )

    items = resolve_batch_items([audio], work_root)

    assert items[0].already_done is True


def test_resolve_batch_items_resume_stage_is_fetch_lyrics_when_demucs_stems_exist(tmp_path, monkeypatch):
    """A song interrupted (app closed/crashed) after separate() already
    finished must not redo the slowest stage in the pipeline from scratch
    on the next Start Batch -- real owner complaint, 2026-09-18: closing
    mid-batch left the interrupted song with "no way to resume"."""
    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"
    demucs_dir = work_root / "some-song" / "htdemucs" / "some-song"
    demucs_dir.mkdir(parents=True)
    (demucs_dir / "vocals.wav").write_bytes(b"fake vocals")
    (demucs_dir / "no_vocals.wav").write_bytes(b"fake instrumental")

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": "Some Song"})(),
    )

    items = resolve_batch_items([audio], work_root)

    assert items[0].already_done is False
    assert items[0].resume_stage == "fetch_lyrics"


def test_resolve_batch_items_resume_stage_is_identify_when_stems_incomplete(tmp_path, monkeypatch):
    """Only one of the two stem files exists (separate() was itself
    interrupted partway) -- must not resume past a stage that never
    actually finished."""
    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"
    demucs_dir = work_root / "some-song" / "htdemucs" / "some-song"
    demucs_dir.mkdir(parents=True)
    (demucs_dir / "vocals.wav").write_bytes(b"fake vocals")

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": "Some Song"})(),
    )

    items = resolve_batch_items([audio], work_root)

    assert items[0].resume_stage == "identify"


def test_resolve_batch_items_falls_back_to_filename_stem_on_extraction_failure(tmp_path, monkeypatch):
    """A file whose metadata extraction raises still gets a usable, UNIQUE
    identity (its own filename stem) -- not a shared constant like "" or
    "untitled" that would collide with every other failing file in the same
    batch (caught during the design spec's own self-review, 2026-09-10)."""
    audio1 = tmp_path / "broken-one.mp3"
    audio2 = tmp_path / "broken-two.mp3"
    audio1.write_bytes(b"")
    audio2.write_bytes(b"")
    work_root = tmp_path / "work"

    def raise_error(path):
        raise RuntimeError("no tags")

    monkeypatch.setattr("lyricvideo.batch.extract_metadata", raise_error)

    items = resolve_batch_items([audio1, audio2], work_root)

    assert items[0].title == "broken-one"
    assert items[1].title == "broken-two"
    assert items[0].work_dir != items[1].work_dir
    assert items[0].already_done is False
    assert items[1].already_done is False


def test_resolve_batch_items_preserves_input_order(tmp_path, monkeypatch):
    audio_a = tmp_path / "a.mp3"
    audio_b = tmp_path / "b.mp3"
    audio_a.write_bytes(b"")
    audio_b.write_bytes(b"")
    work_root = tmp_path / "work"

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": path.stem.upper()})(),
    )

    items = resolve_batch_items([audio_a, audio_b], work_root)

    assert [i.audio_path for i in items] == [audio_a, audio_b]


def test_release_memory_collects_garbage_and_trims_the_heap_on_linux(monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.batch.sys.platform", "linux")
    monkeypatch.setattr("lyricvideo.batch.gc.collect", lambda: calls.append("gc.collect"))

    class _FakeLibc:
        def malloc_trim(self, pad):
            calls.append(("malloc_trim", pad))

    monkeypatch.setattr(
        "lyricvideo.batch.ctypes.CDLL", lambda name: calls.append(("CDLL", name)) or _FakeLibc(),
    )

    release_memory()

    assert calls == ["gc.collect", ("CDLL", "libc.so.6"), ("malloc_trim", 0)]


def test_release_memory_skips_malloc_trim_on_non_linux(monkeypatch):
    calls = []
    monkeypatch.setattr("lyricvideo.batch.sys.platform", "win32")
    monkeypatch.setattr("lyricvideo.batch.gc.collect", lambda: calls.append("gc.collect"))

    def _must_not_run(name):
        raise AssertionError("must not run on non-Linux")

    monkeypatch.setattr("lyricvideo.batch.ctypes.CDLL", _must_not_run)

    release_memory()

    assert calls == ["gc.collect"]


def test_release_memory_swallows_a_missing_libc(monkeypatch):
    """A minimal/musl-based Linux (rare, but not this app's own dev/prod
    environment) might not expose libc.so.6 the same way -- this is a
    best-effort memory-hygiene step, never something that should crash a
    batch run over."""
    monkeypatch.setattr("lyricvideo.batch.sys.platform", "linux")
    monkeypatch.setattr("lyricvideo.batch.gc.collect", lambda: None)

    def _raise(name):
        raise OSError("no such library")

    monkeypatch.setattr("lyricvideo.batch.ctypes.CDLL", _raise)

    release_memory()  # must not raise


def test_a_song_held_before_its_video_counts_as_already_processed_so_a_batch_does_not_redo_it(tmp_path, monkeypatch):
    from lyricvideo.pipeline import HELD_MARKER

    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"
    (work_root / "some-song").mkdir(parents=True)
    (work_root / "some-song" / HELD_MARKER).write_text("{}", encoding="utf-8")      # no video: held before it was made
    monkeypatch.setattr("lyricvideo.batch.extract_metadata", lambda path: type("Info", (), {"title": "Some Song"})())

    items = resolve_batch_items([audio], work_root)

    assert items[0].already_done is True


def test_a_song_the_owner_removed_from_review_counts_as_already_processed_so_a_batch_leaves_it_alone(tmp_path, monkeypatch):
    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"
    (work_root / "some-song").mkdir(parents=True)                         # no video, no hold marker: only the owner's removal
    monkeypatch.setattr("lyricvideo.batch.extract_metadata", lambda path: type("Info", (), {"title": "Some Song"})())
    monkeypatch.setattr("lyricvideo.batch.load_dismissed", lambda list_name: {"some-song"} if list_name == "flagged" else set())

    assert resolve_batch_items([audio], work_root)[0].already_done is True
