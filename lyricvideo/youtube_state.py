"""Per-song YouTube upload tracking -- lets the app know which of its own
uploads exist, so Redo can skip auto-re-uploading a song it already posted.
See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

log = logging.getLogger("playalongvideoproduction")

STATE_FILENAME = "youtube_state.json"
EASY_CHORD_SUBDIR = "easychords"


# --- small, shared state-file plumbing (issue #7 review, F141/F143/F144) -----------------------------------------------
# Every YouTube state file used to be rewritten in place with Path.write_text, which truncates first: a reader landing in
# that moment (another thread, the backfill script) saw an empty file, treated it as "nothing saved", and its next save
# wiped the real contents. Writes now go to a temp file in the same folder and os.replace() it over the old one, so a
# reader only ever sees the old file or the new one.

def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Writes `text` to `path` via a same-folder temp file + os.replace. `mode` (e.g. 0o600) is applied to the temp file
    as it is created, so the finished file never exists with looser permissions (POSIX; ignored on Windows)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None and os.name == "posix":
            os.chmod(tmp, mode)                    # umask can only remove bits; make it exactly `mode`
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another handle has open for a moment (a reader in another
                # process); wait briefly and try again rather than failing the save.
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def quarantine_corrupt_file(path: Path) -> Path | None:
    """Moves an unreadable state file aside to `<name>.corrupt-<timestamp>` (so its bytes survive for recovery and the
    next save cannot silently overwrite them) and warns on stderr. Returns the new path, or None if it could not move."""
    path = Path(path)
    target = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d_%H%M%S_%f}")
    try:
        os.replace(path, target)
    except OSError as exc:
        print(f"WARNING: {path} is unreadable and could not be moved aside ({exc}).", file=sys.stderr)
        return None
    print(f"WARNING: {path} was unreadable; kept its contents as {target.name} and started it fresh.", file=sys.stderr)
    return target


def read_json_state(path: Path, default, expected_type: type):
    """The parsed JSON at `path`: `default` when the file is missing; when it exists but is not valid JSON of
    `expected_type`, the file is quarantined (quarantine_corrupt_file) and `default` returned. A transient read error
    (OSError) returns `default` without touching the file."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return default
    try:
        data = json.loads(text)
    except ValueError:
        quarantine_corrupt_file(path)
        return default
    if not isinstance(data, expected_type):
        quarantine_corrupt_file(path)
        return default
    return data


@dataclass(frozen=True)
class YoutubeState:
    video_id: str
    uploaded_at: str
    title: str
    engagement_comment_posted: bool = False


def load_youtube_state(work_dir: Path) -> YoutubeState | None:
    path = work_dir / STATE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return YoutubeState(**data)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Could not read YouTube state at %s: %s", path, exc)
        return None


def save_youtube_state(work_dir: Path, state: YoutubeState) -> None:
    atomic_write_text(Path(work_dir) / STATE_FILENAME, json.dumps(asdict(state)))


def song_dirs(work_root: Path) -> list[tuple[str, Path]]:
    """Every folder under work_root that can hold its own video: each song folder, plus its nested `easychords/` EASY
    CHORD version -- ("<song>" | "<song>/easychords", path) pairs sorted by name. The same walk as
    pipeline._candidate_song_dirs, kept here (a light module) so YouTube code and scripts need not import pipeline.py."""
    work_root = Path(work_root)
    if not work_root.is_dir():
        return []
    from .banned import is_banned
    pairs: list[tuple[str, Path]] = []
    for entry in work_root.iterdir():
        if not entry.is_dir() or is_banned(entry):       # a banned song (and its EASY version) is left out of everything
            continue
        pairs.append((entry.name, entry))
        nested = entry / EASY_CHORD_SUBDIR
        if nested.is_dir():
            pairs.append((f"{entry.name}/{EASY_CHORD_SUBDIR}", nested))
    return sorted(pairs, key=lambda pair: pair[0])


def uploaded_song_dirs(work_root: Path) -> list[tuple[str, Path, YoutubeState]]:
    """(slug, folder, state) for every video this app uploaded, EASY CHORD versions included (issue #7 review, F087: the
    organization backfill, the comment scan, the engagement-comment marker and the replace report all walked only the
    top level of work/ and never saw an uploaded `<song>/easychords` video)."""
    found = []
    for slug, path in song_dirs(work_root):
        if not (path / STATE_FILENAME).exists():
            continue
        state = load_youtube_state(path)
        if state is not None and state.video_id:
            found.append((slug, path, state))
    return found


def find_uploaded_song_dir(work_root: Path, video_id: str) -> Path | None:
    """The folder whose youtube_state.json holds `video_id` (an EASY CHORD version's nested folder included), or None."""
    for _slug, path, state in uploaded_song_dirs(work_root):
        if state.video_id == video_id:
            return path
    return None


def mark_engagement_comment_posted(work_root: Path, video_id: str) -> bool:
    """Records that `video_id`'s engagement comment is done with (posted -- or, if the GUI chooses, dismissed) so
    organize_video never drafts another. Finds nested EASY CHORD folders too. False when no folder holds that video."""
    path = find_uploaded_song_dir(work_root, video_id)
    if path is None:
        return False
    state = load_youtube_state(path)
    if state is None:
        return False
    if not state.engagement_comment_posted:
        save_youtube_state(path, replace(state, engagement_comment_posted=True))
    return True
