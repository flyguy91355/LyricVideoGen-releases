"""One-time (re-runnable) catch-up: copy every image the program already bought into the shared image library
(spec 2026-09-25). Existing images carry no prompt -- those were never saved -- so each is fingerprinted by its
picture; its source lyric line is recovered by re-hashing every lyric line and instrumental caption of every
song on disk (an image from an older lyric version simply has no text)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .clip_embedder import Embedder
from .image_library import ImageLibrary, content_id
from .imagery import is_fallback_image
from .layout import instrumental_caption
from .models import line_hash, load_song


@dataclass
class ImportSummary:
    seen: int = 0
    to_add: int = 0
    added: int = 0
    skipped_duplicate: int = 0
    skipped_placeholder: int = 0
    failed: int = 0
    text_recovered: int = 0


def find_image_files(work_dir: Path) -> list[Path]:
    """Each song's own images/ and images_backup_*/ only -- a nested EASY CHORD variant's images/ is a copy."""
    return sorted(work_dir.glob("*/images/*.png")) + sorted(work_dir.glob("*/images_backup_*/*.png"))


def collect_source_texts(work_dir: Path) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """(line_hash -> (text, song title)) for every lyric line and instrumental caption on disk, and
    (song folder name -> song title)."""
    texts: dict[str, tuple[str, str]] = {}
    titles: dict[str, str] = {}
    for timed in sorted(work_dir.glob("*/lyrics_timed.json")):
        try:
            song = load_song(timed)
        except Exception as e:
            print(f"WARNING: could not read {timed}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        titles[timed.parent.name] = song.title
        for line in song.lines:
            texts.setdefault(line_hash(line.text), (line.text, song.title))
        labels = sorted({event.label for event in song.chord_track.events})
        for caption in [instrumental_caption(None)] + [instrumental_caption(label) for label in labels]:
            texts.setdefault(line_hash(caption), (caption, song.title))
    return texts, titles


def import_images(
    library: ImageLibrary, embedder: Embedder, work_dir: Path, *, limit: int | None = None,
    dry_run: bool = False, batch_size: int = 32, progress: Callable[[str], None] = print,
) -> ImportSummary:
    texts, titles = collect_source_texts(work_dir)
    summary = ImportSummary()
    pending: list[tuple[Path, str, str, str]] = []      # (path, id, source text, song title)
    queued: set[str] = set()
    for path in find_image_files(work_dir):
        summary.seen += 1
        try:
            image_id = content_id(path)
        except OSError:
            summary.failed += 1
            continue
        if image_id in queued or library.has(image_id):
            summary.skipped_duplicate += 1
            continue
        if is_fallback_image(path):
            summary.skipped_placeholder += 1
            continue
        text = texts.get(path.stem, ("", ""))[0]
        pending.append((path, image_id, text, titles.get(path.parent.parent.name, "")))
        queued.add(image_id)
    if limit is not None:
        pending = pending[:limit]
    summary.to_add = len(pending)
    if dry_run:
        return summary

    for start in range(0, len(pending), batch_size):
        chunk = pending[start:start + batch_size]
        try:
            vectors = embedder.embed_images([item[0] for item in chunk])
            pairs = list(zip(chunk, vectors))
        except Exception:
            pairs = []                      # one bad file must not sink its whole batch -- retry them one by one
            for item in chunk:
                try:
                    pairs.append((item, embedder.embed_images([item[0]])[0]))
                except Exception as e:
                    summary.failed += 1
                    progress(f"  skipped an unreadable image {item[0]} ({type(e).__name__})")
        for (path, _image_id, text, title), vector in pairs:
            library.add(path, vector, prompt="", source_text=text, song_title=title)
            summary.added += 1
            if text:
                summary.text_recovered += 1
        progress(f"  {min(start + batch_size, len(pending))}/{len(pending)} processed")
    return summary
