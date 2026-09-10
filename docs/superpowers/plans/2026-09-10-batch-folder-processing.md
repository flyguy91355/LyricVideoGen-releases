# Batch Folder Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner point the GUI at a folder of audio files and have every
song in it run through the pipeline sequentially, instead of one file at a time.

**Architecture:** A new `lyricvideo/batch.py` finds audio files in a folder and
resolves each one's title/work-dir/already-done status (cheap, no API spend) ahead
of running anything. `gui.py` gets a new "Batch: Process a Folder" section that
reuses the existing status label, progress bar, and log console rather than
duplicating them, plus a single up-front confirmation when some songs in the
folder are already done (skip vs. regenerate, applied batch-wide).

**Tech Stack:** Python 3.12, CustomTkinter, pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-batch-folder-processing-design.md`

## Global Constraints

- Sequential processing only — never concurrent/parallel file processing (keeps
  Anthropic/Replicate spend and Demucs CPU load bounded exactly like today's
  one-at-a-time flow).
- One folder's immediate files only — no recursive subfolder scanning.
- No per-file review/editing before a batch runs — fully automatic, matching how
  a single Generate already behaves when the title is left blank.
- A file that errors is logged and skipped — never aborts the rest of the batch.
- Every `Path.read_text()`/`.write_text()` call passes `encoding="utf-8"`
  explicitly (existing codebase convention).
- Run `cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/ -v`
  after every task; all tests must pass before committing. The pre-commit hook
  blocks any commit touching a `.py`/`.sh` file unless `CLAUDE.md` is also staged
  with a real, accurate change.

---

## Task 1: batch.py — find and resolve audio files in a folder

**Files:**
- Create: `lyricvideo/batch.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: `lyricvideo.identify.extract_metadata(path: Path) -> SongInfo` (has a
  `.title` attribute; existing, unchanged), `lyricvideo.pipeline.slugify(title: str)
  -> str` (existing, unchanged).
- Produces: `find_audio_files(folder: Path) -> list[Path]`, `BatchItem` frozen
  dataclass (`audio_path: Path`, `title: str`, `work_dir: Path`, `already_done:
  bool`), `resolve_batch_items(files: list[Path], work_root: Path) -> list[BatchItem]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_batch.py`:

