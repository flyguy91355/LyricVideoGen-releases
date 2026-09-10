from pathlib import Path

from lyricvideo.batch import BatchItem, find_audio_files, resolve_batch_items, resolve_existing_folder


def test_resolve_existing_folder_returns_real_dir_unchanged(tmp_path):
    real = tmp_path / "songs"
    real.mkdir()

    assert resolve_existing_folder(real) == real


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
