"""Finds audio files in a folder and resolves each one's identity ahead of
actually running the pipeline on any of them -- backs the GUI's "Batch: Process
a Folder" feature. See
docs/superpowers/specs/2026-09-10-batch-folder-processing-design.md."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .identify import extract_metadata
from .pipeline import slugify

log = logging.getLogger("playalongvideoproduction")

_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac"}

# Separate from Settings (lyricvideo/settings.py) on purpose: Settings is
# owner-tunable render config, wholesale-replaced from the Settings panel's
# widgets on every change (SettingsPanel.collect()) -- a field with no widget
# behind it would get silently reset to its default the next time any slider
# moves. This is just remembered GUI convenience state, so it gets its own
# tiny file instead.
_STATE_FILE = Path.home() / ".playalongvideoproduction" / "batch_state.json"


def load_last_batch_folder(path: Path = _STATE_FILE) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("last_folder", "")
    except (OSError, ValueError):
        return ""


def save_last_batch_folder(folder: str, path: Path = _STATE_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_folder": folder}), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues
        log.warning("Could not save last batch folder: %s", exc)


def resolve_existing_folder(folder: Path) -> Path:
    """Returns `folder` unchanged if it exists. Otherwise, looks for a sibling
    in its parent whose name matches once leading/trailing whitespace is
    stripped from both sides -- confirmed live 2026-09-10: the OS folder-picker
    dialog silently drops a trailing space from a real folder named
    "batch music ", so the path handed to this app no longer matches the real
    directory on disk even though the user picked it correctly. If exactly one
    sibling matches, that real path is returned; otherwise `folder` is returned
    unchanged so the original FileNotFoundError still surfaces normally."""
    if folder.is_dir():
        return folder
    target = folder.name.strip()
    try:
        candidates = [p for p in folder.parent.iterdir() if p.is_dir() and p.name.strip() == target]
    except OSError:
        return folder
    if len(candidates) == 1:
        return candidates[0]
    return folder


def find_audio_files(folder: Path) -> list[Path]:
    """Immediate audio files in `folder` (no subfolder recursion), sorted
    alphabetically by name -- same extension filter as the single-file
    Browse dialog."""
    return sorted(
        (f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in _AUDIO_EXTENSIONS),
        key=lambda f: f.name,
    )


@dataclass(frozen=True)
class BatchItem:
    audio_path: Path
    title: str
    work_dir: Path
    already_done: bool


def resolve_batch_items(files: list[Path], work_root: Path) -> list[BatchItem]:
    """Resolves each file's title (free: tags/filename/lrclib/MusicBrainz, no
    Claude/Replicate spend -- same cost profile as the single-song title
    auto-fill), work directory, and whether it already has a finished video.
    A file whose metadata extraction itself raises still gets a usable,
    unique identity -- its own filename stem, never a shared constant --
    so two different failing files in the same batch never collide on the
    same work_dir. Order is preserved from `files`."""
    items: list[BatchItem] = []
    for audio_path in files:
        try:
            title = extract_metadata(audio_path).title
        except Exception:
            title = audio_path.stem
        work_dir = work_root / slugify(title)
        final_video = work_dir / f"{slugify(title)}.mp4"
        items.append(BatchItem(
            audio_path=audio_path, title=title, work_dir=work_dir,
            already_done=final_video.exists(),
        ))
    return items
