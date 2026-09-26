# Shared Image Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. (Owner's standing preference: execute inline.)

**Goal:** Before buying a background image from Replicate, look in a shared, local library of every image already bought (matched by meaning, using a local CLIP model) and reuse a close-enough one.

**Architecture:** A SQLite+PNG library outside the repo (`~/PlayAlongVideoProductionImages/`) stores each image with its CLIP embedding and (from now on) its prompt. `imagery.get_or_generate_image()` gains an optional per-song `LibrarySession`: after Claude writes the prompt, the prompt is embedded and compared with the library; a hit is copied into the song's own `images/` folder (so render/Redo are untouched), a miss buys as today and adds the new image to the library. Everything is off by default (`Settings.use_image_library=False`) and can never block a song.

**Tech Stack:** Python 3.11, SQLite (stdlib), numpy, `open_clip_torch` (ViT-B-32 / `laion2b_s34b_b79k`, CPU), CustomTkinter Settings panel, pytest.

**Spec:** `docs/superpowers/specs/2026-09-25-shared-image-library-design.md`

## Global Constraints

- Library dir: `PLAYALONG_IMAGE_LIBRARY` env var if set, else `Path.home() / "PlayAlongVideoProductionImages"`; never inside the repo; layout `images/<id>.png` + `library.db`; `id` = first 32 hex chars of the PNG bytes' sha256.
- Model: `ViT-B-32`, pretrained `laion2b_s34b_b79k`, weights file `open_clip_model.safetensors` (605.1 MB) from HF repo `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` (MIT). CPU only. Only that one file is downloaded (the repo holds four 605 MB copies).
- **A pipeline run never downloads the model** (`allow_download=False`); only `scripts/import_image_library.py` may.
- **Copy, never move.** A reused image is written to `work/<song>/images/<line_hash>.png`.
- **Once per song:** a library image serves at most one distinct line/caption per song, including images that song just bought; `used_ids` is seeded from the PNGs already in the song's `images/`.
- **Never blocks a song:** any library/embedder error is caught, warned in `imagery.py`'s style (`print(..., file=sys.stderr)`), disables the library for the rest of that song, and the stage buys as today.
- **Redo with "Generate new images"** skips the library *lookup* (`fresh_images=True`); purchases still go into the library.
- Settings: `use_image_library: bool = False`; `image_library_min_score: float = 0.28` (provisional placeholder inside the disabled feature). Settings-panel labels stay short (< ~40 chars).
- Price constant: `REPLICATE_PRICE_PER_IMAGE_USD = 0.003` ($3.00 per thousand images, Replicate pricing page, checked 2026-09-25).
- Tests: no network, no real model (inject a fake `Embedder`); the real-model tests are opt-in and self-skip when the model is not cached. Tests must never touch the owner's real library (conftest autouse fixture, Task 2).
- Cross-platform: `Path.home()`, no hardcoded venv path; scripts start with `sys.path.insert(0, str(Path(__file__).parent.parent))` like the other scripts.
- Owner directive 2026-09-25 ("finish this with no more input from me"): install the six packages shown in the 2026-09-25 dry-run (`open_clip_torch`, `torchvision`, `timm`, `ftfy`, `regex`, `wcwidth`), run the import and the contact sheet, then commit, push and cut a release per the standing instruction. **The feature stays OFF by default** until the owner reviews the contact sheet.

## Review Focus

- **Empty or brand-new library** (folder missing, zero rows): every lookup is a miss and the song buys normally — Task 2 (`test_an_empty_library_never_matches`, `test_image_library_creates_its_folder`), Task 4.
- **A library row whose PNG was deleted from disk:** that image is skipped and never re-offered this song; the song neither crashes nor loses the library — Task 4 (`test_a_library_row_whose_file_was_deleted_is_skipped_not_fatal`).
- **Several lines whose best match is the same image:** only the first gets it; the next gets the next-best above the threshold or buys — Task 4 (`test_once_per_song...`).
- **A corrupt PNG or unreadable `lyrics_timed.json` during import:** that one file is skipped and reported, everything else still imports — Task 7.
- **The embedder failing mid-song** (model error, out of memory): the library disables itself for the song, purchases continue, the count of purchases stays right — Task 4 (`test_an_embedder_error_disables_the_library_for_the_rest_of_the_song`).
- (Also covered: a prompt far longer than CLIP's 77-token window embeds without error — Task 3's opt-in real-model test.)

---

### Task 1: Settings fields and Settings-panel controls

**Files:**
- Modify: `lyricvideo/settings.py` (after the EASY CHORD block)
- Modify: `lyricvideo/settings_panel.py` (`values_to_settings`, `_build`)
- Test: `tests/test_settings.py`, `tests/test_settings_panel.py`

**Interfaces:**
- Produces: `Settings.use_image_library: bool` (default `False`), `Settings.image_library_min_score: float` (default `0.28`). `values_to_settings()` rounds `image_library_min_score` to 3 places.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings.py`:

```python
def test_image_library_settings_default_off_with_a_provisional_score():
    s = Settings()
    assert s.use_image_library is False
    assert s.image_library_min_score == 0.28


def test_image_library_settings_survive_a_save_and_load(tmp_path):
    path = tmp_path / "settings.json"
    Settings(use_image_library=True, image_library_min_score=0.31).save(path)

    loaded = Settings.load(path)

    assert loaded.use_image_library is True
    assert loaded.image_library_min_score == 0.31


def test_an_old_settings_file_without_the_image_library_keys_loads_the_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"fps": 30}), encoding="utf-8")

    loaded = Settings.load(path)

    assert loaded.fps == 30
    assert loaded.use_image_library is False
    assert loaded.image_library_min_score == 0.28
```

In `tests/test_settings_panel.py`, add these two entries to the dict returned by `_raw_defaults()` (after `"generate_easy_chord_versions": False,`):

```python
        "use_image_library": False,
        "image_library_min_score": 0.28,
```

and append:

```python
def test_values_to_settings_rounds_the_library_score_to_three_places():
    raw = _raw_defaults()
    raw["use_image_library"] = True
    raw["image_library_min_score"] = 0.30000000000000004  # what a slider drag hands back

    s = values_to_settings(raw)

    assert s.use_image_library is True
    assert s.image_library_min_score == 0.3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_settings.py tests/test_settings_panel.py -q`
Expected: FAIL (`Settings` has no `use_image_library`; `TypeError`/`AttributeError`).

- [ ] **Step 3: Implement**

In `lyricvideo/settings.py`, after the `generate_easy_chord_versions` field block (before `# --- Quality check`):

```python
    # --- Image library (library_session.py) --------------------------------
    use_image_library: bool = False  # owner, 2026-09-25: look in the shared library of already-bought images
                                      # before buying one from Replicate. Off until the owner has reviewed
                                      # scripts/preview_library_matches.py's contact sheet.
    image_library_min_score: float = 0.28  # CLIP text-to-image cosine similarity a library image needs to be
                                            # reused. PROVISIONAL placeholder -- the contact-sheet review sets it.
```

In `lyricvideo/settings_panel.py`, inside `values_to_settings()` right after the `_INT_FIELDS` loop:

```python
    if "image_library_min_score" in coerced:
        # a slider drag hands back float noise like 0.30000000000000004 -- keep settings.json and the
        # itemized Save confirm readable
        coerced["image_library_min_score"] = round(float(coerced["image_library_min_score"]), 3)
```

and in `SettingsPanel._build()`, between the "Image pacing" block and `self._section("Support overlay & description")`:

```python
        self._section("Image library")
        self._check("use_image_library", "Reuse images from the library")
        self._slider("image_library_min_score", "Library match strictness", 0.15, 0.40, 25, lambda v: f"{v:.2f}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_settings.py tests/test_settings_panel.py -q`
Expected: PASS.

- [ ] **Step 5: Smoke-build the real panel (skip only if there is no display)**

```bash
.venv/bin/python - <<'EOF'
import customtkinter as ctk
from lyricvideo.settings import Settings
from lyricvideo.settings_panel import SettingsPanel
root = ctk.CTk()
panel = SettingsPanel(root, Settings())
print(sorted(k for k in panel.vars if "library" in k), panel.collect().image_library_min_score)
root.destroy()
EOF
```
Expected: `['image_library_min_score', 'use_image_library'] 0.28`.

- [ ] **Step 6: Commit**

```bash
git add lyricvideo/settings.py lyricvideo/settings_panel.py tests/test_settings.py tests/test_settings_panel.py
git commit -m "Add the image-library Settings (off by default) and their panel controls

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: The library store (`image_library.py`)

**Files:**
- Create: `lyricvideo/image_library.py`
- Modify: `tests/conftest.py` (autouse isolation fixture)
- Test: `tests/test_image_library.py`

**Interfaces:**
- Produces:
  - `image_library_dir() -> Path`, `LIBRARY_ENV_VAR`, `EMBEDDING_DIM = 512`
  - `content_id(png_path: Path) -> str`
  - `LibraryMatch(image_id: str, score: float)` (frozen dataclass)
  - `ImageLibrary(root: Path | None = None)` with `.root`, `.images_dir`, `.image_path(id) -> Path`, `.has(id) -> bool`, `.count() -> int`, `.get(id) -> dict | None` (keys `prompt`, `source_text`, `song_title`), `.add(png_path, embedding, prompt="", source_text="", song_title="") -> str`, `.best_match(query, exclude_ids, min_score) -> LibraryMatch | None` (pass `min_score=-1.0` for "best regardless"), `.copy_to(image_id, dest) -> None` (raises `FileNotFoundError` if the PNG is gone), `.record_reuse(image_id, song_slug) -> None`, `.stats() -> dict` (`images`, `with_prompt`, `reuses`), `.close()`.
  - `python -m lyricvideo.image_library stats`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_image_library.py`:

```python
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from lyricvideo.image_library import (
    EMBEDDING_DIM, LIBRARY_ENV_VAR, ImageLibrary, content_id, image_library_dir, main,
)


def _vec(*head: float) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[: len(head)] = head
    return v


def _png(path: Path, color) -> Path:
    Image.new("RGB", (8, 8), color).save(path)
    return path


def test_image_library_dir_honours_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(LIBRARY_ENV_VAR, str(tmp_path / "custom"))
    assert image_library_dir() == tmp_path / "custom"


def test_image_library_dir_defaults_to_a_folder_in_the_home_directory(monkeypatch):
    monkeypatch.delenv(LIBRARY_ENV_VAR, raising=False)
    assert image_library_dir() == Path.home() / "PlayAlongVideoProductionImages"


def test_image_library_creates_its_folder(tmp_path):
    root = tmp_path / "does" / "not" / "exist"
    library = ImageLibrary(root)
    assert (root / "images").is_dir() and library.count() == 0


def test_add_copies_the_png_under_its_content_id_and_records_the_metadata(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    src = _png(tmp_path / "a.png", (255, 0, 0))

    image_id = library.add(src, _vec(1, 0), prompt="a red door", source_text="open the door", song_title="Door")

    assert image_id == content_id(src)
    assert library.image_path(image_id).read_bytes() == src.read_bytes()
    assert library.get(image_id) == {"prompt": "a red door", "source_text": "open the door", "song_title": "Door"}
    assert library.has(image_id) and library.count() == 1


def test_adding_the_same_picture_twice_keeps_one_entry(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    first = _png(tmp_path / "a.png", (1, 2, 3))
    second = tmp_path / "copy.png"
    second.write_bytes(first.read_bytes())

    assert library.add(first, _vec(1)) == library.add(second, _vec(1))
    assert library.count() == 1


def test_an_empty_library_never_matches(tmp_path):
    assert ImageLibrary(tmp_path / "lib").best_match(_vec(1), set(), 0.0) is None


def test_best_match_returns_the_closest_image_above_the_threshold(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    a = library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    library.add(_png(tmp_path / "b.png", (0, 255, 0)), _vec(0, 1))

    match = library.best_match(_vec(1, 0.1), set(), 0.9)

    assert match is not None and match.image_id == a
    assert match.score == pytest.approx(0.995, abs=1e-3)
    assert library.best_match(_vec(1, 0.1), set(), 0.999) is None


def test_best_match_skips_excluded_ids_and_falls_to_the_next_best(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    a = library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    b = library.add(_png(tmp_path / "b.png", (0, 255, 0)), _vec(1, 1))

    match = library.best_match(_vec(1, 0), {a}, 0.5)

    assert match is not None and match.image_id == b
    assert library.best_match(_vec(1, 0), {a}, 0.9) is None  # the runner-up is below the bar


def test_best_match_sees_images_added_after_it_first_ran(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    assert library.best_match(_vec(1), set(), 0.5) is None
    new_id = library.add(_png(tmp_path / "a.png", (9, 9, 9)), _vec(1))

    match = library.best_match(_vec(1), set(), 0.5)

    assert match is not None and match.image_id == new_id


def test_the_library_persists_across_handles(tmp_path):
    root = tmp_path / "lib"
    image_id = ImageLibrary(root).add(_png(tmp_path / "a.png", (5, 5, 5)), _vec(1), prompt="p")

    reopened = ImageLibrary(root)

    assert reopened.count() == 1
    assert reopened.best_match(_vec(1), set(), 0.5).image_id == image_id


def test_two_handles_can_add_to_the_same_library(tmp_path):
    root = tmp_path / "lib"
    one, two = ImageLibrary(root), ImageLibrary(root)
    one.add(_png(tmp_path / "a.png", (1, 1, 1)), _vec(1))
    two.add(_png(tmp_path / "b.png", (2, 2, 2)), _vec(0, 1))

    assert ImageLibrary(root).count() == 2


def test_copy_to_writes_identical_bytes_and_a_missing_file_raises(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    src = _png(tmp_path / "a.png", (7, 7, 7))
    image_id = library.add(src, _vec(1))
    dest = tmp_path / "song" / "x.png"
    dest.parent.mkdir()

    library.copy_to(image_id, dest)
    assert dest.read_bytes() == src.read_bytes()

    library.image_path(image_id).unlink()
    with pytest.raises(FileNotFoundError):
        library.copy_to(image_id, tmp_path / "song" / "y.png")


def test_add_rejects_a_wrong_sized_embedding(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    with pytest.raises(ValueError):
        library.add(_png(tmp_path / "a.png", (1, 1, 1)), np.ones(7, dtype=np.float32))


def test_stats_counts_images_prompts_and_reuses(tmp_path):
    library = ImageLibrary(tmp_path / "lib")
    a = library.add(_png(tmp_path / "a.png", (1, 1, 1)), _vec(1), prompt="has a prompt")
    library.add(_png(tmp_path / "b.png", (2, 2, 2)), _vec(0, 1))
    library.record_reuse(a, "some-song")

    assert library.stats() == {"images": 2, "with_prompt": 1, "reuses": 1}


def test_the_stats_command_reports_the_savings(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(LIBRARY_ENV_VAR, str(tmp_path / "lib"))
    library = ImageLibrary()
    image_id = library.add(_png(tmp_path / "a.png", (3, 3, 3)), _vec(1))
    for _ in range(10):
        library.record_reuse(image_id, "s")
    library.close()

    assert main(["stats"]) == 0

    out = capsys.readouterr().out
    assert "1 images" in out and "reused 10 times" in out and "$0.03" in out
```

Add to `tests/conftest.py` (below the existing `_isolate_redo_log` fixture):

```python
@pytest.fixture(autouse=True)
def _isolate_image_library(tmp_path_factory, monkeypatch):
    """Tests must never read or write the owner's real ~/PlayAlongVideoProductionImages library."""
    monkeypatch.setenv("PLAYALONG_IMAGE_LIBRARY", str(tmp_path_factory.mktemp("imagelib")))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_image_library.py -q`
Expected: FAIL (`ModuleNotFoundError: lyricvideo.image_library`).

- [ ] **Step 3: Implement**

Create `lyricvideo/image_library.py`:

```python
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
        one = lambda sql: int(self._db.execute(sql).fetchone()[0])  # noqa: E731
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
```

Also add the price constant now, in `lyricvideo/imagery.py` next to `_MAX_GENERATION_ATTEMPTS = 3`:

```python
# Replicate's pricing page, checked 2026-09-25: black-forest-labs/flux-schnell is "$3.00 / thousand output images".
REPLICATE_PRICE_PER_IMAGE_USD = 0.003
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_image_library.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/image_library.py lyricvideo/imagery.py tests/test_image_library.py tests/conftest.py
git commit -m "Add the shared image library store (SQLite index + PNG folder)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: The CLIP embedder (`clip_embedder.py`)

**Files:**
- Create: `lyricvideo/clip_embedder.py`
- Test: `tests/test_clip_embedder.py`

**Interfaces:**
- Consumes: `EMBEDDING_DIM` from `image_library`.
- Produces: `EmbedderUnavailable(Exception)`; `Embedder` Protocol (`embed_images(paths) -> np.ndarray (n, 512)`, `embed_text(texts) -> np.ndarray (n, 512)`, both rows L2-normalized); `ClipEmbedder(allow_download: bool = False)` with `.check_available() -> Path` (cheap: no model load), lazy model load on first `embed_*`, `.close()`. Module constants `HF_REPO_ID`, `WEIGHTS_FILENAME`, `MODEL_NAME`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clip_embedder.py`:

```python
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import lyricvideo.clip_embedder as clip_embedder
from lyricvideo.clip_embedder import ClipEmbedder, EmbedderUnavailable


def test_importing_the_module_does_not_import_open_clip_or_torch():
    # a fresh interpreter: the lazy import is what keeps the GUI's launch as fast as before
    import subprocess

    code = "import sys, lyricvideo.clip_embedder; sys.exit(0 if 'open_clip' not in sys.modules and 'torch' not in sys.modules else 1)"
    assert subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent).returncode == 0


def test_unavailable_when_open_clip_is_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "open_clip", None)  # makes `import open_clip` raise ImportError

    with pytest.raises(EmbedderUnavailable, match="open_clip"):
        ClipEmbedder().check_available()


def test_unavailable_when_the_weights_are_not_downloaded(monkeypatch):
    monkeypatch.setattr(clip_embedder, "_require_open_clip", lambda: None)

    def _not_cached(*args, **kwargs):
        assert kwargs["local_files_only"] is True  # a pipeline run must never start a download
        raise OSError("not in the local cache")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", _not_cached)

    with pytest.raises(EmbedderUnavailable, match="not downloaded"):
        ClipEmbedder(allow_download=False).check_available()


def test_allow_download_lets_the_weights_be_fetched(monkeypatch, tmp_path):
    monkeypatch.setattr(clip_embedder, "_require_open_clip", lambda: None)
    seen = {}

    def _fetch(repo_id, filename, local_files_only):
        seen.update(repo_id=repo_id, filename=filename, local_files_only=local_files_only)
        return str(tmp_path / filename)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", _fetch)

    path = ClipEmbedder(allow_download=True).check_available()

    assert path == tmp_path / clip_embedder.WEIGHTS_FILENAME
    assert seen == {
        "repo_id": clip_embedder.HF_REPO_ID, "filename": clip_embedder.WEIGHTS_FILENAME, "local_files_only": False,
    }


# --- opt-in: the real model, only when open_clip is installed AND the weights are already cached -------------

@pytest.fixture(scope="module")
def real_embedder():
    pytest.importorskip("open_clip")
    embedder = ClipEmbedder(allow_download=False)
    try:
        embedder.check_available()
    except EmbedderUnavailable as e:
        pytest.skip(f"CLIP weights not cached: {e}")
    yield embedder
    embedder.close()


def _solid(tmp_path: Path, name: str, color) -> Path:
    path = tmp_path / name
    Image.new("RGB", (320, 180), color).save(path)
    return path


def test_real_model_returns_normalized_512_vectors(real_embedder, tmp_path):
    images = real_embedder.embed_images([_solid(tmp_path, "r.png", (255, 0, 0)), _solid(tmp_path, "b.png", (0, 0, 255))])
    text = real_embedder.embed_text(["a red square", "a blue square"])

    assert images.shape == (2, 512) and text.shape == (2, 512)
    assert np.allclose(np.linalg.norm(images, axis=1), 1.0, atol=1e-4)
    assert np.allclose(np.linalg.norm(text, axis=1), 1.0, atol=1e-4)


def test_real_model_matches_a_colour_word_to_the_right_picture(real_embedder, tmp_path):
    red = real_embedder.embed_images([_solid(tmp_path, "r.png", (255, 0, 0))])[0]
    blue = real_embedder.embed_images([_solid(tmp_path, "b.png", (0, 0, 255))])[0]
    text = real_embedder.embed_text(["a plain red background"])[0]

    assert float(text @ red) > float(text @ blue)


def test_real_model_embeds_a_prompt_far_longer_than_clips_77_token_window(real_embedder):
    vector = real_embedder.embed_text([" ".join(["a lonely lighthouse in a storm at dusk"] * 60)])

    assert vector.shape == (1, 512) and np.isfinite(vector).all()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_clip_embedder.py -q`
