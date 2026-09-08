"""Tests for lyricvideo/update/apply.py -- path allow/deny logic for what
an applied update is permitted to touch, and requirements.txt change
detection."""

import tarfile
from pathlib import Path

from lyricvideo.update.apply import (
    is_path_updatable,
    requirements_changed,
    extract_release_archive,
    copy_updatable_files,
)


def test_lyricvideo_package_files_are_updatable():
    assert is_path_updatable("lyricvideo/gui.py") is True


def test_tests_files_are_updatable():
    assert is_path_updatable("tests/test_gui.py") is True


def test_docs_files_are_updatable():
    assert is_path_updatable("docs/CLAUDE_HISTORY.md") is True


def test_requirements_txt_is_updatable():
    assert is_path_updatable("requirements.txt") is True


def test_claude_md_is_updatable():
    assert is_path_updatable("CLAUDE.md") is True


def test_top_level_py_file_is_updatable():
    assert is_path_updatable("start.py") is True


def test_top_level_sh_file_is_updatable():
    assert is_path_updatable("run_lyricvideogen.sh") is True


def test_env_file_is_never_updatable():
    assert is_path_updatable(".env") is False


def test_songs_directory_is_never_updatable():
    assert is_path_updatable("songs/wish-you-were-here.pdf") is False


def test_work_directory_is_never_updatable():
    assert is_path_updatable("work/some-song/final.mp4") is False


def test_venv_directory_is_never_updatable():
    assert is_path_updatable(".venv/bin/python") is False


def test_random_top_level_file_is_not_updatable():
    assert is_path_updatable("README.md") is False


def test_env_variant_is_denied_anywhere_in_the_tree():
    assert is_path_updatable("lyricvideo/.env.local") is False


def test_traversal_path_is_never_updatable():
    assert is_path_updatable("../outside.py") is False
    assert is_path_updatable("lyricvideo/../../outside.py") is False


def test_absolute_path_is_never_updatable():
    assert is_path_updatable("/etc/passwd") is False


def test_requirements_changed_true_when_different():
    assert requirements_changed("httpx>=0.27\n", "httpx>=0.28\n") is True


def test_requirements_changed_false_when_identical():
    content = "httpx>=0.27\nanthropic>=0.34\n"
    assert requirements_changed(content, content) is False


def test_requirements_changed_ignores_trailing_whitespace_differences():
    assert requirements_changed("httpx>=0.27\n", "httpx>=0.27") is False


def _make_fake_release_tarball(tmp_path):
    source_root = tmp_path / "flyguy91355-LyricVideoGen-abc1234"
    (source_root / "lyricvideo").mkdir(parents=True)
    (source_root / "songs").mkdir()
    (source_root / "lyricvideo" / "gui.py").write_text("# new gui.py\n", encoding="utf-8")
    (source_root / "requirements.txt").write_text("httpx>=0.28\n", encoding="utf-8")
    (source_root / "songs" / "sneaky.pdf").write_text("should never be copied", encoding="utf-8")
    (source_root / "README.md").write_text("not updatable\n", encoding="utf-8")

    archive_path = tmp_path / "release.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_root, arcname=source_root.name)
    return archive_path


def test_extract_release_archive_returns_top_level_dir(tmp_path):
    archive_path = _make_fake_release_tarball(tmp_path)
    dest_dir = tmp_path / "extracted"
    dest_dir.mkdir()

    extracted_root = extract_release_archive(str(archive_path), str(dest_dir))

    assert (Path(extracted_root) / "lyricvideo" / "gui.py").exists()


def test_copy_updatable_files_only_copies_allowed_paths(tmp_path):
    archive_path = _make_fake_release_tarball(tmp_path)
    dest_dir = tmp_path / "extracted"
    dest_dir.mkdir()
    extracted_root = extract_release_archive(str(archive_path), str(dest_dir))

    target_dir = tmp_path / "live_install"
    target_dir.mkdir()

    copied = copy_updatable_files(extracted_root, str(target_dir))

    assert sorted(copied) == ["lyricvideo/gui.py", "requirements.txt"]
    assert (target_dir / "lyricvideo" / "gui.py").read_text(encoding="utf-8") == "# new gui.py\n"
    assert not (target_dir / "songs").exists()
    assert not (target_dir / "README.md").exists()


def test_copy_updatable_files_never_overwrites_denied_paths_even_if_present(tmp_path):
    source_root = tmp_path / "release_source"
    (source_root / "songs").mkdir(parents=True)
    (source_root / "songs" / "sneaky.pdf").write_text("should never be copied", encoding="utf-8")
    (source_root / "lyricvideo").mkdir()
    (source_root / "lyricvideo" / "gui.py").write_text("# new gui.py\n", encoding="utf-8")

    target_dir = tmp_path / "live_install"
    (target_dir / "songs").mkdir(parents=True)
    (target_dir / "songs" / "real_song.pdf").write_text(
        "owner's real song -- must survive", encoding="utf-8"
    )

    copy_updatable_files(str(source_root), str(target_dir))

    assert (target_dir / "songs" / "real_song.pdf").read_text(encoding="utf-8") == (
        "owner's real song -- must survive"
    )
    assert not (target_dir / "songs" / "sneaky.pdf").exists()


def test_symlinked_destination_is_never_overwritten(tmp_path):
    target_dir = tmp_path / "live_install"
    (target_dir / "lyricvideo").mkdir(parents=True)
    real_env = target_dir / ".env"
    real_env.write_text("SECRET=1", encoding="utf-8")
    symlink_path = target_dir / "lyricvideo" / "gui.py"
    symlink_path.symlink_to(real_env)

    source_root = tmp_path / "release_source"
    (source_root / "lyricvideo").mkdir(parents=True)
    (source_root / "lyricvideo" / "gui.py").write_text(
        "# malicious overwrite attempt\n", encoding="utf-8"
    )

    copy_updatable_files(str(source_root), str(target_dir))

    assert real_env.read_text(encoding="utf-8") == "SECRET=1"