```python
from pathlib import Path

from lyricvideo.batch import BatchItem, find_audio_files, resolve_batch_items


def test_find_audio_files_filters_by_extension(tmp_path):
    (tmp_path / "song1.mp3").write_bytes(b"")
    (tmp_path / "song2.wav").write_bytes(b"")
    (tmp_path / "song3.m4a").write_bytes(b"")
    (tmp_path / "song4.flac").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    (tmp_path / "cover.jpg").write_bytes(b"")

    found = find_audio_files(tmp_path)

    assert [f.name for f in found] == ["song1.mp3", "song2.wav", "song3.m4a", "song4.flac"]


def test_find_audio_files_sorted_alphabetically(tmp_path):
    (tmp_path / "zebra.mp3").write_bytes(b"")
    (tmp_path / "apple.mp3").write_bytes(b"")
    (tmp_path / "mango.mp3").write_bytes(b"")

    found = find_audio_files(tmp_path)

    assert [f.name for f in found] == ["apple.mp3", "mango.mp3", "zebra.mp3"]


def test_find_audio_files_ignores_subfolders(tmp_path):
    (tmp_path / "top.mp3").write_bytes(b"")
    subfolder = tmp_path / "subfolder"
    subfolder.mkdir()
    (subfolder / "nested.mp3").write_bytes(b"")

    found = find_audio_files(tmp_path)

    assert [f.name for f in found] == ["top.mp3"]


def test_find_audio_files_empty_folder_returns_empty_list(tmp_path):
    assert find_audio_files(tmp_path) == []


def test_resolve_batch_items_resolves_title_and_work_dir(tmp_path, monkeypatch):
    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": "Some Song"})(),
    )

    items = resolve_batch_items([audio], work_root)

    assert items == [
        BatchItem(audio_path=audio, title="Some Song", work_dir=work_root / "some-song", already_done=False)
    ]


def test_resolve_batch_items_already_done_true_when_video_exists(tmp_path, monkeypatch):
    audio = tmp_path / "some-song.mp3"
    audio.write_bytes(b"")
    work_root = tmp_path / "work"
    finished_dir = work_root / "some-song"
    finished_dir.mkdir(parents=True)
    (finished_dir / "some-song.mp4").write_bytes(b"fake video")

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": "Some Song"})(),
    )

    items = resolve_batch_items([audio], work_root)

    assert items[0].already_done is True


def test_resolve_batch_items_falls_back_to_filename_stem_on_extraction_failure(tmp_path, monkeypatch):
    """A file whose metadata extraction raises still gets a usable, UNIQUE
    identity (its own filename stem) -- not a shared constant like "" or
    "untitled" that would collide with every other failing file in the same
    batch (caught during the design spec's own self-review, 2026-09-10)."""
    audio1 = tmp_path / "broken-one.mp3"
    audio2 = tmp_path / "broken-two.mp3"
    audio1.write_bytes(b"")
    audio2.write_bytes(b"")
    work_root = tmp_path / "work"

    def raise_error(path):
        raise RuntimeError("no tags")

    monkeypatch.setattr("lyricvideo.batch.extract_metadata", raise_error)

    items = resolve_batch_items([audio1, audio2], work_root)

    assert items[0].title == "broken-one"
    assert items[1].title == "broken-two"
    assert items[0].work_dir != items[1].work_dir
    assert items[0].already_done is False
    assert items[1].already_done is False


def test_resolve_batch_items_preserves_input_order(tmp_path, monkeypatch):
    audio_a = tmp_path / "a.mp3"
    audio_b = tmp_path / "b.mp3"
    audio_a.write_bytes(b"")
    audio_b.write_bytes(b"")
    work_root = tmp_path / "work"

    monkeypatch.setattr(
        "lyricvideo.batch.extract_metadata",
        lambda path: type("Info", (), {"title": path.stem.upper()})(),
    )

    items = resolve_batch_items([audio_a, audio_b], work_root)

    assert [i.audio_path for i in items] == [audio_a, audio_b]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_batch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.batch'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/batch.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_batch.py -v`
Expected: 7 passed

- [ ] **Step 5: Add a minimal CLAUDE.md note and commit**

```
`lyricvideo/batch.py` (new) finds audio files in a folder and resolves each
one's title/work-dir/already-done status -- not yet wired into gui.py (in
progress; see docs/superpowers/plans/2026-09-10-batch-folder-processing.md).
```

```bash
git add lyricvideo/batch.py tests/test_batch.py CLAUDE.md
git commit -m "Add batch.py: find and resolve audio files in a folder"
```

---

## Task 2: gui.py — the Batch: Process a Folder section

This is the full end-to-end wiring: a new section (folder picker + Start button),
a background thread that resolves the folder's files, a confirmation dialog when
some are already done, a sequential per-file worker that reuses the existing
status/progress bar/log console, and a completion summary. Widget wiring stays
manually/visually verified (this project's existing testing-constraint precedent
for GUI code) — Step 6 below is a mandatory real manual smoke test.

**Files:**
- Modify: `lyricvideo/gui.py`

**Interfaces:**
- Consumes: `lyricvideo.batch.find_audio_files`/`resolve_batch_items`/`BatchItem`
  (Task 1), `lyricvideo.pipeline.run_pipeline`/`backup_song_outputs`/`slugify`
  (existing), `lyricvideo.pipeline.STAGES` (existing, for combined progress).
- Produces: no new public functions — `LyricVideoGUI`'s batch behavior is what
  Step 6's manual smoke test verifies.

- [ ] **Step 1: Add the imports**

In `lyricvideo/gui.py`, find:

```python
from .identify import extract_metadata
from .pipeline import (
    STAGES,
    run_pipeline,
    slugify as _slugify,
    list_redoable_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
)
```

Replace with:

```python
from .batch import find_audio_files, resolve_batch_items
from .identify import extract_metadata
from .pipeline import (
    STAGES,
    run_pipeline,
    slugify as _slugify,
    list_redoable_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
)
```

- [ ] **Step 2: Add batch state and the folder/Start Batch section**

Find (in `__init__`):

```python
        self.redo_song_var = tk.StringVar()
        self.redo_new_images_var = tk.BooleanVar(value=False)
```

