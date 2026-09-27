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
import subprocess
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
    "deep_review/",     # its tests ship under tests/deep_review/, which cannot even be collected without it
    "scripts/",         # the tools CLAUDE.md and the app's own log messages tell the owner to run
    "tests/",
    "docs/",
    "requirements.txt",
    "CLAUDE.md",
)

# Written by scripts/cut_release.sh at the release root: every path that release ships, one per line. Like
# RELEASE_SOURCE_COMMIT it is not in the allow-list, so it is never itself copied into the install.
RELEASE_MANIFEST_FILE = "RELEASE_MANIFEST"
# The install's own record of what the last applied release shipped (also outside the allow-list), so the next
# apply can remove what a newer release no longer ships.
APPLIED_MANIFEST_FILE = ".release_manifest"


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
    fallback rule for a bare top-level *.py, *.sh or *.bat file (the
    Linux and Windows launcher scripts)."""
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

    if "/" not in normalized and normalized.endswith((".py", ".sh", ".bat")):
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


def read_release_source_commit(extracted_root: str) -> str | None:
    """The git commit (in this program's own repo, not the releases repo)
    that cut_release.sh built this release from, read from the
    RELEASE_SOURCE_COMMIT marker file cut_release.sh writes alongside the
    synced code -- never part of ALLOWED_PATH_PREFIXES, so it's never
    itself copied into the install. Returns None for a release cut before
    this marker existed, which simply disables the staleness check below
    for that release rather than blocking it."""
    marker = Path(extracted_root) / "RELEASE_SOURCE_COMMIT"
    if not marker.exists():
        return None
    content = marker.read_text(encoding="utf-8").strip()
    return content or None


def is_source_commit_already_applied(source_commit: str | None, repo_root: str) -> bool:
    """True iff source_commit is already part of this checkout's own git
    history (as HEAD or an ancestor of it) -- applying a release built
    from a commit this checkout already contains (or has moved past)
    would silently revert any local commits made after that point back to
    the release's older snapshot. Real incident, 2026-09-11: release
    v1.8.2 was cut from commit 4917284; a further local commit (920cbb2)
    landed ~41 minutes later; Apply Update then overwrote CLAUDE.md/docs
    with the older v1.8.2 content, discarding 920cbb2's documentation
    changes in the working tree (caught and fixed by hand the next day,
    not by this check, which didn't exist yet). Returns False -- never
    blocks -- when there's no recorded source_commit (older releases),
    repo_root isn't a git checkout, or git can't resolve the ancestry
    (e.g. the commit is unknown here); only a *confirmed* ancestor
    relationship blocks the apply, since refusing to update over an
    inconclusive check would be worse than the bug this guards against."""
    if not source_commit:
        return False
    if not (Path(repo_root) / ".git").exists():
        return False
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_commit, "HEAD"],
            cwd=repo_root,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _read_manifest(path: Path) -> list[str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return [line.strip().replace("\\", "/") for line in text.splitlines() if line.strip()]


def read_release_manifest(extracted_root: str) -> list[str] | None:
    """Every path the extracted release ships (cut_release.sh's RELEASE_MANIFEST), or None for a release cut
    before the manifest existed -- which simply means nothing is removed on that apply."""
    return _read_manifest(Path(extracted_root) / RELEASE_MANIFEST_FILE)


def read_applied_manifest(target_dir: str) -> list[str] | None:
    """What the release last applied to this install shipped, or None when no manifest was ever recorded here."""
    return _read_manifest(Path(target_dir) / APPLIED_MANIFEST_FILE)


def write_applied_manifest(target_dir: str, paths: list[str]) -> None:
    (Path(target_dir) / APPLIED_MANIFEST_FILE).write_text("\n".join(paths) + "\n", encoding="utf-8")


def remove_stale_files(target_dir: str, previous_manifest: list[str], new_manifest: list[str]) -> list[str]:
    """Deletes each file the PREVIOUS applied release shipped that the new one no longer does (a module or test
    deleted upstream would otherwise stay importable and collected by pytest forever). Only ever a regular file on
    an allow-listed path that is not a symlink and does not resolve outside the allow-list -- never a denied path,
    and never anything the previous release did not ship (the owner's own untracked files are safe). Folders
    this leaves empty are removed too. Returns the sorted relative paths removed."""
    target_path = Path(target_dir)
    keep = {p.replace("\\", "/") for p in new_manifest}
    removed: list[str] = []
    for relative in sorted({p.replace("\\", "/") for p in previous_manifest} - keep):
        if not is_path_updatable(relative):
            continue
        destination = _safe_destination(target_path, relative)
        if destination is None or destination.is_symlink() or not destination.is_file():
            continue
        try:
            destination.unlink()
        except OSError as exc:
            logger.warning("Update: could not remove %s, which this release no longer ships: %s", relative, exc)
            continue
        removed.append(relative)
        if destination.suffix == ".py":
            # Its compiled copy too: a folder left holding only __pycache__/ would still import as a namespace package.
            cache_dir = destination.parent / "__pycache__"
            for compiled in cache_dir.glob(f"{destination.stem}.*.pyc") if cache_dir.is_dir() else []:
                try:
                    compiled.unlink()
                except OSError:
                    pass
            try:
                cache_dir.rmdir()
            except OSError:
                pass
        parent = destination.parent
        while parent != target_path and target_path in parent.parents:
            try:
                parent.rmdir()          # only succeeds when empty
            except OSError:
                break
            parent = parent.parent
    return removed


def copy_updatable_files(source_dir: str, target_dir: str, remove_stale: bool = True) -> list[str]:
    """Walks source_dir, copies every file whose path (relative to
    source_dir) passes is_path_updatable() into the same relative path
    under target_dir, creating parent directories as needed. Returns the
    sorted list of relative paths actually copied. Never touches anything
    outside that allow-list, even if the source tree contains a denied
    path -- the deny check in is_path_updatable() is authoritative
    regardless of what's on disk in target_dir already.

    With `remove_stale` (the default) and a release that carries a
    RELEASE_MANIFEST, files the previously applied release shipped but this
    one no longer does are then removed (remove_stale_files), and this
    release's manifest is recorded for the next apply. The first apply that
    has a manifest removes nothing -- there is no earlier record to compare."""
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

    new_manifest = read_release_manifest(source_dir) if remove_stale else None
    if new_manifest is not None:
        previous_manifest = read_applied_manifest(target_dir)
        if previous_manifest is not None:
            removed = remove_stale_files(target_dir, previous_manifest, new_manifest)
            if removed:
                logger.info("Update: removed %d file(s) this release no longer ships: %s", len(removed), ", ".join(removed))
        try:
            write_applied_manifest(target_dir, new_manifest)
        except OSError as exc:
            logger.warning("Update: could not record this release's file list: %s", exc)

    return sorted(copied)
