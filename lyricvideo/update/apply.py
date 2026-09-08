"""Applying a downloaded release to the program's own files: what's safe to
touch, and how the archive gets extracted and copied. Deny-list always
wins over allow-list -- this program's input songs, generated work files,
managed virtualenv, and credentials must never be touched by an applied
update. Ported from AITrading's src/update/apply.py; only the path lists
below are LyricVideoGen-specific, the traversal/symlink-safety logic is
unchanged. See
docs/superpowers/specs/2026-09-08-update-available-design.md."""

import logging
import ntpath
import shutil
import tarfile
from pathlib import Path

logger = logging.getLogger(__name__)

DENIED_PATH_PREFIXES = (
    ".env",
    "songs/",
    "work/",
    ".venv/",
)

# Deliberately looser than the boundary rule above: ".env.local" etc. are
# real credential-adjacent files, so any file whose basename starts with
# ".env" is denied outright no matter where it sits.
DENIED_FILENAME_PREFIXES = (".env",)

ALLOWED_PATH_PREFIXES = (
    "lyricvideo/",
    "tests/",
    "docs/",
    "requirements.txt",
    "CLAUDE.md",
)


def _matches_path_entry(normalized: str, entry: str) -> bool:
    """Directory-boundary-aware match: a trailing-slash entry ("songs/")
    matches anything inside that directory; a bare entry ("CLAUDE.md")
    matches that exact path, or something genuinely nested under it --
    never a longer sibling name such as "CLAUDE.md.bak"."""
    if entry.endswith("/"):
        return normalized.startswith(entry)
    return normalized == entry or normalized.startswith(entry + "/")


def _is_traversal_unsafe(normalized: str) -> bool:
    """True for any path that isn't a plain relative path inside the
    install root: empty, absolute (POSIX, Windows drive, or UNC), or
    containing a ".." component."""
    if not normalized:
        return True
    if normalized.startswith("/"):
        return True
    if ntpath.splitdrive(normalized)[0]:
        return True
    return any(part == ".." for part in normalized.split("/"))


def is_path_updatable(relative_path: str) -> bool:
    """True iff an update is allowed to overwrite this path (relative to
    the repo root). Checked in order: traversal/absolute-path rejection,
    then the deny-list (always wins), then the explicit allow-list, then a
    fallback rule for a bare top-level *.py or *.sh file (e.g.
    run_lyricvideogen.sh)."""
    normalized = relative_path.replace("\\", "/")

    if _is_traversal_unsafe(normalized):
        return False

    basename = normalized.rsplit("/", 1)[-1]
    for denied_name in DENIED_FILENAME_PREFIXES:
        if basename.startswith(denied_name):
            return False

    for denied in DENIED_PATH_PREFIXES:
        if _matches_path_entry(normalized, denied):
            return False

    for allowed in ALLOWED_PATH_PREFIXES:
        if _matches_path_entry(normalized, allowed):
            return True

    if "/" not in normalized and (normalized.endswith(".py") or normalized.endswith(".sh")):
        return True

    return False


def requirements_changed(old_content: str, new_content: str) -> bool:
    """True iff the two requirements.txt contents differ, ignoring
    leading/trailing whitespace (a trailing-newline-only diff shouldn't
    trigger a real pip install)."""
    return old_content.strip() != new_content.strip()


def extract_release_archive(archive_path: str, dest_dir: str) -> str:
    """Extracts a .tar.gz release archive into dest_dir and returns the
    path to its single top-level directory (GitHub's auto-generated
    release tarballs always have exactly one)."""
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(dest_dir, filter="data")
    dest_path = Path(dest_dir)
    top_level_dirs = [entry for entry in dest_path.iterdir() if entry.is_dir()]
    if len(top_level_dirs) != 1:
        raise ValueError(
            f"Expected exactly one top-level directory in the release archive, "
            f"found {len(top_level_dirs)}"
        )
    return str(top_level_dirs[0])


def _safe_destination(target_root: Path, relative: str) -> Path | None:
    """Where `relative` may actually be written under target_root, or None
    if writing there wouldn't land where the allow-list thinks it does.
    shutil.copy2 FOLLOWS a symlink at the destination and writes straight
    through it, so a symlink planted at an allow-listed path pointing at
    .env (or songs/, or work/) would overwrite that denied location while
    every string-level check still reported "allowed"."""
    destination = target_root / relative
    if destination.is_symlink():
        return None
    try:
        resolved_root = target_root.resolve()
        real_relative = destination.resolve().relative_to(resolved_root).as_posix()
    except (OSError, ValueError):
        return None
    if not is_path_updatable(real_relative):
        return None
    return destination


def copy_updatable_files(source_dir: str, target_dir: str) -> list[str]:
    """Walks source_dir, copies every file whose path (relative to
    source_dir) passes is_path_updatable() into the same relative path
    under target_dir, creating parent directories as needed. Returns the
    sorted list of relative paths actually copied. Never touches anything
    outside that allow-list, even if the source tree contains a denied
    path -- the deny check in is_path_updatable() is authoritative
    regardless of what's on disk in target_dir already."""
    source_path = Path(source_dir)
    target_path = Path(target_dir)
    copied: list[str] = []

    for file_path in source_path.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(source_path).as_posix()
        if not is_path_updatable(relative):
            continue
        destination = _safe_destination(target_path, relative)
        if destination is None:
            logger.warning(
                "Update: refusing to write %s -- the destination is a symlink or "
                "resolves outside the allow-listed install path",
                relative,
            )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, destination)
        copied.append(relative)

    return sorted(copied)
