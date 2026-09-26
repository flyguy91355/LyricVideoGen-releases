from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from lyricvideo.image_library import (
    EMBEDDING_DIM, LIBRARY_ENV_VAR, ImageLibrary, content_id, image_library_dir, main,
)


def _vec(*head: float) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[: len(head)] = head
    return v


def _png(path: Path, color) -> Path:
    Image.new("RGB", (8, 8), color).save(path)
    return path


def test_image_library_dir_honours_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(LIBRARY_ENV_VAR, str(tmp_path / "custom"))
    assert image_library_dir() == tmp_path / "custom"


def test_image_library_dir_defaults_to_a_folder_in_the_home_directory(monkeypatch):
    monkeypatch.delenv(LIBRARY_ENV_VAR, raising=False)
    assert image_library_dir() == Path.home() / "PlayAlongVideoProductionImages"


def test_image_library_creates_its_folder(tmp_path):
    root = tmp_path / "does" / "not" / "exist"
    library = ImageLibrary(root)
    assert (root / "images").is_dir() and library.count() == 0


def test_add_copies_the_png_under_its_content_id_and_records_the_metadata(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    src = _png(tmp_path / "a.png", (255, 0, 0))

    image_id = library.add(src, _vec(1, 0), prompt="a red door", source_text="open the door", song_title="Door")

    assert image_id == content_id(src)
    assert library.image_path(image_id).read_bytes() == src.read_bytes()
    assert library.get(image_id) == {"prompt": "a red door", "source_text": "open the door", "song_title": "Door"}
    assert library.has(image_id) and library.count() == 1


def test_adding_the_same_picture_twice_keeps_one_entry(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    first = _png(tmp_path / "a.png", (1, 2, 3))
    second = tmp_path / "copy.png"
    second.write_bytes(first.read_bytes())

    assert library.add(first, _vec(1)) == library.add(second, _vec(1))
    assert library.count() == 1


def test_an_empty_library_never_matches(tmp_path):
    assert ImageLibrary(tmp_path / "lib").best_match(_vec(1), set(), 0.0) is None


def test_best_match_returns_the_closest_image_above_the_threshold(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    a = library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    library.add(_png(tmp_path / "b.png", (0, 255, 0)), _vec(0, 1))

    match = library.best_match(_vec(1, 0.1), set(), 0.9)

    assert match is not None and match.image_id == a
    assert match.score == pytest.approx(0.995, abs=1e-3)
    assert library.best_match(_vec(1, 0.1), set(), 0.999) is None


def test_best_match_skips_excluded_ids_and_falls_to_the_next_best(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    a = library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    b = library.add(_png(tmp_path / "b.png", (0, 255, 0)), _vec(1, 1))

    match = library.best_match(_vec(1, 0), {a}, 0.5)

    assert match is not None and match.image_id == b
    assert library.best_match(_vec(1, 0), {a}, 0.9) is None  # the runner-up is below the bar


def test_best_match_sees_images_added_after_it_first_ran(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    assert library.best_match(_vec(1), set(), 0.5) is None
    new_id = library.add(_png(tmp_path / "a.png", (9, 9, 9)), _vec(1))

    match = library.best_match(_vec(1), set(), 0.5)

    assert match is not None and match.image_id == new_id


def test_the_library_persists_across_handles(tmp_path):
    root = tmp_path / "lib"
    image_id = ImageLibrary(root).add(_png(tmp_path / "a.png", (5, 5, 5)), _vec(1), prompt="p")

    reopened = ImageLibrary(root)

    assert reopened.count() == 1
    assert reopened.best_match(_vec(1), set(), 0.5).image_id == image_id


def test_two_handles_can_add_to_the_same_library(tmp_path):
    root = tmp_path / "lib"
    one, two = ImageLibrary(root), ImageLibrary(root)
    one.add(_png(tmp_path / "a.png", (1, 1, 1)), _vec(1))
    two.add(_png(tmp_path / "b.png", (2, 2, 2)), _vec(0, 1))

    assert ImageLibrary(root).count() == 2


def test_copy_to_writes_identical_bytes_and_a_missing_file_raises(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    src = _png(tmp_path / "a.png", (7, 7, 7))
    image_id = library.add(src, _vec(1))
    dest = tmp_path / "song" / "x.png"
    dest.parent.mkdir()

    library.copy_to(image_id, dest)
    assert dest.read_bytes() == src.read_bytes()

    library.image_path(image_id).unlink()
    with pytest.raises(FileNotFoundError):
        library.copy_to(image_id, tmp_path / "song" / "y.png")


def test_add_rejects_a_wrong_sized_embedding(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    with pytest.raises(ValueError):
        library.add(_png(tmp_path / "a.png", (1, 1, 1)), np.ones(7, dtype=np.float32))


def test_stats_counts_images_prompts_and_reuses(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    a = library.add(_png(tmp_path / "a.png", (1, 1, 1)), _vec(1), prompt="has a prompt")
    library.add(_png(tmp_path / "b.png", (2, 2, 2)), _vec(0, 1))
    library.record_reuse(a, "some-song")

    assert library.stats() == {"images": 2, "with_prompt": 1, "reuses": 1}


def test_the_stats_command_reports_the_savings(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(LIBRARY_ENV_VAR, str(tmp_path / "lib"))
    library = ImageLibrary()
    image_id = library.add(_png(tmp_path / "a.png", (3, 3, 3)), _vec(1))
    for _ in range(10):
        library.record_reuse(image_id, "s")
    library.close()

    assert main(["stats"]) == 0

    out = capsys.readouterr().out
    assert "1 images" in out and "reused 10 times" in out and "$0.03" in out