Replace with:

```python
        self.redo_song_var = tk.StringVar()
        self.redo_new_images_var = tk.BooleanVar(value=False)
        self.batch_folder_var = tk.StringVar()
        self._batch_items: list = []  # list[BatchItem] once resolved
        self._batch_index = 0
        self._batch_results = {"succeeded": [], "skipped_already_done": [], "failed": []}
```

Find (right after the Redo section, before the log widget):

```python
        self.redo_button = ctk.CTkButton(redo_controls, text="Redo", command=self._on_redo, width=80)
        self.redo_button.pack(side="left", padx=8)

        self.log_widget = ctk.CTkTextbox(left, state="disabled", wrap="word")
```

Replace with:

```python
        self.redo_button = ctk.CTkButton(redo_controls, text="Redo", command=self._on_redo, width=80)
        self.redo_button.pack(side="left", padx=8)

        batch_frame = ctk.CTkFrame(left)
        batch_frame.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(batch_frame, text="Batch: Process a Folder", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        batch_controls = ctk.CTkFrame(batch_frame, fg_color="transparent")
        batch_controls.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkEntry(batch_controls, textvariable=self.batch_folder_var, width=340, state="readonly").pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(batch_controls, text="Browse Folder...", command=self._on_browse_batch_folder, width=110).pack(
            side="left", padx=8
        )
        self.batch_button = ctk.CTkButton(batch_controls, text="Start Batch", command=self._on_start_batch, width=90)
        self.batch_button.pack(side="left", padx=8)

        self.log_widget = ctk.CTkTextbox(left, state="disabled", wrap="word")
```

- [ ] **Step 3: Add the batch handler methods**

Find (right after `_on_new_song`, before `_on_generate`):

```python
    def _on_new_song(self) -> None:
        if self._running:
            return
        self.title_var.set("")
        self.audio_var.set("")
        self.work_dir_var.set("")  # after title_var -- overrides its own auto-fill trace
        self.status_var.set("Ready")
        self.progress_bar.set(0.0)
        self._clear_log()

    def _on_generate(self) -> None:
```

Replace with:

```python
    def _on_new_song(self) -> None:
        if self._running:
            return
        self.title_var.set("")
        self.audio_var.set("")
        self.work_dir_var.set("")  # after title_var -- overrides its own auto-fill trace
        self.status_var.set("Ready")
        self.progress_bar.set(0.0)
        self._clear_log()

    def _on_browse_batch_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.batch_folder_var.set(folder)

    def _on_start_batch(self) -> None:
        if self._running:
            return
        folder = self.batch_folder_var.get().strip()
        if not folder:
            messagebox.showerror("No folder selected", "Choose a folder to batch-process first.")
            return

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.status_var.set("Scanning folder...")
        self._clear_log()

        thread = threading.Thread(target=self._resolve_batch_worker, args=(Path(folder),), daemon=True)
        thread.start()
        self.root.after(100, self._poll_queue)

    def _resolve_batch_worker(self, folder: Path) -> None:
        try:
            files = find_audio_files(folder)
            items = resolve_batch_items(files, PROJECT_ROOT / "work")
            self._queue.put(("batch_resolved", items))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

    def _start_batch_run(self, items: list) -> None:
        self._batch_items = items
        self._batch_index = 0
        self._batch_results = {"succeeded": [], "skipped_already_done": [], "failed": []}
        self.status_var.set(f"Starting batch (0/{len(items)})...")
        self.progress_bar.set(0.0)

        thread = threading.Thread(target=self._run_batch_worker, args=(items,), daemon=True)
        thread.start()
        self.root.after(100, self._poll_queue)

    def _run_batch_worker(self, items: list) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        results = {"succeeded": [], "skipped_already_done": [], "failed": []}
        try:
            for index, item in enumerate(items, start=1):
                self._queue.put(("batch_file_start", (index, len(items), item.title)))
                try:
                    if item.already_done:
                        backup_song_outputs(item.work_dir, _slugify(item.title))
                        run_pipeline(
                            item.audio_path, item.work_dir, item.title,
                            start_stage="fetch_lyrics", settings=self.settings,
                            progress_callback=lambda stage: self._queue.put(("stage", stage)),
                        )
                    else:
                        run_pipeline(
                            item.audio_path, item.work_dir, item.title,
                            settings=self.settings,
                            progress_callback=lambda stage: self._queue.put(("stage", stage)),
                        )
                    results["succeeded"].append(item.title)
                except Exception as e:
                    results["failed"].append((item.title, f"{type(e).__name__}: {e}"))
                    print(f"Batch item {item.title!r} failed: {type(e).__name__}: {e}")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        self._queue.put(("batch_done", results))

    def _on_generate(self) -> None:
```