Expected: FAIL (`ModuleNotFoundError: lyricvideo.clip_embedder`).

- [ ] **Step 3: Implement**

Create `lyricvideo/clip_embedder.py`:

```python
"""Local CLIP image/text embeddings for the shared image library (spec 2026-09-25).

`open_clip` and `torch` are imported lazily, only when a model is actually needed, so nothing else
in the app (the GUI's launch, every other stage) pays for them. A pipeline run passes
allow_download=False and therefore NEVER starts the ~605 MB weights download -- if the weights are not
already on disk it raises EmbedderUnavailable and the images stage buys as usual. Only
scripts/import_image_library.py (started by the owner) constructs ClipEmbedder(allow_download=True).

Weights: the HF repo laion/CLIP-ViT-B-32-laion2B-s34B-b79K holds four ~605 MB copies of them; only
open_clip_model.safetensors is fetched. CPU only (the owner's GT 1030 has 2 GB and CPU is plenty)."""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from .image_library import EMBEDDING_DIM

MODEL_NAME = "ViT-B-32"
HF_REPO_ID = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
WEIGHTS_FILENAME = "open_clip_model.safetensors"
_IMAGE_BATCH = 32


class EmbedderUnavailable(Exception):
    """The CLIP model can't be used right now (package missing, or weights not downloaded)."""


class Embedder(Protocol):
    def embed_images(self, paths: Sequence[Path]) -> np.ndarray: ...   # (n, 512), L2-normalized rows
    def embed_text(self, texts: Sequence[str]) -> np.ndarray: ...      # (n, 512), L2-normalized rows


def _require_open_clip() -> None:
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
    except ImportError as e:
        raise EmbedderUnavailable(
            f"open_clip is not installed ({e}) -- run: .venv/bin/python -m pip install open_clip_torch"
        ) from e


def _locate_weights(allow_download: bool) -> Path:
    from huggingface_hub import hf_hub_download

    try:
        return Path(hf_hub_download(HF_REPO_ID, WEIGHTS_FILENAME, local_files_only=not allow_download))
    except Exception as e:
        raise EmbedderUnavailable(
            f"CLIP weights are not downloaded ({type(e).__name__}) -- run scripts/import_image_library.py once"
        ) from e


class ClipEmbedder:
    def __init__(self, allow_download: bool = False):
        self.allow_download = allow_download
        self._weights_path: Path | None = None
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._torch = None

    def check_available(self) -> Path:
        """Cheap: confirms open_clip imports and the weights file is on disk (downloading it only when
        allow_download is set). Does not load the model."""
        _require_open_clip()
        self._weights_path = _locate_weights(self.allow_download)
        return self._weights_path

    def _load(self) -> None:
        if self._model is not None:
            return
        weights = self._weights_path or self.check_available()
        import open_clip
        import torch

        model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=str(weights), device="cpu")
        model.eval()
        self._model, self._preprocess = model, preprocess
        self._tokenizer = open_clip.get_tokenizer(MODEL_NAME)
        self._torch = torch

    def _normalized(self, features) -> np.ndarray:
        features = features / features.norm(dim=-1, keepdim=True)
        return features.float().cpu().numpy()

    def embed_images(self, paths: Sequence[Path]) -> np.ndarray:
        from PIL import Image

        self._load()
        chunks = []
        for start in range(0, len(paths), _IMAGE_BATCH):
            tensors = []
            for path in paths[start:start + _IMAGE_BATCH]:
                with Image.open(path) as image:
                    tensors.append(self._preprocess(image.convert("RGB")))
            with self._torch.no_grad():
                chunks.append(self._normalized(self._model.encode_image(self._torch.stack(tensors))))
        return np.vstack(chunks) if chunks else np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    def embed_text(self, texts: Sequence[str]) -> np.ndarray:
        self._load()
        tokens = self._tokenizer(list(texts))   # CLIP reads at most 77 tokens; longer prompts are truncated
        with self._torch.no_grad():
            return self._normalized(self._model.encode_text(tokens))

    def close(self) -> None:
        """Frees the model (~600 MB) -- called at the end of the images stage."""
        self._model = self._preprocess = self._tokenizer = self._torch = None
        gc.collect()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_clip_embedder.py -q`
Expected: the four unit tests PASS; the three real-model tests SKIP (open_clip not installed yet — Task 9 installs it and runs them).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/clip_embedder.py tests/test_clip_embedder.py
git commit -m "Add the local CLIP embedder (lazy, offline-only during pipeline runs)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: The per-song library session (`library_session.py`)

**Files:**
- Create: `lyricvideo/library_session.py`
- Test: `tests/test_library_session.py`

**Interfaces:**
- Consumes: `ImageLibrary`, `content_id` (Task 2); `ClipEmbedder`, `Embedder`, `EmbedderUnavailable` (Task 3); `REPLICATE_PRICE_PER_IMAGE_USD` from `imagery` (Task 2).
- Produces:
  - `LibrarySession(library, embedder, song_slug, song_title, min_score, skip_lookup=False)` with `.enabled`, `.used_ids`, `.reused`, `.bought`, `.seed_used_ids(images_dir)`, `.find_match(prompt: str, dest: Path) -> Path | None` (on a hit writes `dest` and returns it), `.record_purchase(image_path: Path, prompt: str, source_text: str) -> None`, `.summary_line() -> str`, `.close()`.
  - `open_library_session(settings, song_slug, song_title, images_dir, fresh_images=False, embedder=None, library=None) -> LibrarySession | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_library_session.py`:

