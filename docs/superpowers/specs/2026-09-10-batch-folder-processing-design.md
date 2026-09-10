# Batch Folder Processing — Design

**Date:** 2026-09-10
**Status:** Approved by owner, ready for implementation planning

## Purpose

Owner has to run Generate one file at a time today, and pointed at LyricChord
(the sibling project) having a batch mode. They want to point the GUI at a
folder of audio files and have every song in it run through the pipeline
without babysitting each one individually.

## Scope

**In scope:**
1. `lyricvideo/batch.py` (new): pure, testable logic for finding audio files in
   a folder and resolving each one's identity/work directory/already-done
   status, ahead of actually running the pipeline on any of them.
2. `gui.py`: a new "Batch: Process a Folder" section (folder picker + Start
   button), a batch worker that runs `run_pipeline`/the existing Redo path
   sequentially per file, and wiring that reuses the existing status label,
   progress bar, and log console rather than adding parallel copies of them.
3. A single up-front confirmation when some songs in the folder already have a
   finished video: skip those, or regenerate all of them (batch-wide choice,
   not per file).
4. Per-file failure isolation: one file erroring is logged and skipped, never
   aborts the rest of the batch. A completion summary reports
   succeeded/skipped/failed counts and names any failures.
5. Tests for `batch.py`'s pure functions. GUI wiring (the new section, the
   confirmation/summary dialogs) stays manually/visually verified, matching
   this project's existing testing-constraint precedent for GUI code.

**Out of scope:**
- Per-file review/editing before a batch runs (owner confirmed: fully
  automatic, matching how a single Generate already behaves when the title is
  left blank — auto-identified, no manual per-song intervention).
- A drag-and-drop file queue or a persistent multi-run job history — this is a
  "point at a folder, click Start" tool, not a job manager.
- Recursive subfolder scanning — one folder's immediate files only, matching
  the simplicity of the rest of this feature; can be revisited later if asked.
- Concurrent/parallel file processing — sequential only, so Anthropic/Replicate
  spend and Demucs's CPU load stay bounded exactly like today's one-at-a-time
  flow; nothing in this design changes that.

## Architecture

```
lyricvideo/batch.py
  ├─ find_audio_files(folder: Path) -> list[Path]
  │     Same extension filter as the single-file Browse dialog
  │     (*.mp3 *.wav *.m4a *.flac), sorted, folder's immediate contents only.
  │
  ├─ @dataclass BatchItem:
  │     audio_path: Path
  │     title: str
  │     work_dir: Path
  │     already_done: bool     # work_dir/<slugify(title)>.mp4 already exists
  │
  └─ resolve_batch_items(files: list[Path], work_root: Path) -> list[BatchItem]
        For each file: identify.extract_metadata(file) -> title (free: tags/
        filename/lrclib/MusicBrainz, no Claude/Replicate spend, matching the
        existing single-song title auto-fill's own cost profile) -> work_dir =
        work_root / slugify(title) -> already_done = (work_dir /
        f"{slugify(title)}.mp4").exists(). A file whose metadata extraction
        itself raises is still included -- title falls back to the audio
        file's own stem (`audio_path.stem`, e.g. "some_song" from
        "some_song.mp3"), NOT a constant like "" or "untitled", so two
        different failing files never collide on the same work_dir; the "" or
        similar failure mode was caught during spec self-review. already_done
        is forced False for these (there's nothing real to check yet).
        run_pipeline's own identify stage will raise the same real error again
        when the batch actually reaches this item, where it's caught by the
        existing per-file try/except and reported as a normal batch failure
        (see below) instead of silently vanishing from the list here.

gui.py
  ├─ "Batch: Process a Folder" section (below "Redo an Existing Song"):
  │     folder path field (read-only display) + "Browse Folder..." + "Start Batch"
  │
  ├─ _on_start_batch():
  │     guarded by the existing self._running flag, same as Generate/Redo.
  │     Runs _resolve_batch_worker(folder) on a background thread (network
  │     calls inside extract_metadata must never block the UI thread).
  │
  ├─ _resolve_batch_worker(folder):
  │     items = resolve_batch_items(find_audio_files(folder), PROJECT_ROOT / "work")
  │     posted back to the main thread via the existing queue mechanism as a
  │     new ("batch_resolved", items) message.
  │
  ├─ on ("batch_resolved", items) in _poll_queue:
  │     if not items: messagebox.showinfo("No audio files found", ...); return
  │     done_count = sum(1 for i in items if i.already_done)
  │     if done_count: ask via messagebox.askyesno("N of M songs already have a
  │       finished video. Skip those, or regenerate everyone?") -> skip_done: bool
  │     else: skip_done = True  # nothing to decide
  │     to_process = [i for i in items if not (skip_done and i.already_done)]
  │     starts _run_batch_worker(to_process) the same way Generate/Redo start
  │     their own worker thread.
  │
  └─ _run_batch_worker(items):
        results = {"succeeded": [], "skipped_already_done": [], "failed": []}
        (skipped-already-done items are recorded here too, purely for the
        final summary's counts -- they never reach this function's loop body)
        for index, item in enumerate(items, start=1):
            report(("batch_file_start", (index, len(items), item.title)))
            try:
                if item.already_done:
                    backup_song_outputs(item.work_dir, slugify(item.title))
                    run_pipeline(item.audio_path, item.work_dir, item.title,
                                 start_stage="fetch_lyrics", settings=self.settings)
                else:
                    run_pipeline(item.audio_path, item.work_dir, item.title,
                                 settings=self.settings)
                results["succeeded"].append(item.title)
            except Exception as e:
                results["failed"].append((item.title, str(e)))
                # logged via the same stdout/stderr redirect _run_worker already
                # uses -- continues to the next item, never re-raises.
        report(("batch_done", results))
```

`("batch_file_start", (index, total, title))` and stage-progress messages
(the existing `("stage", ...)` messages `run_pipeline`'s `progress_callback`
already emits, unchanged) combine in `_poll_queue` into one combined status
line ("File 3 of 12: <title> — Stage: align") and one combined progress bar
fraction: `(index - 1 + stage_fraction) / total`, where `stage_fraction` is the
same `STAGES.index(stage) / len(STAGES)` calculation the single-song path
already uses. No second progress bar or status label — the batch reuses the
exact widgets a single Generate/Redo already has.

`("batch_done", results)` shows one summary dialog (`messagebox.showinfo` for
an all-success run, `messagebox.showwarning` if anything failed, listing failed
titles) and re-enables the Generate/Redo/Start Batch buttons, mirroring how
`("done", ...)`/`("error", ...)` already finish a single-song run.

## Testing

- `tests/test_batch.py`: `find_audio_files` (filters extensions correctly,
  ignores subfolders, sorts, empty-folder case), `resolve_batch_items`
  (`already_done` true/false based on a real file on disk, extraction failure
  doesn't crash the resolve step itself).
- No new tests for `run_pipeline`/`backup_song_outputs` — the batch worker
  calls them exactly as the existing single-song Generate/Redo paths already
  do, and those call sites are already covered by `test_pipeline.py`.
- GUI wiring (the new section, confirmation dialog, summary dialog) stays
  manually/visually verified per this project's established precedent — a
  real folder with a mix of new and already-done songs, run through the actual
  GUI, is the acceptance test.
