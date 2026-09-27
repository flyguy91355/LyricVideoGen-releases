"""Apply Update ships what the program needs (deep_review/ and scripts/ too) and removes what a newer release
no longer ships (a manifest cut_release.sh writes), without ever touching a denied path, a symlink, or a file
the previous release did not ship."""

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lyricvideo.update.apply import (
    APPLIED_MANIFEST_FILE,
    RELEASE_MANIFEST_FILE,
    copy_updatable_files,
    is_path_updatable,
    read_applied_manifest,
    read_release_manifest,
    remove_stale_files,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CUT_RELEASE = REPO_ROOT / "scripts" / "cut_release.sh"


def _write(root: Path, relative: str, text: str = "x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _release(tmp_path: Path, name: str, files: dict[str, str], manifest: bool = True) -> Path:
    root = tmp_path / name
    for relative, text in files.items():
        _write(root, relative, text)
    if manifest:
        (root / RELEASE_MANIFEST_FILE).write_text("\n".join(sorted(files)) + "\n", encoding="utf-8")
    return root


# --- F149: what a release ships ----------------------------------------------------------------------

def test_deep_review_and_scripts_are_updatable_but_denied_paths_still_win():
    assert is_path_updatable("deep_review/runner.py") is True
    assert is_path_updatable("scripts/settle_keys.py") is True
    assert is_path_updatable("scripts/import_image_library.py") is True
    assert is_path_updatable("scripts/.env") is False
    assert is_path_updatable("work/deep_review/x.py") is False
    assert is_path_updatable(".venv/scripts/x.py") is False


def _shipped_pathspecs() -> list[str]:
    """The `git ls-files -- <paths>` list cut_release.sh publishes (continuation lines joined)."""
    text = CUT_RELEASE.read_text(encoding="utf-8").replace("\\\n", " ")
    match = re.search(r"git ls-files --(.*?)>", text)
    assert match, "cut_release.sh no longer lists its shipped paths with `git ls-files -- ... >`"
    return match.group(1).split()


def test_every_path_cut_release_ships_is_one_apply_update_will_copy():
    specs = _shipped_pathspecs()
    assert "deep_review/" in specs and "scripts/" in specs
    for spec in specs:
        sample = spec + "some_file.py" if spec.endswith("/") else spec
        assert is_path_updatable(sample), f"cut_release.sh ships {spec} but apply.py would never copy it"


def test_every_tracked_file_a_release_ships_passes_the_allow_list():
    if not (REPO_ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    listed = subprocess.run(
        ["git", "ls-files", "--", *_shipped_pathspecs()], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert listed
    refused = [path for path in listed if not is_path_updatable(path)]
    assert refused == []


def test_every_local_package_a_shipped_test_imports_is_itself_shipped():
    """tests/deep_review/ once shipped without deep_review/, so an updated install could not collect it."""
    specs = _shipped_pathspecs()
    local_packages = {p.name for p in REPO_ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    imported = set()
    for test_file in (REPO_ROOT / "tests").rglob("*.py"):
        for node in ast.walk(ast.parse(test_file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
    for package in sorted((imported & local_packages) - {"tests"}):
        assert f"{package}/" in specs, f"tests import {package}, but cut_release.sh does not ship {package}/"


def test_cut_release_writes_the_release_manifest():
    assert f'"$CLONE_DIR/{RELEASE_MANIFEST_FILE}"' in CUT_RELEASE.read_text(encoding="utf-8")


def test_the_manifests_themselves_are_never_copied_into_an_install():
    assert is_path_updatable(RELEASE_MANIFEST_FILE) is False
    assert is_path_updatable(APPLIED_MANIFEST_FILE) is False


# --- F140: removing what a newer release no longer ships ------------------------------------------------

def test_a_file_the_previous_release_shipped_and_the_new_one_does_not_is_removed(tmp_path):
    install = tmp_path / "install"
    old = _release(tmp_path, "v1", {"lyricvideo/keep.py": "1", "lyricvideo/old.py": "1", "tests/test_old.py": "1"})
    copy_updatable_files(str(old), str(install))
    assert (install / "lyricvideo" / "old.py").exists()

    new = _release(tmp_path, "v2", {"lyricvideo/keep.py": "2"})
    copied = copy_updatable_files(str(new), str(install))

    assert copied == ["lyricvideo/keep.py"]
    assert (install / "lyricvideo" / "keep.py").read_text(encoding="utf-8") == "2"
    assert not (install / "lyricvideo" / "old.py").exists()
    assert not (install / "tests" / "test_old.py").exists()
    assert not (install / "tests").exists()                       # left empty: removed too
    assert read_applied_manifest(str(install)) == ["lyricvideo/keep.py"]


def test_a_removed_modules_compiled_copy_goes_too(tmp_path):
    install = tmp_path / "install"
    copy_updatable_files(str(_release(tmp_path, "v1", {"deep_review/gone.py": "1", "deep_review/stay.py": "1"})), str(install))
    _write(install, "deep_review/__pycache__/gone.cpython-311.pyc")
    _write(install, "deep_review/__pycache__/stay.cpython-311.pyc")

    copy_updatable_files(str(_release(tmp_path, "v2", {"deep_review/stay.py": "2"})), str(install))

    assert not (install / "deep_review" / "gone.py").exists()
    assert not (install / "deep_review" / "__pycache__" / "gone.cpython-311.pyc").exists()
    assert (install / "deep_review" / "__pycache__" / "stay.cpython-311.pyc").exists()


def test_the_owners_own_file_that_no_release_shipped_is_never_removed(tmp_path):
    install = tmp_path / "install"
    copy_updatable_files(str(_release(tmp_path, "v1", {"lyricvideo/keep.py": "1"})), str(install))
    _write(install, "lyricvideo/mine.py", "the owner's own")

    copy_updatable_files(str(_release(tmp_path, "v2", {"lyricvideo/keep.py": "2"})), str(install))

    assert (install / "lyricvideo" / "mine.py").read_text(encoding="utf-8") == "the owner's own"


def test_a_denied_path_listed_in_the_previous_manifest_is_never_removed(tmp_path):
    install = tmp_path / "install"
    for relative in (".env", "work/song/lyrics_timed.json", "songs/a.mp3", ".venv/bin/python", "README.md"):
        _write(install, relative, "precious")

    removed = remove_stale_files(
        str(install), [".env", "work/song/lyrics_timed.json", "songs/a.mp3", ".venv/bin/python", "README.md"], [],
    )

    assert removed == []
    for relative in (".env", "work/song/lyrics_timed.json", "songs/a.mp3", ".venv/bin/python", "README.md"):
        assert (install / relative).read_text(encoding="utf-8") == "precious"


def test_a_stale_path_that_is_a_symlink_is_neither_followed_nor_removed(tmp_path):
    install = tmp_path / "install"
    secret = _write(install, ".env", "SECRET=1")
    link = install / "lyricvideo" / "old.py"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/user cannot create symlinks")

    removed = remove_stale_files(str(install), ["lyricvideo/old.py"], [])

    assert removed == []
    assert link.is_symlink()
    assert secret.read_text(encoding="utf-8") == "SECRET=1"


def test_with_no_earlier_manifest_nothing_is_removed_but_this_one_is_recorded(tmp_path):
    install = tmp_path / "install"
    _write(install, "lyricvideo/old.py", "from an older install")

    copy_updatable_files(str(_release(tmp_path, "v2", {"lyricvideo/keep.py": "2"})), str(install))

    assert (install / "lyricvideo" / "old.py").exists()
    assert read_applied_manifest(str(install)) == ["lyricvideo/keep.py"]


def test_a_release_without_a_manifest_removes_nothing_and_keeps_the_earlier_record(tmp_path):
    install = tmp_path / "install"
    copy_updatable_files(str(_release(tmp_path, "v1", {"lyricvideo/keep.py": "1", "lyricvideo/old.py": "1"})), str(install))

    new = _release(tmp_path, "v2", {"lyricvideo/keep.py": "2"}, manifest=False)
    copy_updatable_files(str(new), str(install))

    assert read_release_manifest(str(new)) is None
    assert (install / "lyricvideo" / "old.py").exists()
    assert read_applied_manifest(str(install)) == ["lyricvideo/keep.py", "lyricvideo/old.py"]


def test_remove_stale_can_be_turned_off(tmp_path):
    install = tmp_path / "install"
    copy_updatable_files(str(_release(tmp_path, "v1", {"lyricvideo/keep.py": "1", "lyricvideo/old.py": "1"})), str(install))

    copy_updatable_files(str(_release(tmp_path, "v2", {"lyricvideo/keep.py": "2"})), str(install), remove_stale=False)

    assert (install / "lyricvideo" / "old.py").exists()