```python
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import lyricvideo.library_session as library_session
from lyricvideo.image_library import EMBEDDING_DIM, ImageLibrary
from lyricvideo.library_session import LibrarySession, open_library_session
from lyricvideo.settings import Settings


def _vec(*head: float) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[: len(head)] = head
    return v


class _FakeEmbedder:
    """Prompts map to vectors by exact text; images map to vectors by file name."""

    def __init__(self, texts: dict[str, np.ndarray], images: dict[str, np.ndarray] | None = None,
                 default_image: np.ndarray | None = None):
        self.texts, self.images, self.default_image = texts, images or {}, default_image
        self.text_calls = 0
        self.closed = False

    def embed_text(self, texts):
        self.text_calls += 1
        return np.vstack([self.texts[t] for t in texts])

    def embed_images(self, paths):
        return np.vstack([self.images.get(Path(p).name, self.default_image) for p in paths])

    def close(self):
        self.closed = True


def _png(path: Path, color) -> Path:
    Image.new("RGB", (8, 8), color).save(path)
    return path


def _session(tmp_path, texts, min_score=0.5, skip_lookup=False, images=None):
    library = ImageLibrary(tmp_path / "lib")
    embedder = _FakeEmbedder(texts, images)
    return LibrarySession(library, embedder, "my-song", "My Song", min_score, skip_lookup=skip_lookup), library, embedder


def test_a_hit_copies_the_library_image_records_the_reuse_and_returns_the_destination(tmp_path):
    session, library, _ = _session(tmp_path, {"a red door": _vec(1, 0)})
    src = _png(tmp_path / "a.png", (255, 0, 0))
    library.add(src, _vec(1, 0))
    dest = tmp_path / "song" / "line.png"
    dest.parent.mkdir()

    result = session.find_match("a red door", dest)

    assert result == dest and dest.read_bytes() == src.read_bytes()
    assert session.reused == 1 and library.stats()["reuses"] == 1


def test_a_miss_below_the_threshold_returns_none(tmp_path):
    session, library, _ = _session(tmp_path, {"a green field": _vec(0, 1)})
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("a green field", tmp_path / "x.png") is None
    assert session.reused == 0 and not (tmp_path / "x.png").exists()


def test_once_per_song_the_second_line_cannot_take_the_same_image(tmp_path):
    session, library, _ = _session(
        tmp_path, {"first": _vec(1, 0), "second": _vec(1, 0.5)}, min_score=0.4,
    )
    a = library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    b = library.add(_png(tmp_path / "b.png", (0, 255, 0)), _vec(0, 1))

    session.find_match("first", tmp_path / "1.png")
    session.find_match("second", tmp_path / "2.png")

    assert (tmp_path / "1.png").read_bytes() == library.image_path(a).read_bytes()
    assert (tmp_path / "2.png").read_bytes() == library.image_path(b).read_bytes()  # next-best, not the same picture


def test_once_per_song_with_no_runner_up_the_second_line_gets_no_match(tmp_path):
    session, library, _ = _session(tmp_path, {"first": _vec(1, 0), "second": _vec(1, 0)}, min_score=0.9)
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("first", tmp_path / "1.png") is not None
    assert session.find_match("second", tmp_path / "2.png") is None


def test_a_purchase_is_added_with_its_prompt_and_never_matched_again_this_song(tmp_path):
    session, library, _ = _session(
        tmp_path, {"same idea": _vec(1, 0)}, images={"bought.png": _vec(1, 0)},
    )
    bought = _png(tmp_path / "bought.png", (10, 20, 30))

    session.record_purchase(bought, "a moody forest", "the line text")

    assert session.bought == 1 and library.count() == 1
    (image_id,) = session.used_ids
    assert library.get(image_id) == {"prompt": "a moody forest", "source_text": "the line text", "song_title": "My Song"}
    assert session.find_match("same idea", tmp_path / "x.png") is None   # its own song's picture is off-limits


def test_a_resumed_song_cannot_repick_a_picture_its_earlier_run_already_used(tmp_path):
    session, library, _ = _session(tmp_path, {"a red door": _vec(1, 0)})
    src = _png(tmp_path / "a.png", (255, 0, 0))
    library.add(src, _vec(1, 0))
    images_dir = tmp_path / "song-images"
    images_dir.mkdir()
    (images_dir / "earlier-line.png").write_bytes(src.read_bytes())   # the earlier run already reused it

    session.seed_used_ids(images_dir)

    assert session.find_match("a red door", tmp_path / "x.png") is None


def test_fresh_images_skips_the_lookup_but_still_files_purchases(tmp_path):
    session, library, embedder = _session(
        tmp_path, {"a red door": _vec(1, 0)}, skip_lookup=True, images={"new.png": _vec(0, 1)},
    )
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("a red door", tmp_path / "x.png") is None
    assert embedder.text_calls == 0

    session.record_purchase(_png(tmp_path / "new.png", (1, 2, 3)), "p", "t")
    assert library.count() == 2


def test_a_library_row_whose_file_was_deleted_is_skipped_not_fatal(tmp_path, capsys):
    session, library, _ = _session(tmp_path, {"one": _vec(1, 0), "two": _vec(1, 0.4)}, min_score=0.3)
    gone = library.add(_png(tmp_path / "gone.png", (255, 0, 0)), _vec(1, 0))
    kept = library.add(_png(tmp_path / "kept.png", (0, 255, 0)), _vec(0, 1))
    library.image_path(gone).unlink()

    assert session.find_match("one", tmp_path / "1.png") is None      # best match was the deleted one
    assert session.enabled is True                                     # ...but the library itself is fine
    assert "WARNING" in capsys.readouterr().err
    assert session.find_match("two", tmp_path / "2.png") is not None   # and the next line can still hit the other image
    assert (tmp_path / "2.png").read_bytes() == library.image_path(kept).read_bytes()


def test_an_embedder_error_disables_the_library_for_the_rest_of_the_song(tmp_path, capsys):
    session, library, embedder = _session(tmp_path, {})    # embed_text raises KeyError for any prompt
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("anything", tmp_path / "x.png") is None
    assert session.enabled is False
    err = capsys.readouterr().err
    assert "WARNING" in err and "disabled" in err

    calls = embedder.text_calls
    assert session.find_match("again", tmp_path / "y.png") is None
    assert embedder.text_calls == calls                                # not even tried again

    session.record_purchase(_png(tmp_path / "p.png", (5, 5, 5)), "p", "t")
    assert session.bought == 1 and library.count() == 1               # counted, but not filed once disabled


def test_the_summary_line_reports_reuses_purchases_and_the_saving(tmp_path):
    session, _, _ = _session(tmp_path, {})
    session.reused, session.bought = 14, 22

    assert session.summary_line() == "Image library: 14 reused, 22 bought (about $0.04 saved at $0.003/image)"


def test_close_frees_the_embedder_and_the_database(tmp_path):
    session, _, embedder = _session(tmp_path, {})

    session.close()

    assert embedder.closed is True


def test_open_library_session_is_none_when_the_setting_is_off(tmp_path):
    assert open_library_session(Settings(), "song", "Song", tmp_path / "images") is None
    assert open_library_session(None, "song", "Song", tmp_path / "images") is None


def test_open_library_session_builds_a_seeded_session_from_the_settings(tmp_path):
    src = _png(tmp_path / "a.png", (255, 0, 0))
    library = ImageLibrary(tmp_path / "lib")
    library.add(src, _vec(1, 0))
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "x.png").write_bytes(src.read_bytes())

    session = open_library_session(
        Settings(use_image_library=True, image_library_min_score=0.33), "song", "Song", images_dir,
        fresh_images=True, embedder=_FakeEmbedder({}), library=library,
    )

    assert session is not None
    assert session.min_score == 0.33 and session.skip_lookup is True and session.song_slug == "song"
    assert len(session.used_ids) == 1


def test_open_library_session_falls_back_quietly_when_the_model_is_not_installed(tmp_path, monkeypatch, capsys):
    class _NoModel:
        def __init__(self, allow_download=False):
            assert allow_download is False

        def check_available(self):
            raise library_session.EmbedderUnavailable("CLIP weights are not downloaded")

    monkeypatch.setattr(library_session, "ClipEmbedder", _NoModel)

    session = open_library_session(Settings(use_image_library=True), "song", "Song", tmp_path / "images")

    assert session is None
    assert "import_image_library" in capsys.readouterr().err


def test_a_library_bought_image_serves_a_second_song_end_to_end(tmp_path, monkeypatch):
    """Song one buys (and files) a picture; song two, with a similar prompt, reuses it and never calls Replicate."""
    from lyricvideo.imagery import get_or_generate_image

    class _Claude:
        def __init__(self, prompt):
            self.messages = self
            self._prompt = prompt

        def create(self, **kwargs):
            block = type("B", (), {"type": "text", "text": self._prompt})()
            return type("R", (), {"content": [block]})()

    bought = []

    def _fake_generate(token, prompt, out_path, *a, **k):
        bought.append(prompt)
        _png(out_path, (200, 10, 10))
        return out_path

    monkeypatch.setattr("lyricvideo.imagery.generate_line_image", _fake_generate)
    library = ImageLibrary(tmp_path / "lib")
    texts = {"a red door at dusk": _vec(1, 0), "a crimson doorway in twilight": _vec(0.95, 0.1)}

    for slug, prompt in [("song-one", "a red door at dusk"), ("song-two", "a crimson doorway in twilight")]:
        images_dir = tmp_path / slug / "images"
        # every picture a song buys is fingerprinted as the same "red door" vector
        session = LibrarySession(library, _FakeEmbedder(texts, default_image=_vec(1, 0)), slug, slug, 0.8)
        session.seed_used_ids(images_dir)
        get_or_generate_image(_Claude(prompt), "tok", "gist", f"{slug} lyric", images_dir, library=session)

    assert bought == ["a red door at dusk"]                     # only song one paid for anything
    assert library.stats() == {"images": 1, "with_prompt": 1, "reuses": 1}
    assert len(list((tmp_path / "song-two" / "images").glob("*.png"))) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_library_session.py -q`
Expected: FAIL (`ModuleNotFoundError: lyricvideo.library_session`).

- [ ] **Step 3: Implement**

Create `lyricvideo/library_session.py`:

