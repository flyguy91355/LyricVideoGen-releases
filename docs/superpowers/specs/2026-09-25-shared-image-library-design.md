# Shared Image Library (reuse paid-for images) — Design

**Date:** 2026-09-25
**Status:** Design approved by owner in conversation; written spec awaiting owner review

## Purpose

Owner: "move (maybe copy — you tell me) all the images created from this program, along
with the prompt that created them, into a separate Play Along Video Production images
folder so the program can also use all the images I have already bought in the new songs
created — what the AI is looking for, or close. So look in the directory first before
buying one at Replicate."

Replicate has created **8,435** images for this program (its own account, read-only API
count, 2026-09-06 → 2026-09-22; 316 further predictions failed and produced nothing).
Replicate's pricing page lists `black-forest-labs/flux-schnell` at **$3.00 per thousand
output images**, so about **$25.30** so far, roughly 12 cents per finished song
(median 37 images/song across 215 songs). Every song's images live only in its own
`work/<song>/images/` folder, so each new song buys its pictures from scratch even when an
earlier song already has one that would do. In particular, 60 instrumental-chord captions
(e.g. `[Instrumental — chord: G]`) account for 1,552 image files — every song buys its own.

Success means: a new song reuses a library image whenever one fits "close enough", buys
only when nothing does, and the owner can see (and tune) how strict "close" is before it
goes live.

## Facts that shaped the design (all measured 2026-09-25)

- **Prompts were never saved.** `imagery.build_image_prompt()` asks Claude for a prompt
  per line, sends it to Replicate, and discards it. The only trace of an existing image
  is its filename: `line_hash(text)` of the lyric line / instrumental caption. Existing
  images therefore have no prompt to import; their source text is recoverable by
  re-hashing (6,098 of 6,578 distinct hashes match a lyric line, 60 more are instrumental
  captions; ~420 come from older lyric versions and have no recoverable text).
  **Consequence:** matching must key on what the *picture shows* (image embeddings), not on
  stored text.