- [ ] **Step 4: Wire the new queue message kinds into `_poll_queue`**

Find:

```python
        for kind, payload in items:
            if kind == "log":
                log_chunks.append(payload)
                continue
            flush_log()
            if kind == "stage":
                self.status_var.set(f"Stage: {payload}")
                # +1: report("done") isn't a real STAGES entry, but seeing the
                # bar reach 100% only once done fires (not at the start of the
                # last real stage) reads better than stalling at 6/7.
                try:
                    fraction = (STAGES.index(payload) + 1) / len(STAGES)
                except ValueError:
                    fraction = self.progress_bar.get()
                self.progress_bar.set(min(1.0, fraction))
            elif kind == "done":
                self.status_var.set("Done")
                self.progress_bar.set(1.0)
                self._running = False
                self.generate_button.configure(state="normal")
                self.redo_button.configure(state="normal")
                messagebox.showinfo("Video ready", f"Wrote {payload}")
                return
            elif kind == "error":
                self.status_var.set("Failed")
                self._running = False
                self.generate_button.configure(state="normal")
                self.redo_button.configure(state="normal")
                self._append_log(f"\nERROR:\n{payload}\n")
                messagebox.showerror("Generation failed", payload.splitlines()[0])
                return
        flush_log()
```

Replace with:

```python
        for kind, payload in items:
            if kind == "log":
                log_chunks.append(payload)
                continue
            flush_log()
            if kind == "stage":
                if self._batch_items:
                    self.status_var.set(f"File {self._batch_index}/{len(self._batch_items)}: Stage: {payload}")
                    try:
                        stage_fraction = (STAGES.index(payload) + 1) / len(STAGES)
                    except ValueError:
                        stage_fraction = 0.0
                    combined = (self._batch_index - 1 + stage_fraction) / len(self._batch_items)
                    self.progress_bar.set(min(1.0, combined))
                else:
                    self.status_var.set(f"Stage: {payload}")
                    # +1: report("done") isn't a real STAGES entry, but seeing the
                    # bar reach 100% only once done fires (not at the start of the
                    # last real stage) reads better than stalling at 6/7.
                    try:
                        fraction = (STAGES.index(payload) + 1) / len(STAGES)
                    except ValueError:
                        fraction = self.progress_bar.get()
                    self.progress_bar.set(min(1.0, fraction))
            elif kind == "done":
                self.status_var.set("Done")
                self.progress_bar.set(1.0)
                self._running = False
                self.generate_button.configure(state="normal")
                self.redo_button.configure(state="normal")
                self.batch_button.configure(state="normal")
                messagebox.showinfo("Video ready", f"Wrote {payload}")
                return
            elif kind == "error":
                self.status_var.set("Failed")
                self._running = False
                self.generate_button.configure(state="normal")
                self.redo_button.configure(state="normal")
                self.batch_button.configure(state="normal")
                self._append_log(f"\nERROR:\n{payload}\n")
                messagebox.showerror("Generation failed", payload.splitlines()[0])
                return
            elif kind == "batch_resolved":
                self._on_batch_resolved(payload)
                return
            elif kind == "batch_file_start":
                self._batch_index, total, title = payload
                self.status_var.set(f"File {self._batch_index}/{total}: {title}")
                self.progress_bar.set(min(1.0, (self._batch_index - 1) / total))
            elif kind == "batch_done":
                self._on_batch_done(payload)
                return
        flush_log()
```

- [ ] **Step 5: Add `_on_batch_resolved` and `_on_batch_done`**

Find (right after `_run_batch_worker`, i.e. right before `def _on_generate`):

Add these two new methods in the same place, right after `_run_batch_worker` (so
directly before the `def _on_generate(self) -> None:` line already located in
Step 3 above):