```python
"""Per-song glue between imagery.get_or_generate_image() and the shared image library (spec 2026-09-25).

One LibrarySession lives for one song's images stage. It embeds the Claude-written prompt, asks the library
for a close-enough picture the song hasn't used yet, copies a hit into the song's own images/ folder, and files
every newly bought picture (with its real prompt) into the library. It never raises: any failure disables the
library for the rest of the song and the caller simply buys as it always did."""

from __future__ import annotations

import sys
from pathlib import Path

from .clip_embedder import ClipEmbedder, Embedder, EmbedderUnavailable
from .image_library import ImageLibrary, content_id
from .imagery import REPLICATE_PRICE_PER_IMAGE_USD


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_library_session.py -q`
Expected: PASS. (The last test needs Task 5's `library=` parameter — it is written now and goes green at the end of Task 5; until then run the others with `-k "not end_to_end"`.)

- [ ] **Step 5: Commit (after Task 5 makes the whole file green)** — combined with Task 5's commit below.

---

### Task 5: Wire the session into `imagery.get_or_generate_image`

**Files:**
- Modify: `lyricvideo/imagery.py` (`get_or_generate_image`, imports)
- Test: `tests/test_imagery.py`

**Interfaces:**
- Consumes: `LibrarySession.find_match(prompt, dest) -> Path | None`, `LibrarySession.record_purchase(image_path, prompt, source_text)`.
- Produces: `get_or_generate_image(..., previous_image=None, library: LibrarySession | None = None) -> Path`. `library=None` reproduces today's behaviour exactly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_imagery.py`:

```python
class _StubLibrary:
    """Stands in for a LibrarySession: records the calls get_or_generate_image makes on it."""

    def __init__(self, hit_bytes=None):
        self.hit_bytes = hit_bytes
        self.lookups = []
        self.purchases = []

    def find_match(self, prompt, dest):
        self.lookups.append(prompt)
        if self.hit_bytes is None:
            return None
        dest.write_bytes(self.hit_bytes)
        return dest

    def record_purchase(self, image_path, prompt, source_text):
        self.purchases.append((image_path, prompt, source_text))


def test_a_library_hit_is_used_without_calling_replicate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.imagery.generate_line_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Replicate must not be called on a library hit")),
    )
    library = _StubLibrary(hit_bytes=b"library-picture")

    path = get_or_generate_image(
        _FakeAnthropicClient("a moody forest at dusk"), "tok", "gist", "some line", tmp_path, library=library,
    )

    assert path.read_bytes() == b"library-picture"
    assert library.lookups == ["a moody forest at dusk"] and library.purchases == []


def test_a_library_miss_buys_and_files_the_purchase_with_its_prompt(tmp_path, monkeypatch):
    def fake_generate(token, prompt, out_path, model="black-forest-labs/flux-schnell"):
        out_path.write_bytes(b"bought-picture")
        return out_path

    monkeypatch.setattr("lyricvideo.imagery.generate_line_image", fake_generate)
    library = _StubLibrary()

    path = get_or_generate_image(
        _FakeAnthropicClient("a lighthouse"), "tok", "gist", "the line", tmp_path, library=library,
    )

    assert path.read_bytes() == b"bought-picture"
    assert library.purchases == [(path, "a lighthouse", "the line")]


def test_the_library_is_consulted_once_and_the_saved_prompt_is_the_one_that_succeeded(tmp_path, monkeypatch):
    prompts = iter(["prompt one", "prompt two"])

    class _Messages:
        def create(self, **kwargs):
            return _FakeResponse(next(prompts))

    class _Client:
        messages = _Messages()

    attempts = []

    def flaky_generate(token, prompt, out_path, model="black-forest-labs/flux-schnell"):
        attempts.append(prompt)
        if len(attempts) == 1:
            raise RuntimeError("content filter")
        out_path.write_bytes(b"second-try")
        return out_path

    monkeypatch.setattr("lyricvideo.imagery.generate_line_image", flaky_generate)
    library = _StubLibrary()

    path = get_or_generate_image(_Client(), "tok", "gist", "the line", tmp_path, library=library)

    assert library.lookups == ["prompt one"]                        # once, with the first prompt that built
    assert library.purchases == [(path, "prompt two", "the line")]  # the prompt that actually made the picture


def test_an_already_cached_image_never_touches_the_library(tmp_path):
    from lyricvideo.models import line_hash

    (tmp_path / f"{line_hash('same line')}.png").write_bytes(b"already here")
    library = _StubLibrary(hit_bytes=b"should not be used")

    path = get_or_generate_image(_FakeAnthropicClient(), "tok", "gist", "same line", tmp_path, library=library)

    assert path.read_bytes() == b"already here"
    assert library.lookups == [] and library.purchases == []


def test_a_failed_generation_never_files_a_placeholder_into_the_library(tmp_path):
    class _FailingMessages:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    class _FailingClient:
        messages = _FailingMessages()

    library = _StubLibrary()

    get_or_generate_image(_FailingClient(), "tok", "gist", "a broken line", tmp_path, library=library)

    assert library.purchases == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_imagery.py -q -k library`
Expected: FAIL (`TypeError: get_or_generate_image() got an unexpected keyword argument 'library'`).

- [ ] **Step 3: Implement**

In `lyricvideo/imagery.py`, add to the imports block:

```python
from typing import TYPE_CHECKING
```

and after `from .models import line_hash`:

```python
if TYPE_CHECKING:
    from .library_session import LibrarySession
```

Change the signature and loop of `get_or_generate_image`:

```python
def get_or_generate_image(
    anthropic_client,
    replicate_token: str,
    song_gist: str,
    line_text: str,
    cache_dir: Path,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
    extra_cache_dirs: list[Path] | None = None,
    previous_image: Path | None = None,
    library: "LibrarySession | None" = None,
) -> Path:
```

Replace the `for attempt ...` loop body so it reads:

```python
    last_error: Exception | None = None
    library_consulted = False
    for attempt in range(_MAX_GENERATION_ATTEMPTS):
        try:
            prompt = build_image_prompt(anthropic_client, song_gist, line_text)
            if library is not None and not library_consulted:
                # Once, with the first prompt that builds: a later retry's rewritten prompt is not re-matched.
                # The library never raises (it disables itself), so a problem there can't trigger a retry.
                library_consulted = True
                hit = library.find_match(prompt, cached_path)
                if hit is not None:
                    return hit
            generate_line_image(replicate_token, prompt, cached_path)
            if library is not None:
                library.record_purchase(cached_path, prompt, line_text)
            return cached_path
        except Exception as e:
            last_error = e
            print(
                f"WARNING: image generation attempt {attempt + 1}/{_MAX_GENERATION_ATTEMPTS} "
                f"failed for line {key}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
```

(The previous-image and plain-color fallbacks below the loop stay exactly as they are.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_imagery.py tests/test_library_session.py -q`
Expected: PASS (existing imagery tests unchanged and green; the end-to-end session test now green too).

- [ ] **Step 5: Commit Tasks 4 and 5**

```bash
git add lyricvideo/library_session.py lyricvideo/imagery.py tests/test_library_session.py tests/test_imagery.py
git commit -m "Reuse a close library image before buying one, and file every purchase with its prompt

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Pipeline and GUI plumbing (`fresh_images`, session lifecycle)

**Files:**
- Modify: `lyricvideo/pipeline.py` (import, `run_pipeline` signature + docstring, images stage)
- Modify: `lyricvideo/gui.py` (`_run_worker`, the Redo call)
- Test: `tests/test_pipeline.py`, `tests/test_gui.py`

**Interfaces:**
- Consumes: `open_library_session(settings, song_slug, song_title, images_dir, fresh_images=...) -> LibrarySession | None`, `LibrarySession.summary_line()`, `.close()`.
- Produces: `run_pipeline(..., capo=None, fresh_images: bool = False)`; `LyricVideoGUI._run_worker(..., force_easy_chord=False, fresh_images=False)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pipeline.py` (it already imports `Settings`? if not, add `from lyricvideo.settings import Settings` to its imports):

```python
def test_run_pipeline_images_stage_hands_the_library_session_to_every_image_and_closes_it(tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path)

    class _Session:
        closed = False

        def summary_line(self):
            return "Image library: 0 reused, 2 bought (about $0.00 saved at $0.003/image)"

        def close(self):
            self.closed = True

    session, opened = _Session(), {}

    def _fake_open(settings, song_slug, song_title, images_dir, fresh_images=False, **kwargs):
        opened.update(settings=settings, slug=song_slug, title=song_title, fresh=fresh_images)
        return session

    monkeypatch.setattr("lyricvideo.pipeline.open_library_session", _fake_open)
    seen = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: seen.append(k.get("library")) or Path("x"),
    )
    settings = Settings(use_image_library=True)

    run_pipeline(Path("audio.mp3"), tmp_path / "work", settings=settings, fresh_images=True)

    assert seen and all(library is session for library in seen)
    assert opened == {"settings": settings, "slug": "work", "title": "Test Song", "fresh": True}
    assert session.closed is True
    assert "Image library: 0 reused, 2 bought" in capsys.readouterr().out


def test_run_pipeline_without_the_library_passes_none_and_prints_no_summary(tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: seen.append(k.get("library", "missing")) or Path("x"),
    )

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert seen and all(library is None for library in seen)
    assert "Image library:" not in capsys.readouterr().out
```

In `tests/test_gui.py`: change the existing assertion in `test_on_redo_passes_the_easy_chord_checkbox_through_to_the_worker_thread` (line ~1311) to

```python
    assert calls == [
        ((audio_path, tmp_path / "work" / "angie", "Angie", "fetch_lyrics"),
         {"force_easy_chord": True, "fresh_images": False}),
    ]
```

and append:

```python
def test_on_redo_passes_generate_new_images_through_as_fresh_images(monkeypatch, tmp_path):
    """Redo with "Generate new images" must reach run_pipeline as fresh_images, so the image library is not
    asked for the very pictures the owner just asked to replace."""
    monkeypatch.setattr("lyricvideo.gui.PROJECT_ROOT", tmp_path)
    audio_path = tmp_path / "angie.mp3"
    audio_path.write_bytes(b"fake")
    monkeypatch.setattr("lyricvideo.gui.load_redo_inputs", lambda song_dir: (audio_path, "Angie"))
    monkeypatch.setattr("lyricvideo.gui.backup_song_outputs", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.gui.prepare_images_for_fresh_regeneration", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", lambda *a, **k: True)
    monkeypatch.setattr("lyricvideo.gui.threading.Thread", _ImmediateThread)

    calls = []
    button = SimpleNamespace(configure=lambda **kw: None)
    stub = _gui_stub(
        _running=False,
        redo_song_var=SimpleNamespace(get=lambda: "angie"),
        redo_new_images_var=SimpleNamespace(get=lambda: True),
        redo_easy_chord_var=SimpleNamespace(get=lambda: False),
        generate_button=button, redo_button=button, batch_button=button,
        status_var=SimpleNamespace(set=lambda v: None),
        progress_bar=SimpleNamespace(set=lambda v: None),
        _clear_log=lambda: None,
        _run_worker=lambda *a, **k: calls.append((a, k)),
        _poll_queue=lambda: None,
    )

    LyricVideoGUI._on_redo(stub)

    assert calls[0][1] == {"force_easy_chord": False, "fresh_images": True}


def test_run_worker_passes_fresh_images_through_to_run_pipeline(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "lyricvideo.gui.run_pipeline",
        lambda *a, **k: captured.setdefault("fresh_images", k["fresh_images"]) or Path("work/a/a.mp4"),
    )
    monkeypatch.setattr("lyricvideo.gui._maybe_upload_to_youtube", lambda work_dir, settings: None)
    monkeypatch.setattr("lyricvideo.gui.undismiss_song", lambda list_name, slug: None)
    stub = _gui_stub(_queue=queue.Queue(), settings=Settings())

    LyricVideoGUI._run_worker(stub, Path("a.mp3"), Path("work/a"), "A", "fetch_lyrics", fresh_images=True)

    assert captured["fresh_images"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py tests/test_gui.py -q -k "library or fresh_images or easy_chord_checkbox"`
Expected: FAIL (`run_pipeline() got an unexpected keyword argument 'fresh_images'`, missing `open_library_session`).

- [ ] **Step 3: Implement**

`lyricvideo/pipeline.py`:

1. After `from .layout import instrumental_image_captions` add:
```python
from .library_session import open_library_session
```
2. Signature: add `fresh_images: bool = False,` after `capo: int | None = None,`.
3. Extend the docstring (append before the closing `"""`, after the `capo` paragraph):
```
    `fresh_images` (owner, 2026-09-25) is Redo's "Generate new images": the shared image library is not consulted for
    this run (it would hand back the very pictures being replaced), though every picture bought is still filed into it."""
```
(remove the docstring's existing closing `"""` from the `capo` paragraph accordingly).
4. In the images stage, right after `backup_dirs = sorted(work_dir.glob("images_backup_*"))` add:
```python
        # The shared library of already-bought images (Settings.use_image_library; None whenever it is off or
        # unavailable, in which case every line below behaves exactly as before). Not wrapped in try/finally: on
        # an error the session is simply dropped and freed with the frame, same as every other per-run object.
        library = open_library_session(
            settings, song_slug=work_dir.name, song_title=song.title, images_dir=images_dir,
            fresh_images=fresh_images,
        )
```
5. Add `library=library,` to BOTH `get_or_generate_image(` calls (the per-line one and the instrumental-caption one) as an extra keyword after `previous_image=last_real_image,`.
6. Right after `substitute_fallback_images(image_paths)` (still inside the images stage) add:
```python
        if library is not None:
            print(library.summary_line())
            library.close()
```

`lyricvideo/gui.py`:

1. `_run_worker(...)`: add `fresh_images: bool = False,` after `force_easy_chord: bool = False,` and pass `fresh_images=fresh_images,` in its `run_pipeline(...)` call (after `settings=settings,`).
2. In `_on_redo`, change the thread's kwargs to `kwargs={"force_easy_chord": easy_chord, "fresh_images": generate_new_images},`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py tests/test_gui.py tests/test_imagery.py tests/test_library_session.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/pipeline.py lyricvideo/gui.py tests/test_pipeline.py tests/test_gui.py
git commit -m "Open a library session in the images stage; Redo's fresh images skip the lookup

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: The catch-up import (`image_library_import.py` + script)

**Files:**
- Create: `lyricvideo/image_library_import.py`
- Create: `scripts/import_image_library.py`
- Test: `tests/test_image_library_import.py`

**Interfaces:**
- Consumes: `ImageLibrary`, `content_id` (Task 2); `Embedder` (Task 3); `is_fallback_image` (`imagery`); `load_song`, `line_hash` (`models`); `instrumental_caption` (`layout`).
- Produces: `ImportSummary` (fields `seen, to_add, added, skipped_duplicate, skipped_placeholder, failed, text_recovered`), `find_image_files(work_dir) -> list[Path]`, `collect_source_texts(work_dir) -> tuple[dict[str, tuple[str, str]], dict[str, str]]` (`line_hash -> (text, song_title)`, `folder name -> song_title`), `import_images(library, embedder, work_dir, *, limit=None, dry_run=False, batch_size=32, progress=print) -> ImportSummary`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_image_library_import.py`:

```python
import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

from lyricvideo.image_library import EMBEDDING_DIM, ImageLibrary
from lyricvideo.image_library_import import collect_source_texts, find_image_files, import_images
from lyricvideo.layout import instrumental_caption
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, line_hash, save_song

BAD = b"not a png"


class _HashEmbedder:
    """A deterministic fake: each file's vector comes from its bytes; the 'not a png' file raises."""

    def __init__(self):
        self.calls = 0

    def embed_images(self, paths):
        self.calls += 1
        rows = []
        for path in paths:
            data = Path(path).read_bytes()
            if data == BAD:
                raise ValueError("cannot identify image file")
            rng = np.random.default_rng(int(hashlib.sha256(data).hexdigest()[:8], 16))
            rows.append(rng.standard_normal(EMBEDDING_DIM).astype(np.float32))
        return np.vstack(rows)


def _color_png(path: Path, color, size=(16, 16)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def _song(work: Path, slug: str, title: str, lines: list[str], chords=("C",)) -> Path:
    folder = work / slug
    folder.mkdir(parents=True, exist_ok=True)
    song = Song(
        title=title, audio_path="a.mp3",
        lines=[LyricLine(words=[Word(word=w, start_time=0.0, end_time=1.0) for w in text.split()],
                         start_time=0.0, end_time=1.0) for text in lines],
        chord_track=ChordTrack(events=[ChordEvent(0.0, 5.0, c) for c in chords]),
    )
    save_song(song, folder / "lyrics_timed.json")
    return folder


def _work(tmp_path) -> Path:
    work = tmp_path / "work"
    one = _song(work, "song-one", "Song One", ["hello there", "my friend"], chords=("C", "G"))
    _color_png(one / "images" / f"{line_hash('hello there')}.png", (255, 0, 0))
    _color_png(one / "images" / f"{line_hash('my friend')}.png", (0, 255, 0))
    _color_png(one / "images" / f"{line_hash(instrumental_caption('G'))}.png", (0, 0, 255))
    _color_png(one / "images" / "deadbeefdeadbeef.png", (9, 9, 9))                       # older lyric version: no text
    _color_png(one / "images_backup_2026" / f"{line_hash('hello there')}.png", (255, 0, 0))  # same picture as above
    _color_png(one / "easychords" / "images" / "copy.png", (1, 2, 3))                    # nested variant: a copy, skipped
    two = _song(work, "song-two", "Song Two", ["hello there"])
    _color_png(two / "images" / f"{line_hash('hello there')}.png", (250, 0, 0))            # same line, different picture
    _color_png(two / "images" / "placeholder.png", (30, 30, 40), size=(1920, 1080))       # the plain-colour fallback
    return work


def test_find_image_files_covers_images_and_backups_but_not_nested_variants(tmp_path):
    files = find_image_files(_work(tmp_path))

    names = {f.parent.name for f in files}
    assert names == {"images", "images_backup_2026"}
    assert all("easychords" not in str(f) for f in files)


def test_collect_source_texts_recovers_lyric_lines_and_instrumental_captions(tmp_path):
    texts, titles = collect_source_texts(_work(tmp_path))

    assert texts[line_hash("hello there")] == ("hello there", "Song One")
    assert texts[line_hash(instrumental_caption("G"))][0] == instrumental_caption("G")
    assert texts[line_hash(instrumental_caption(None))][0] == "[Instrumental]"
    assert titles == {"song-one": "Song One", "song-two": "Song Two"}


def test_import_adds_distinct_pictures_and_recovers_their_text(tmp_path):
    work = _work(tmp_path)
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert summary.seen == 7                    # song-one: 4 + 1 backup copy; song-two: 2 (the nested variant isn't walked)
    assert summary.skipped_placeholder == 1
    assert summary.skipped_duplicate == 1       # the backup copy of the red picture
    assert summary.added == 5 and library.count() == 5
    assert summary.text_recovered == 4          # every added image but the old-lyric-version one
    hello_there = library._db.execute("SELECT id FROM images WHERE source_text = 'hello there'").fetchall()
    assert len(hello_there) == 2                # the same line bought in two songs is two different pictures
    assert library.get(hello_there[0][0])["song_title"] in {"Song One", "Song Two"}


def test_a_second_import_adds_nothing(tmp_path):
    work = _work(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    again = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert again.added == 0 and again.skipped_duplicate == 6 and library.count() == 5


def test_dry_run_counts_but_never_embeds_or_writes(tmp_path):
    class _Boom:
        def embed_images(self, paths):
            raise AssertionError("a dry run must not embed anything")

    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _Boom(), _work(tmp_path), dry_run=True, progress=lambda *_: None)

    assert summary.to_add == 5 and summary.added == 0 and library.count() == 0


def test_limit_caps_how_many_are_added(tmp_path):
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), _work(tmp_path), limit=2, progress=lambda *_: None)

    assert summary.added == 2 and library.count() == 2


def test_a_corrupt_png_is_skipped_and_the_rest_still_import(tmp_path):
    work = _work(tmp_path)
    (work / "song-one" / "images" / "corrupt.png").write_bytes(BAD)
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert summary.failed == 1 and summary.added == 5 and library.count() == 5


def test_an_unreadable_lyrics_file_does_not_stop_the_import(tmp_path, capsys):
    work = _work(tmp_path)
    (work / "song-two" / "lyrics_timed.json").write_text("{ this is not json", encoding="utf-8")
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert summary.added == 5                       # every picture still imports, just without song two's own text
    assert "WARNING" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_image_library_import.py -q`
Expected: FAIL (`ModuleNotFoundError: lyricvideo.image_library_import`).

- [ ] **Step 3: Implement**

Create `lyricvideo/image_library_import.py`:

```python
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
```

Create `scripts/import_image_library.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_image_library_import.py -q`
Expected: PASS (with `seen == 7` per the note above).

- [ ] **Step 5: Real dry run against the owner's actual work folder (no model needed)**

Run: `.venv/bin/python scripts/import_image_library.py --dry-run`
Expected: `Seen ~8,5xx image files: would add ~8,3xx` (≈8,346 distinct pictures), placeholders 0. (The `seen`/`to_add` figures in the fixture-based tests above are for the tiny test tree only.)

- [ ] **Step 6: Commit**

```bash
git add lyricvideo/image_library_import.py scripts/import_image_library.py tests/test_image_library_import.py
git commit -m "Add the one-time catch-up import of already-bought images into the library

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: The calibration contact sheet (`library_preview.py` + script)

**Files:**
- Create: `lyricvideo/library_preview.py`
- Create: `scripts/preview_library_matches.py`
- Test: `tests/test_library_preview.py`

**Interfaces:**
- Consumes: `ImageLibrary`, `LibraryMatch`, `content_id` (Task 2); `Embedder` (Task 3); `build_image_prompt`, `summarize_song_gist` (`imagery`); `load_song`, `line_hash` (`models`); `instrumental_image_captions` (`layout`); `song_end_time` (`pipeline`).
- Produces: `THRESHOLDS: list[float]`, `PreviewRow(text, prompt, own_image, query, match)`, `cached_prompt_maker(cache_path, make_prompt) -> Callable[[str], str]` (with a `.calls` int attribute counting real `make_prompt` calls), `song_keys(song) -> list[str]`, `build_rows(song_dir, library, embedder, prompt_for) -> tuple[list[PreviewRow], set[str]]` (rows, the song's own picture ids), `reuse_counts(rows, library, own_ids, thresholds) -> list[tuple[float, int]]`, `write_report(out_dir, title, rows, counts, library, current_threshold) -> Path`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_library_preview.py`:

```python
import json
from pathlib import Path

import numpy as np
from PIL import Image

from lyricvideo.image_library import EMBEDDING_DIM, ImageLibrary, content_id
from lyricvideo.library_preview import (
    build_rows, cached_prompt_maker, reuse_counts, song_keys, write_report,
)
from lyricvideo.layout import instrumental_caption
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, line_hash, load_song, save_song


def _vec(*head):
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[: len(head)] = head
    return v


class _TextEmbedder:
    def __init__(self, mapping):
        self.mapping = mapping

    def embed_text(self, texts):
        return np.vstack([self.mapping[t] for t in texts])


def _png(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), color).save(path)
    return path


def _song_dir(tmp_path) -> Path:
    folder = tmp_path / "work" / "my-song"
    folder.mkdir(parents=True)
    lines = ["red door", "green field", "red door"]      # a repeated line is one picture, so one row
    song = Song(
        title="My <Song>", audio_path="a.mp3",
        lines=[LyricLine(words=[Word(word=w, start_time=float(i), end_time=i + 0.5) for w in text.split()],
                         start_time=float(i), end_time=i + 0.5) for i, text in enumerate(lines)],
        chord_track=ChordTrack(events=[ChordEvent(0.0, 30.0, "Am")]),
    )
    save_song(song, folder / "lyrics_timed.json")
    return folder


def test_song_keys_are_the_distinct_lines_then_the_instrumental_captions(tmp_path):
    song = load_song(_song_dir(tmp_path) / "lyrics_timed.json")

    keys = song_keys(song)

    assert keys[:2] == ["red door", "green field"]
    assert instrumental_caption("Am") in keys[2:]


def test_cached_prompt_maker_asks_once_per_text_even_across_runs(tmp_path):
    cache = tmp_path / "prompts.json"
    made = []
    first = cached_prompt_maker(cache, lambda text: made.append(text) or f"prompt for {text}")

    assert first("a") == "prompt for a" and first("a") == "prompt for a" and first.calls == 1

    second = cached_prompt_maker(cache, lambda text: made.append(text) or "SHOULD NOT BE CALLED")
    assert second("a") == "prompt for a" and second.calls == 0 and made == ["a"]
    assert json.loads(cache.read_text(encoding="utf-8")) == {"a": "prompt for a"}


def test_build_rows_excludes_the_songs_own_pictures_from_the_offers(tmp_path):
    song_dir = _song_dir(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    own = _png(song_dir / "images" / f"{line_hash('red door')}.png", (255, 0, 0))
    library.add(own, _vec(1, 0))                                          # the song's own picture, perfect match
    other = library.add(_png(tmp_path / "other.png", (0, 255, 0)), _vec(0.9, 0.3))
    embedder = _TextEmbedder({"p-red": _vec(1, 0), "p-green": _vec(0, 1), "p-inst": _vec(1, 0)})
    prompts = {"red door": "p-red", "green field": "p-green", instrumental_caption("Am"): "p-inst"}

    rows, own_ids = build_rows(song_dir, library, embedder, lambda text: prompts[text])

    assert own_ids == {content_id(own)}
    assert [r.text for r in rows][:2] == ["red door", "green field"]
    assert rows[0].own_image == own and rows[1].own_image is None
    assert rows[0].match.image_id == other          # never its own picture, even though that scored 1.0


def test_reuse_counts_apply_the_once_per_song_rule_at_each_threshold(tmp_path):
    song_dir = _song_dir(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    embedder = _TextEmbedder({"p1": _vec(1, 0), "p2": _vec(1, 0), "p3": _vec(0, 1)})
    prompts = {"red door": "p1", "green field": "p2", instrumental_caption("Am"): "p3"}
    rows, own_ids = build_rows(song_dir, library, embedder, lambda text: prompts[text])

    counts = reuse_counts(rows, library, own_ids, [0.5, 0.99])

    assert counts == [(0.5, 1), (0.99, 1)]      # p1 and p2 both want the single picture: only one line gets it


def test_write_report_makes_a_self_contained_page_with_escaped_text_and_thumbnails(tmp_path):
    song_dir = _song_dir(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    _png(song_dir / "images" / f"{line_hash('red door')}.png", (0, 0, 255))
    embedder = _TextEmbedder({"<b>p</b>": _vec(1, 0)})
    rows, own_ids = build_rows(song_dir, library, embedder, lambda text: "<b>p</b>")
    counts = reuse_counts(rows, library, own_ids, [0.3, 0.6])

    index = write_report(tmp_path / "out", "My <Song>", rows, counts, library, current_threshold=0.28)

    html_text = index.read_text(encoding="utf-8")
    assert index.name == "index.html"
    assert "My &lt;Song&gt;" in html_text and "&lt;b&gt;p&lt;/b&gt;" in html_text and "<b>p</b>" not in html_text
    assert "0.30" in html_text and "0.60" in html_text                   # the threshold table
    assert list((tmp_path / "out" / "thumbs").glob("*.jpg"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_library_preview.py -q`
Expected: FAIL (`ModuleNotFoundError: lyricvideo.library_preview`).

- [ ] **Step 3: Implement**

Create `lyricvideo/library_preview.py`:

```python
"""Calibration contact sheet for the shared image library (spec 2026-09-25, section 5).

For a song the owner already knows: what would the library have offered instead of each picture he paid for?
Claude writes each prompt exactly as the images stage would (cached, so trying other thresholds is free); the
song's OWN pictures are hidden from the library for the run (leave-one-out), so the sheet shows what a different
song's picture would have been. The owner judges it, and the default match score is set from what he accepts."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
from PIL import Image

from .image_library import ImageLibrary, LibraryMatch, content_id
from .layout import instrumental_image_captions
from .models import Song, line_hash, load_song

THRESHOLDS = [round(0.20 + 0.02 * i, 2) for i in range(9)]      # 0.20 ... 0.36
_THUMB_SIZE = (384, 216)


@dataclass
class PreviewRow:
    text: str
    prompt: str
    own_image: Path | None          # the picture actually bought for this line/caption, if it is still on disk
    query: np.ndarray               # the prompt's CLIP text embedding
    match: LibraryMatch | None      # the library's closest picture (this song's own pictures excluded), any score


def cached_prompt_maker(cache_path: Path, make_prompt: Callable[[str], str]) -> Callable[[str], str]:
    cache: dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def prompt_for(text: str) -> str:
        if text not in cache:
            prompt_for.calls += 1
            cache[text] = make_prompt(text)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        return cache[text]

    prompt_for.calls = 0
    return prompt_for


def song_keys(song: Song) -> list[str]:
    """The distinct lyric lines (first-seen order) then the instrumental captions the images stage would generate."""
    from .pipeline import song_end_time      # imported here: pipeline pulls in the whole heavy stack

    keys: list[str] = []
    for line in song.lines:
        if line.text not in keys:
            keys.append(line.text)
    for caption in instrumental_image_captions(song.lines, song.chord_track, song_end_time(song)):
        if caption not in keys:
            keys.append(caption)
    return keys


def build_rows(
    song_dir: Path, library: ImageLibrary, embedder, prompt_for: Callable[[str], str],
) -> tuple[list[PreviewRow], set[str]]:
    song = load_song(song_dir / "lyrics_timed.json")
    images_dir = song_dir / "images"
    own_ids = {content_id(p) for p in images_dir.glob("*.png")} if images_dir.is_dir() else set()
    keys = song_keys(song)
    prompts = [prompt_for(key) for key in keys]
    queries = embedder.embed_text(prompts) if prompts else []
    rows = []
    for key, prompt, query in zip(keys, prompts, queries):
        own = images_dir / f"{line_hash(key)}.png"
        rows.append(PreviewRow(
            text=key, prompt=prompt, own_image=own if own.exists() else None, query=query,
            match=library.best_match(query, own_ids, -1.0),
        ))
    return rows, own_ids


def reuse_counts(
    rows: Sequence[PreviewRow], library: ImageLibrary, own_ids: set[str], thresholds: Sequence[float],
) -> list[tuple[float, int]]:
    """How many lines the images stage would reuse a library picture for at each threshold, with the same
    once-per-song rule production applies (a picture serves one line only)."""
    counts = []
    for threshold in thresholds:
        used = set(own_ids)
        reused = 0
        for row in rows:
            match = library.best_match(row.query, used, threshold)
            if match is not None:
                used.add(match.image_id)
                reused += 1
        counts.append((threshold, reused))
    return counts


def _thumb(source: Path, dest: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail(_THUMB_SIZE)
        image.save(dest, "JPEG", quality=80)


def write_report(
    out_dir: Path, title: str, rows: Sequence[PreviewRow], counts: Sequence[tuple[float, int]],
    library: ImageLibrary, current_threshold: float,
) -> Path:
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    esc = html.escape
    table = "".join(
        f"<tr><td>{threshold:.2f}</td><td>{reused} of {len(rows)}</td></tr>" for threshold, reused in counts
    )
    body = []
    for n, row in enumerate(rows):
        own_cell = "<em>(file not on disk)</em>"
        if row.own_image is not None:
            _thumb(row.own_image, thumbs / f"own{n}.jpg")
            own_cell = f'<img src="thumbs/own{n}.jpg">'
        offer_cell = "<em>(library empty)</em>"
        verdict = ""
        if row.match is not None:
            _thumb(library.image_path(row.match.image_id), thumbs / f"lib{n}.jpg")
            offer_cell = f'<img src="thumbs/lib{n}.jpg"><br>score {row.match.score:.2f}'
            verdict = "reused" if row.match.score >= current_threshold else "would buy"
        body.append(
            f"<tr class='{verdict.replace(' ', '-')}'><td>{esc(row.text)}<div class='prompt'>{esc(row.prompt)}</div></td>"
            f"<td>{own_cell}</td><td>{offer_cell}</td><td>{esc(verdict)}</td></tr>"
        )
    page = f"""<!doctype html><meta charset="utf-8"><title>Library preview: {esc(title)}</title>
<style>
body {{ font: 14px system-ui, sans-serif; margin: 24px; background: #111; color: #eee; }}
table {{ border-collapse: collapse; }} td, th {{ border: 1px solid #333; padding: 6px 10px; vertical-align: top; }}
img {{ display: block; }} .prompt {{ color: #9aa; font-size: 12px; max-width: 420px; margin-top: 4px; }}
tr.reused td:last-child {{ color: #7ddc7d; }} tr.would-buy td:last-child {{ color: #f0a339; }}
</style>
<h1>{esc(title)}</h1>
<p>Left: the picture actually bought for that line. Right: the closest picture from the library (this song's own
pictures hidden). Current match score: <b>{current_threshold:.2f}</b> -- a line is "reused" when the score is at
least that.</p>
<h2>How many lines would reuse a library picture at each match score</h2>
<table><tr><th>match score</th><th>lines reused</th></tr>{table}</table>
<h2>Line by line</h2>
<table><tr><th>Line / prompt</th><th>You bought</th><th>Library offers</th><th></th></tr>{"".join(body)}</table>
"""
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    return index
```

Create `scripts/preview_library_matches.py`:

```python
"""Calibration contact sheet for the shared image library (owner reviews it before the feature is switched on).

  .venv/bin/python scripts/preview_library_matches.py <song-folder-name-or-path> [...] [--threshold 0.28]

For each song: Claude writes each line's image prompt exactly as the images stage would (a few cents' worth of the
same small call, cached in reports/library_preview/<song>/prompts.json so re-running is free), the song's own
pictures are hidden from the library, and an HTML page shows, per line, the picture you bought next to the library's
closest offer and its score -- plus how many lines would be reused at each match score. Needs the CLIP model already
downloaded (run scripts/import_image_library.py first) and ANTHROPIC_API_KEY in .env."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from dotenv import load_dotenv

from lyricvideo.clip_embedder import ClipEmbedder, EmbedderUnavailable
from lyricvideo.image_library import ImageLibrary
from lyricvideo.imagery import build_image_prompt, summarize_song_gist
from lyricvideo.library_preview import THRESHOLDS, build_rows, cached_prompt_maker, reuse_counts, write_report
from lyricvideo.models import load_song
from lyricvideo.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("songs", nargs="+", help="a folder name under work/, or a path to a song's work folder")
    parser.add_argument("--threshold", type=float, default=Settings().image_library_min_score)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports" / "library_preview")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(f"ANTHROPIC_API_KEY not found in {PROJECT_ROOT / '.env'}.")
    embedder = ClipEmbedder(allow_download=False)
    try:
        embedder.check_available()
    except EmbedderUnavailable as e:
        raise SystemExit(f"{e}")
    library = ImageLibrary()
    if library.count() == 0:
        raise SystemExit("The library is empty -- run scripts/import_image_library.py first.")
    client = anthropic.Anthropic()

    total_calls = 0
    for name in args.songs:
        song_dir = Path(name) if Path(name).is_dir() else PROJECT_ROOT / "work" / name
        song = load_song(song_dir / "lyrics_timed.json")
        out_dir = args.out / song_dir.name
        gist_for = cached_prompt_maker(out_dir / "gist.json", lambda _key: summarize_song_gist(
            client, "\n".join(line.text for line in song.lines)))
        gist = gist_for("gist")
        prompt_for = cached_prompt_maker(out_dir / "prompts.json", lambda text: build_image_prompt(client, gist, text))
        rows, own_ids = build_rows(song_dir, library, embedder, prompt_for)
        counts = reuse_counts(rows, library, own_ids, THRESHOLDS)
        index = write_report(out_dir, song.title, rows, counts, library, args.threshold)
        total_calls += gist_for.calls + prompt_for.calls
        print(f"{song.title}: {len(rows)} lines/captions -> {index}")
        print("  " + "  ".join(f"{t:.2f}: {n}" for t, n in counts))
    print(f"\nClaude calls made this run: {total_calls} (cached answers are free next time)")
    embedder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_library_preview.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/library_preview.py scripts/preview_library_matches.py tests/test_library_preview.py
git commit -m "Add the leave-one-out contact sheet used to calibrate library matching

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: Install, verify with the real model, run the import and the contact sheet, document, ship

**Files:**
- Modify: `requirements.txt`, `CLAUDE.md`, `docs/CLAUDE_HISTORY.md`, `docs/superpowers/specs/2026-09-25-shared-image-library-design.md` (one factual correction)
- No new tests (the opt-in real-model tests from Task 3 are exercised here).

- [ ] **Step 1: Requirements.** Append to `requirements.txt` (before `pytest`):

```
# Local CLIP model for the shared image library (image_library.py / clip_embedder.py): reuse an already-bought
# background image that fits a new lyric line instead of buying one from Replicate. Dry-run 2026-09-25 against
# this venv: adds only open_clip_torch, torchvision, timm, ftfy, regex, wcwidth -- torch/torchaudio/tensorflow
# untouched. The ~605 MB weights are NOT fetched by a pipeline run; scripts/import_image_library.py downloads them.
open_clip_torch>=3.0
```

- [ ] **Step 2: Install into the venv and prove nothing else broke.**

```bash
.venv/bin/python -m pip install open_clip_torch
.venv/bin/python -c "import torch, torchaudio, demucs, faster_whisper, crema, open_clip, tensorflow; print('imports ok', torch.__version__, open_clip.__version__)"
```
Expected: `imports ok 2.14.0... 3.x`. If any import fails: `pip uninstall` the six new packages, report, and stop the install path (the feature then stays inert; nothing else is affected).

- [ ] **Step 3: Download the weights and run the real-model tests.**

```bash
.venv/bin/python -c "from lyricvideo.clip_embedder import ClipEmbedder; print(ClipEmbedder(allow_download=True).check_available())"
.venv/bin/python -m pytest tests/test_clip_embedder.py -v
```
Expected: a path under `~/.cache/huggingface/hub/models--laion--CLIP-ViT-B-32-laion2B-s34B-b79K/...`; all 7 tests PASS (none skipped). If `open_clip` cannot load a `.safetensors` path, switch `WEIGHTS_FILENAME` to `open_clip_pytorch_model.bin` (also 605 MB, same repo) and rerun; record which one worked.

- [ ] **Step 4: Full test suite.** `.venv/bin/python -m pytest tests/ -q` — Expected: all green (the two documented Windows-only skips aside).

- [ ] **Step 5: Run the import for real (visible, foreground; resumable).**

`.venv/bin/python scripts/import_image_library.py`
Expected: progress lines, then a summary of about 8,3xx added. If it is interrupted or times out, re-run the same command — it resumes. Then `.venv/bin/python -m lyricvideo.image_library stats`.

- [ ] **Step 6: Build the contact sheet for three songs and look at it.** Pick three finished songs from `work/` with a normal amount of imagery (e.g. `free-bird`, `black-hole-sun`, `maggie-may`), run:

`.venv/bin/python scripts/preview_library_matches.py free-bird black-hole-sun maggie-may`

Open a few thumbnail pairs from `reports/library_preview/*/thumbs/` and describe honestly what the matches look like at the current threshold, and how many lines each threshold would reuse. **Do not flip `use_image_library` to True and do not change `image_library_min_score`** — the owner sets those from the sheet.

- [ ] **Step 7: Documentation.**
  - Correct the spec: in "Out of scope", replace `- \`deep_review/\` (it does not call the images stage).` with `- \`deep_review/\` needs no change: its redo goes through \`run_pipeline()\`, so it picks the library up automatically whenever the setting is on.`
  - `CLAUDE.md` is 39,960 bytes -- right at the 40K limit. In pipeline stage 6 ("images"), add a compact paragraph AND trim at least as many bytes of older, already-in-HISTORY prose elsewhere (check `wc -c CLAUDE.md` stays under 40,000): the shared library (`~/PlayAlongVideoProductionImages/`, `lyricvideo/image_library.py`/`clip_embedder.py`/`library_session.py`), the lookup order, once-per-song, never-blocks, `fresh_images`, the two Settings (off by default), `scripts/import_image_library.py`, `scripts/preview_library_matches.py`, `python -m lyricvideo.image_library stats`.
  - `docs/CLAUDE_HISTORY.md`: a `## 2026-09-25: Shared image library` entry — owner's request, the measured facts (8,435 images / $25.30 / prompts never saved / 8,346 distinct pictures), the decisions (copy not move, CLIP on the prompt, once per song, off until calibrated), the model download size, and the deep_review correction.

- [ ] **Step 8: Commit, push, release (standing instruction).**

```bash
git add requirements.txt CLAUDE.md docs/CLAUDE_HISTORY.md docs/superpowers/specs/2026-09-25-shared-image-library-design.md docs/superpowers/plans/2026-09-25-shared-image-library.md
git commit -m "Document the shared image library; require open_clip_torch

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
git push origin main
gh release list -R flyguy91355/LyricVideoGen-releases -L 3     # choose the next tag after the latest
scripts/cut_release.sh <next-tag> <notes-file>
```
The uncommitted `M VERSION` in the working tree is the owner's Apply-Update state — never stage it. The release notes say plainly that the library ships **off** and how to set it up (run the import script, look at the contact sheet, then tick "Reuse images from the library").
