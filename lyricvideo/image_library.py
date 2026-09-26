"""Shared library of every background image already paid for (owner, 2026-09-25) -- see
docs/superpowers/specs/2026-09-25-shared-image-library-design.md.

Layout:  <library dir>/images/<id>.png   and   <library dir>/library.db (SQLite, WAL)
`id` is the sha256 of the PNG's own bytes, so the library dedupes by picture, not by the
lyric-line hash an image happened to be filed under in some song's work folder. Each row keeps
the image's L2-normalized CLIP embedding (cosine similarity == dot product), the Claude-written
prompt (blank for images imported from before prompts were saved), the lyric line/caption it was
made for, and the song. One ImageLibrary handle is meant for one thread; two processes may each
hold their own handle on the same folder."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection

import numpy as np

LIBRARY_ENV_VAR = "PLAYALONG_IMAGE_LIBRARY"
EMBEDDING_DIM = 512

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL DEFAULT '',
    source_text TEXT NOT NULL DEFAULT '',
    song_title TEXT NOT NULL DEFAULT '',
    embedding BLOB NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reuses (
    image_id TEXT NOT NULL,
    song_slug TEXT NOT NULL,
    reused_at TEXT NOT NULL
);
"""


def image_library_dir() -> Path:
    override = os.environ.get(LIBRARY_ENV_VAR, "").strip()
    return Path(override) if override else Path.home() / "PlayAlongVideoProductionImages"


def content_id(png_path: Path) -> str:
    return hashlib.sha256(Path(png_path).read_bytes()).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(vector) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


@dataclass(frozen=True)
class LibraryMatch:
    image_id: str
    score: float


class ImageLibrary:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else image_library_dir()
        self.images_dir = self.root / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.root / "library.db", timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._ids: list[str] | None = None       # lazily-loaded embedding matrix, kept in step with add()
        self._matrix: np.ndarray | None = None

    def image_path(self, image_id: str) -> Path:
        return self.images_dir / f"{image_id}.png"

    def has(self, image_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone() is not None

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM images").fetchone()[0])

    def get(self, image_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT prompt, source_text, song_title FROM images WHERE id = ?", (image_id,),
        ).fetchone()
        return None if row is None else {"prompt": row[0], "source_text": row[1], "song_title": row[2]}

    def add(self, png_path: Path, embedding, prompt: str = "", source_text: str = "", song_title: str = "") -> str:
        vector = _normalize(embedding)
        if vector.shape != (EMBEDDING_DIM,):
            raise ValueError(f"embedding must have {EMBEDDING_DIM} values, got {vector.shape[0]}")
        image_id = content_id(png_path)
        dest = self.image_path(image_id)
        if not dest.exists():
            # copy to a private temp name, then rename: another process never sees a half-written PNG
            tmp = dest.with_name(f".{image_id}.{os.getpid()}.tmp")
            shutil.copyfile(png_path, tmp)
            os.replace(tmp, dest)
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO images (id, prompt, source_text, song_title, embedding, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (image_id, prompt, source_text, song_title, vector.tobytes(), _now()),
        )
        self._db.commit()
        if cursor.rowcount == 1 and self._matrix is not None:
            self._ids.append(image_id)
            self._matrix = np.vstack([self._matrix, vector])
        return image_id

    def _ensure_matrix(self) -> None:
        if self._matrix is not None:
            return
        rows = self._db.execute("SELECT id, embedding FROM images ORDER BY added_at, id").fetchall()
        self._ids = [row[0] for row in rows]
        self._matrix = (
            np.vstack([np.frombuffer(row[1], dtype=np.float32) for row in rows])
            if rows else np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        )

    def best_match(self, query, exclude_ids: Collection[str], min_score: float) -> LibraryMatch | None:
        """The closest image not in `exclude_ids` whose cosine similarity to `query` is at least
        `min_score`, else None. Pass min_score=-1.0 to get the closest one regardless of the bar."""
        self._ensure_matrix()
        if not self._ids:
            return None
        scores = self._matrix @ _normalize(query)
        for index in np.argsort(-scores):
            image_id = self._ids[int(index)]
            if image_id in exclude_ids:
                continue
            score = float(scores[index])
            return LibraryMatch(image_id, score) if score >= min_score else None
        return None

    def copy_to(self, image_id: str, dest: Path) -> None:
        Path(dest).write_bytes(self.image_path(image_id).read_bytes())

    def record_reuse(self, image_id: str, song_slug: str) -> None:
        self._db.execute(
            "INSERT INTO reuses (image_id, song_slug, reused_at) VALUES (?, ?, ?)", (image_id, song_slug, _now()),
        )
        self._db.commit()

    def stats(self) -> dict:
        def one(sql: str) -> int:
            return int(self._db.execute(sql).fetchone()[0])

        return {
            "images": one("SELECT COUNT(*) FROM images"),
            "with_prompt": one("SELECT COUNT(*) FROM images WHERE prompt != ''"),
            "reuses": one("SELECT COUNT(*) FROM reuses"),
        }

    def close(self) -> None:
        self._db.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["stats"]:
        print("usage: python -m lyricvideo.image_library stats")
        return 2
    from .imagery import REPLICATE_PRICE_PER_IMAGE_USD

    library = ImageLibrary()
    stats = library.stats()
    print(f"Library: {library.root}")
    print(f"  {stats['images']} images ({stats['with_prompt']} with their prompt saved)")
    print(
        f"  reused {stats['reuses']} times -- about ${stats['reuses'] * REPLICATE_PRICE_PER_IMAGE_USD:.2f} "
        "of Replicate images not bought"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
