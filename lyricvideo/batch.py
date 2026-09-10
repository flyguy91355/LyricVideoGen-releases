"""Finds audio files in a folder and resolves each one's identity ahead of
actually running the pipeline on any of them -- backs the GUI's "Batch: Process
a Folder" feature. See
docs/superpowers/specs/2026-09-10-batch-folder-processing-design.md."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .identify import extract_metadata
from .pipeline import slugify

_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac"}


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