- **Distinct pictures ≠ distinct hashes.** The same line in two songs is two different
  purchases with the same filename. On disk (each song's own `images/` only): 8,582 files,
  8,346 distinct picture contents. The library dedupes by picture *content*, not filename.
  (159 files are same-song copies from the "reuse previous image after failed
  generation" path. The import's own dry run on 2026-09-25, which also walks `images_backup_*/`, saw 8,599 files
  = 8,351 distinct pictures to add + 169 duplicates + **79 plain-colour placeholder files** (an earlier check that
  found "0" used a <4 KB size filter that a solid 1080p PNG exceeds; the import skips them via `is_fallback_image`).)
- **Copy, not move.** Render and Redo read `work/<song>/images/<hash>.png`; moving would
  break re-rendering existing songs. The whole set is under 1 GB.
- **Each image costs about a third of a cent**, so matching itself must cost ~nothing —
  no per-candidate Claude call.

## Scope

**In scope:**
1. `lyricvideo/image_library.py` — the store (SQLite index + PNG folder).
2. `lyricvideo/clip_embedder.py` — local CLIP wrapper (image and text embeddings, CPU).
3. Images-stage integration in `imagery.py` / `pipeline.py` — look in the library before
   buying, add every new purchase to it.
4. `scripts/import_image_library.py` — one-time, re-runnable import of the existing
   images.
5. `scripts/preview_library_matches.py` — the calibration/proof contact sheet.
6. Two Settings (`use_image_library`, `image_library_min_score`) with `SettingsPanel`
   controls.
7. A per-song log line and a small `stats` command showing reuse and estimated savings.

**Out of scope (deliberately):**
- Recovering the old prompts (impossible), or re-captioning old images with Claude
  vision (costs money; CLIP matching makes it unnecessary).
- Skipping the Claude prompt-writing call on a hit. Matching on the raw lyric would save
  it, but matches worse than matching on the prompt (which carries the song's theme).
  Revisit only if the prompt call proves to be a meaningful cost.
- Pruning/curating the library, a UI to browse it, sharing one library between machines
  (a collaborator's machine builds its own via the same import script).
- Any change to rendering, Ken Burns, crossfades, chord bar — a reused image is a plain
  PNG at `work/<song>/images/<hash>.png`, identical to a bought one.
- `deep_review/` needs no change: its redo goes through `run_pipeline()`, so it picks the library up
  automatically whenever the setting is on (corrected 2026-09-25 -- this line first said it never reaches the
  images stage, which was wrong).

## Design

### 1. The library

Location: `image_library_dir()` → env `PLAYALONG_IMAGE_LIBRARY` if set (tests, unusual
setups), else `Path.home() / "PlayAlongVideoProductionImages"` (outside the repo, never in
git, untouched by Update Available's allow-list copy, works on Linux and Windows).

```
PlayAlongVideoProductionImages/
  images/<id>.png        # id = sha256 of the PNG bytes (hex, first 32 chars)
  library.db             # SQLite, WAL mode
```

`library.db` tables:

- `images(id PK, prompt, source_text, song_title, embedding BLOB, added_at)` —
  `embedding` is the L2-normalized float32[512] CLIP image embedding (cosine similarity =
  dot product). `prompt` is empty for imported images and the real Claude-written prompt
  for every image bought from now on. `source_text` is the lyric line / instrumental
  caption the image was made for, when known.
- `reuses(image_id, song_slug, reused_at)` — one row per reuse, so `stats` can report
  real savings instead of guessing.

`ImageLibrary` (the only code that touches the DB and folder):
`open()`, `add(png_path, prompt, source_text, song_title, embedding) -> id` (copy to a
temp name then atomic rename, then insert; a duplicate content id is a no-op),
`load_matrix() -> (ids, np.ndarray)` (all embeddings once per images stage — ~8.3K × 512
float32 ≈ 17 MB), `best_match(query_embedding, exclude_ids, min_score) -> (id, score) | None`,
`copy_to(id, dest_path)`, `record_reuse(id, song_slug)`, `stats()`. SQLite (stdlib) rather
than JSON so two processes (a GUI Batch and a CLI run) can add images without corrupting
each other.

### 2. The embedder

`ClipEmbedder` wraps `open_clip` with model `ViT-B-32`, pretrained `laion2b_s34b_b79k`
(MIT-licensed weights; open_clip README's own example model; ~0.2 B parameters, so the
download is on the order of 0.6 GB — exact size confirmed at download). Forced to CPU
(the GT 1030's 2 GB is not spared for it, and CPU is fast enough for a few hundred
short strings or several thousand thumbnails). Interface:

```python
class Embedder(Protocol):
    def embed_images(self, paths: list[Path]) -> np.ndarray: ...   # (n, 512) normalized
    def embed_text(self, texts: list[str]) -> np.ndarray: ...      # (n, 512) normalized
```

Tests inject a fake `Embedder`; the real one is never loaded in unit tests.

- `open_clip` and `torch` are imported lazily inside the class, so the GUI and every
  other stage start exactly as fast as today.
- **A pipeline run never downloads the model.** It loads offline (`HF_HUB_OFFLINE`);
  if the model is not already cached it logs one line ("Image library: model not
  installed — run scripts/import_image_library.py once; buying images as usual") and
  proceeds normally. Only the import script, which the owner starts, downloads it.
- Loaded at the start of the images stage and released at its end (a Batch already
  calls `release_memory()` after each song); ~a few seconds per song.
- CLIP's text tower reads at most 77 tokens; the tokenizer truncates longer prompts. The
  Claude prompt is written subject-first, so the informative part survives. Verify the
  truncation behaviour in the implementation, not assumed.

### 3. Images-stage flow

`get_or_generate_image()` gains one optional keyword, `library: LibrarySession | None =
None`. `None` (every existing caller and test) reproduces today's behaviour exactly.

`LibrarySession` (built once per images stage in `run_pipeline`) holds the loaded matrix,
the embedder, the song slug, `min_score`, the set of **used ids**, and counters
(`reused`, `bought`). Per requested image key:

1. `work/<song>/images/<hash>.png` exists → use it. *(unchanged)*
2. `extra_cache_dirs` (`images_backup_*`) hit → use it. *(unchanged)*
3. Claude writes the prompt (`build_image_prompt`, unchanged). The library is consulted
   **once, with the first prompt that builds successfully**; a later retry's new prompt
   is not re-matched.
4. Embed the prompt → `best_match(..., exclude_ids=session.used_ids, min_score)`. On a hit:
   copy the library file to `work/<song>/images/<hash>.png` (same write the
   `extra_cache_dirs` path does), add the id to `used_ids`, `record_reuse`, count it, and
   return — **no Replicate call**.
5. On a miss: generate exactly as today (same 3-attempt retry, same
   previous-image/plain-color fallbacks). After success (not a fallback — checked with
   `is_fallback_image`) add the new image with its **real prompt** and `source_text` to
   the library, add its id to `used_ids`, count it as bought.

**Once per song:** a library image serves at most one distinct line/caption per song,
including images this song just bought. `used_ids` is seeded at session start by hashing
the PNGs already in `work/<song>/images/` (so a resumed run cannot re-pick a picture the
earlier run already used). Identical repeated lines still share one image, as today,
because step 1 catches them by hash before the library is consulted.

**Never blocks a song.** Any library/embedder error — missing library, missing model,
corrupt row, embedding failure — is caught at the session boundary, logged as a warning in
the style of `imagery.py`'s existing warnings, disables the library for the remainder of
that song, and the stage buys as it does today. An empty library is simply all misses.

**Redo with "Generate new images" checked** must not re-match the very pictures the owner
asked to replace. `run_pipeline()` gains `fresh_images: bool = False`; the GUI's existing
Redo branch (`prepare_images_for_fresh_regeneration`) passes `True`, which skips the
library *lookup* (purchases still go *into* the library). Everything else is unchanged.

**Visibility:** at the end of the images stage, one line — e.g. `Image library: 14 reused,
22 bought (about $0.04 saved at $0.003/image)`. The `$0.003` is one named constant beside a
comment citing Replicate's pricing page and date.

### 4. Import of existing images (`scripts/import_image_library.py`)

Run once by the owner (he starts runs; nothing launches in the background):
`.venv/bin/python scripts/import_image_library.py [--limit N] [--dry-run]`.

1. Downloads/loads the model (the only place a download is allowed).
2. Walks `work/*/images/*.png` and `work/*/images_backup_*/*.png`. Nested
   `work/<slug>/easychords/images/` and `images_prior_*` are skipped (copies).
3. For each file: content id; skip if already in the library; skip if
   `is_fallback_image`.
4. Recovers `source_text` by re-hashing: every lyric line of every `lyrics_timed.json`
   (via `load_song()`, `LyricLine.text`) plus `instrumental_caption(label)` for every
   distinct chord label in those songs' chord tracks plus the generic
   `[Instrumental]`. Unmatched images are imported anyway with empty text.
5. Embeds in batches on CPU, commits every batch (interrupt-safe), prints progress.
6. Ends with a summary: files seen / added / skipped-duplicate / skipped-placeholder /
   text recovered.

Idempotent and re-runnable, so images from songs finished later can be picked up too.
New purchases are added automatically by the images stage, so this is a *catch-up*
tool, not a required step after every song. `--dry-run` reports counts without
embedding (no model needed).

### 5. Calibration and proof (`scripts/preview_library_matches.py`)

Owner's standing rule (see memory: validate a new measure on songs he has already
judged before anything is turned on): **the feature ships off (`use_image_library`
default False) until the owner has reviewed this report.**

`preview_library_matches.py <song-folder> [...]`:

- For each song, for each distinct line and instrumental caption: Claude writes the prompt
  exactly as production would (one small call per line; **cached to a JSON file so
  re-running at other thresholds is free**, and the spend is reported at the end).
- The song's **own** pictures (its `images/` files' content ids) are excluded from the
  library for that run — leave-one-out — so the sheet shows what a *different* song's
  picture would have been offered.
- Output: an HTML contact sheet under `reports/` (already gitignored): per row the line,
  the prompt, the picture that was actually bought, the library's best pick, and the score;
  plus a summary table "at min-score T, X of N lines would be reused" across a range of T.
- The owner judges the sheet; the default `image_library_min_score` (and the slider's
  range) are set from what he accepts, then `use_image_library` is flipped to default True
  in a follow-up commit.

### 6. Settings

`Settings` gains (defaults reproduce today's behaviour until calibrated):

- `use_image_library: bool = False`
- `image_library_min_score: float = 0.34` — raw CLIP cosine similarity, never a made-up
  percent, so the preview sheet's numbers and the slider agree. **Provisional**, sitting inside the
  disabled feature; the owner's review of the section-5 contact sheet sets the final value. (First
  written as 0.28; the 2026-09-25 contact sheets for Free Bird, Black Hole Sun and Maggie May showed
  0.28 and below giving clearly wrong pictures, while 0.33-0.40 looked good, so the placeholder was
  moved to 0.34 and the slider widened to 0.15-0.45.)

`SettingsPanel` gets a checkbox ("Reuse library images") and a slider ("Library match
strictness") in the existing image section beside "Minimum image hold"/"Crossfade
length", following that block's `_slider()` pattern (typeable value box, default text,
dirty marker, itemized Save confirm). Labels stay short (see `_add()`'s warning).
`run_pipeline()` reads both when building its session, the same way it unpacks
`detect_kwargs`; `Settings.render_kwargs()`/`assemble_video()` are untouched.
`settings.json` files without the new keys load with the defaults, as for every earlier
field.

### 7. Dependencies

`requirements.txt` gains `open_clip_torch` (dry-run 2026-09-25 against the live venv:
would add only `open_clip_torch`, `torchvision`, `timm`, `ftfy`, `regex`, `wcwidth`;
torch/torchaudio/tensorflow untouched). **No venv change is made until the owner approves
it** at implementation time, and a torch/demucs/crema import check follows the install.
Update Available reinstalls dependencies when `requirements.txt` changes, so a
collaborator's checkout picks the package up; the model download still happens only via
their own run of the import script.

## Testing

Unit tests, no network, no real model (matching the repo's inject-the-client convention):

- `ImageLibrary`: add / dedupe by content / `best_match` threshold and `exclude_ids` /
  atomic copy / two-handle concurrent add / `stats`.
- `get_or_generate_image` with a session and a fake embedder: hit copies the file and
  makes **no** Replicate call; miss buys and adds the image with its prompt; once-per-song
  (second line cannot take the same id; a just-bought id is excluded); resume seeds
  `used_ids`; library raising → falls through to a normal purchase; `library=None`
  unchanged (existing `test_imagery.py` passes untouched).
- `fresh_images=True` skips the lookup but still adds purchases.
- Import script on a temp work tree of tiny PNGs: text recovery, placeholder skip,
  duplicate skip, idempotent second run, `--dry-run` embeds nothing.
- Settings round-trip for the new fields, old `settings.json` loads; `SettingsPanel`
  controls per `test_settings_panel.py`'s patterns.
- One slow, opt-in test with the real CLIP model that self-skips when the model is not
  cached (like the align tests' environment skips).

## Risks and mitigations

- **A reused picture is a false match** (wrong mood/subject). Mitigated by the calibrated
  threshold, the once-per-song rule, and the preview sheet; the owner controls strictness.
- **Visual repetition across videos.** Accepted by the owner ("or close"); a stricter
  threshold reduces it.
- **Library grows without bound.** ~80-100 KB per picture; even 50,000 images is ~5 GB.
  Pruning is out of scope until it matters.
- **Absolute CLIP scores are fuzzy** (they vary by prompt style). That is why the threshold
  is set from real Claude-written prompts on real songs, not from a textbook value.
- **Model/torch issues on a machine** never stop a song (see "Never blocks a song").
