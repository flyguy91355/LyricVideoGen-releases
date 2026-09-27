"""One-time (re-runnable) catch-up: copies every image the program has already bought (each song's work/<song>/
images/ and images_backup_*/) into the shared image library, fingerprinting each with a local CLIP model so a new
song can reuse a close-enough picture instead of buying one from Replicate. See
docs/superpowers/specs/2026-09-25-shared-image-library-design.md.

  .venv/bin/python scripts/import_image_library.py [--dry-run] [--limit N] [--work-dir DIR]

Safe to re-run (already-imported pictures are skipped) and interrupt-safe (each batch is committed). The FIRST run
downloads the CLIP weights (~605 MB) -- this script is the only place that is allowed to. --dry-run needs no model
and just reports what it would add. Images bought from now on are added to the library automatically by the images
stage; this is only the catch-up for what already exists."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.clip_embedder import ClipEmbedder, EmbedderUnavailable
from lyricvideo.image_library import ImageLibrary
from lyricvideo.image_library_import import import_images

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would be added; no model, no writes")
    parser.add_argument("--limit", type=int, default=None, help="add at most N images (a trial run)")
    parser.add_argument("--work-dir", type=Path, default=PROJECT_ROOT / "work")
    args = parser.parse_args(argv)

    library = ImageLibrary()
    print(f"Library: {library.root} ({library.count()} images already in it)")
    embedder = ClipEmbedder(allow_download=True)
    if not args.dry_run:
        try:
            embedder.check_available()      # downloads the ~605 MB weights the first time
        except EmbedderUnavailable as e:
            raise SystemExit(f"Cannot fingerprint images: {e}")

    summary = import_images(library, embedder, args.work_dir, limit=args.limit, dry_run=args.dry_run)

    print(
        f"\nSeen {summary.seen} image files: "
        f"{'would add' if args.dry_run else 'added'} {summary.to_add if args.dry_run else summary.added}, "
        f"already in library / duplicate picture {summary.skipped_duplicate}, "
        f"plain-colour placeholders {summary.skipped_placeholder}, unreadable {summary.failed}."
    )
    if not args.dry_run:
        print(f"Source lyric line recovered for {summary.text_recovered} of the {summary.added} added. "
              f"Library now holds {library.count()} images.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
