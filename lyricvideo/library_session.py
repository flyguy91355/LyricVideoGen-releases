"""Per-song glue between imagery.get_or_generate_image() and the shared image library (spec 2026-09-25).

One LibrarySession lives for one song's images stage. It embeds the Claude-written prompt, asks the library
for a close-enough picture the song hasn't used yet, copies a hit into the song's own images/ folder, and files
every newly bought picture (with its real prompt) into the library. It never raises: any failure disables the
library for the rest of the song and the caller simply buys as it always did."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .clip_embedder import ClipEmbedder, Embedder, EmbedderUnavailable
from .image_library import ImageLibrary, content_id
from .imagery import REPLICATE_PRICE_PER_IMAGE_USD

# Pictures the owner REPLACED with Redo's "Generate new images" (moved aside to images_prior_<timestamp>/): never
# offered to that song again by the library, on any later run (issue #7 review; spec 2026-09-25 "must not re-match
# the very pictures the owner asked to replace"). Remembered in this per-song file too, so deleting the
# images_prior_* folders to free disk space does not bring them back.
REJECTED_IMAGES_FILE = "image_library_rejected.json"


def rejected_image_ids(work_dir: Path) -> set[str]:
    """Content ids of every picture this song's owner replaced: the saved list plus whatever images_prior_*/ holds
    now (newly found ones are added to the saved list)."""
    work_dir = Path(work_dir)
    record = work_dir / REJECTED_IMAGES_FILE
    saved: set[str] = set()
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        if isinstance(data, list):
            saved = {str(item) for item in data}
    except (OSError, ValueError):
        pass
    found: set[str] = set()
    for prior in sorted(work_dir.glob("images_prior_*")):
        if not prior.is_dir():
            continue
        for png in prior.glob("*.png"):
            try:
                found.add(content_id(png))
            except OSError:
                continue
    if found - saved:
        saved |= found
        tmp = record.with_name(f".{record.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(sorted(saved), indent=1), encoding="utf-8")
            os.replace(tmp, record)
        except OSError as e:
            print(f"WARNING: could not save the replaced-pictures list ({e}); using it for this run only",
                  file=sys.stderr)
    return saved | found


class LibrarySession:
    def __init__(
        self, library: ImageLibrary, embedder: Embedder, song_slug: str, song_title: str,
        min_score: float, skip_lookup: bool = False,
    ):
        self.library = library
        self.embedder = embedder
        self.song_slug = song_slug
        self.song_title = song_title
        self.min_score = min_score
        self.skip_lookup = skip_lookup      # Redo with "Generate new images": never hand back the pictures being replaced
        self.enabled = True
        self.used_ids: set[str] = set()     # a library picture serves at most one line/caption per song
        self.reused = 0
        self.bought = 0

    def seed_used_ids(self, images_dir: Path) -> None:
        """A resumed song: pictures its earlier run already put in images/ count as used."""
        if not images_dir.is_dir():
            return
        for png in images_dir.glob("*.png"):
            try:
                self.used_ids.add(content_id(png))
            except OSError:
                continue

    def _disable(self, error: Exception) -> None:
        self.enabled = False
        print(
            f"WARNING: the image library is disabled for the rest of this song "
            f"({type(error).__name__}: {error}); buying images as usual",
            file=sys.stderr,
        )

    def find_match(self, prompt: str, dest: Path) -> Path | None:
        if not self.enabled or self.skip_lookup:
            return None
        try:
            query = self.embedder.embed_text([prompt])[0]
            match = self.library.best_match(query, self.used_ids, self.min_score)
            if match is None:
                return None
            try:
                self.library.copy_to(match.image_id, dest)
            except FileNotFoundError:
                # a row whose PNG was deleted from the library folder -- never offer it again this song
                self.used_ids.add(match.image_id)
                print(
                    f"WARNING: library image {match.image_id} is missing from disk; skipping it",
                    file=sys.stderr,
                )
                return None
            self.used_ids.add(match.image_id)
            self.library.record_reuse(match.image_id, self.song_slug)
            self.reused += 1
            print(f"Image library: reused an existing image (match {match.score:.2f}) instead of buying one")
            return dest
        except Exception as e:
            self._disable(e)
            return None

    def record_purchase(self, image_path: Path, prompt: str, source_text: str) -> None:
        self.bought += 1
        if not self.enabled:
            return
        try:
            embedding = self.embedder.embed_images([image_path])[0]
            image_id = self.library.add(image_path, embedding, prompt, source_text, self.song_title)
            self.used_ids.add(image_id)
        except Exception as e:
            self._disable(e)

    def summary_line(self) -> str:
        saved = self.reused * REPLICATE_PRICE_PER_IMAGE_USD
        return (
            f"Image library: {self.reused} reused, {self.bought} bought "
            f"(about ${saved:.2f} saved at ${REPLICATE_PRICE_PER_IMAGE_USD:.3f}/image)"
        )

    def close(self) -> None:
        close_embedder = getattr(self.embedder, "close", None)
        if close_embedder is not None:
            close_embedder()
        self.library.close()


def open_library_session(
    settings, song_slug: str, song_title: str, images_dir: Path, fresh_images: bool = False,
    embedder: Embedder | None = None, library: ImageLibrary | None = None,
) -> LibrarySession | None:
    """None whenever the library is off, unavailable, or fails to open -- the images stage then behaves exactly
    as it did before this feature existed. `embedder`/`library` are injection points for tests."""
    if settings is None or not settings.use_image_library:
        return None
    try:
        if embedder is None:
            clip = ClipEmbedder(allow_download=False)
            clip.check_available()
            embedder = clip
        if library is None:
            library = ImageLibrary()
        session = LibrarySession(
            library, embedder, song_slug, song_title, settings.image_library_min_score, skip_lookup=fresh_images,
        )
        session.seed_used_ids(images_dir)
        session.used_ids |= rejected_image_ids(images_dir.parent)       # pictures the owner replaced: never again
        return session
    except EmbedderUnavailable as e:
        print(
            f"Image library: {e}. Run scripts/import_image_library.py once to set it up; buying images as usual.",
            file=sys.stderr,
        )
        return None
    except Exception as e:
        print(
            f"WARNING: the image library could not be opened ({type(e).__name__}: {e}); buying images as usual",
            file=sys.stderr,
        )
        return None