```python
    def _on_batch_resolved(self, items: list) -> None:
        if not items:
            self._running = False
            self.generate_button.configure(state="normal")
            self.redo_button.configure(state="normal")
            self.batch_button.configure(state="normal")
            self.status_var.set("Ready")
            messagebox.showinfo("No audio files found", "That folder has no .mp3/.wav/.m4a/.flac files.")
            return

        done_count = sum(1 for i in items if i.already_done)
        to_process = items
        if done_count:
            skip_done = messagebox.askyesno(
                "Some songs already done",
                f"{done_count} of {len(items)} songs already have a finished video.\n\n"
                "Skip those and only process the rest? (No = regenerate everyone, "
                "backing up each one's current video/timing first, same as Redo.)",
            )
            if skip_done:
                to_process = [i for i in items if not i.already_done]

        if not to_process:
            self._running = False
            self.generate_button.configure(state="normal")
            self.redo_button.configure(state="normal")
            self.batch_button.configure(state="normal")
            self.status_var.set("Ready")
            messagebox.showinfo("Nothing to do", "Every song in that folder is already done.")
            return

        self._start_batch_run(to_process)

    def _on_batch_done(self, results: dict) -> None:
        self._batch_items = []
        self._batch_index = 0
        self.status_var.set("Batch done")
        self.progress_bar.set(1.0)
        self._running = False
        self.generate_button.configure(state="normal")
        self.redo_button.configure(state="normal")
        self.batch_button.configure(state="normal")

        summary = (
            f"Succeeded: {len(results['succeeded'])}\n"
            f"Failed: {len(results['failed'])}"
        )
        if results["failed"]:
            failed_names = "\n".join(f"  - {title}: {err}" for title, err in results["failed"])
            messagebox.showwarning("Batch finished with failures", f"{summary}\n\n{failed_names}")
        else:
            messagebox.showinfo("Batch finished", summary)
```

- [ ] **Step 6: Run the full test suite, then a mandatory manual smoke test**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: every test passes, including the unchanged `test_gui.py`.

```bash
cd /home/doug/PlayAlongVideoProduction && ./run_playalongvideoproduction.sh
```

Confirm, by actually looking at the window and using it:
- The "Batch: Process a Folder" section appears below Redo, with a folder field,
  "Browse Folder...", and "Start Batch".
- Point it at a real folder with 2+ audio files (at least one you've already
  generated before, so `already_done` is exercised) and click Start Batch.
- The scan happens, then (if any are already done) the skip/regenerate dialog
  appears with the right counts; picking "skip" only processes the new ones.
- The status label shows "File N/M: <title>: Stage: ..." and the progress bar
  advances smoothly across the whole batch, not just one file.
- Every file that finishes updates results; a completion dialog reports
  succeeded/failed counts.
- Generate/Redo/Start Batch buttons are disabled while a batch runs and
  re-enabled afterward.

If anything above doesn't hold, fix it and re-run this whole step before moving
on — this task is not done until the real window is confirmed correct by eye.

- [ ] **Step 7: Update the CLAUDE.md in-progress note and commit**

```bash
git add lyricvideo/gui.py CLAUDE.md
git commit -m "Add Batch: Process a Folder to the GUI"
```

---

## Task 3: Final CLAUDE.md rewrite and full-suite verification

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Rewrite the GUI description to describe the finished batch feature**

Find (in the "Running it" section's GUI bullet, right after the New Song
sentence added by the earlier plan):

```
  a required input — then click Generate. A "New Song" button next to Generate
  clears the form/log/progress bar back to blank without relaunching the app.
  Built with CustomTkinter
```

Replace with:

```
  a required input — then click Generate. A "New Song" button next to Generate
  clears the form/log/progress bar back to blank without relaunching the app. A
  "Batch: Process a Folder" section (`lyricvideo/batch.py` finds/resolves the
  files) runs every audio file in a folder through the pipeline sequentially --
  one up-front confirmation decides whether already-done songs are skipped or
  regenerated (backing up each one first, like Redo) for the whole batch; a
  file that errors is logged and skipped, never aborting the rest. Built with
  CustomTkinter
```

- [ ] **Step 2: Run the full test suite one final time**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: every test passes.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the finished batch folder processing feature"
```

---
