"""Tests for lyricvideo/update/apply.py -- path allow/deny logic for what
an applied update is permitted to touch, and requirements.txt change
detection."""

import subprocess
import tarfile

import pytest
from pathlib import Path

from lyricvideo.update.apply import (
    is_path_updatable,
    is_source_commit_already_applied,
    read_release_source_commit,
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
    assert is_path_updatable("run_playalongvideoproduction.bat") is True
    assert is_path_updatable("scripts/tool.bat") is False


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
    try:
        symlink_path.symlink_to(real_env)
    except OSError as e:  # Windows without Developer Mode / admin can't create symlinks
        pytest.skip(f"symlinks not creatable here: {e}")

    source_root = tmp_path / "release_source"
    (source_root / "lyricvideo").mkdir(parents=True)
    (source_root / "lyricvideo" / "gui.py").write_text(
        "# malicious overwrite attempt\n", encoding="utf-8"
    )

    copy_updatable_files(str(source_root), str(target_dir))

    assert real_env.read_text(encoding="utf-8") == "SECRET=1"


def test_read_release_source_commit_returns_marker_contents(tmp_path):
    (tmp_path / "RELEASE_SOURCE_COMMIT").write_text("abc123\n", encoding="utf-8")
    assert read_release_source_commit(str(tmp_path)) == "abc123"


def test_read_release_source_commit_returns_none_when_marker_missing(tmp_path):
    assert read_release_source_commit(str(tmp_path)) is None


def _init_repo_with_commits(repo_root: Path, n: int) -> list[str]:
    """Creates a real git repo with n commits, returning each commit's sha
    oldest-first -- used to build a real ancestor relationship rather than
    faking git's own graph logic."""
    subprocess.run(["git", "init", "--quiet"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    shas = []
    for i in range(n):
        (repo_root / "file.txt").write_text(f"version {i}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", f"commit {i}"], cwd=repo_root, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        shas.append(sha)
    return shas


def test_is_source_commit_already_applied_true_for_ancestor(tmp_path):
    shas = _init_repo_with_commits(tmp_path, 3)
    assert is_source_commit_already_applied(shas[0], str(tmp_path)) is True
    assert is_source_commit_already_applied(shas[-1], str(tmp_path)) is True


def test_is_source_commit_already_applied_false_for_unknown_future_commit(tmp_path):
    _init_repo_with_commits(tmp_path, 1)
    assert is_source_commit_already_applied("0" * 40, str(tmp_path)) is False


def test_is_source_commit_already_applied_false_when_no_source_commit(tmp_path):
    _init_repo_with_commits(tmp_path, 1)
    assert is_source_commit_already_applied(None, str(tmp_path)) is False


def test_is_source_commit_already_applied_false_when_not_a_git_repo(tmp_path):
    assert is_source_commit_already_applied("abc123", str(tmp_path)) is False
