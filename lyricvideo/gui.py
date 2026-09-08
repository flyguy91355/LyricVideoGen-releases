from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import httpx
from dotenv import load_dotenv

from .pipeline import run_pipeline
from .update.apply import copy_updatable_files, extract_release_archive, requirements_changed
from .update.release_client import RELEASES_REPO, check_for_update
from .update.version import read_local_version, write_local_version

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE_PATH = PROJECT_ROOT / "VERSION"


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled-song"


class _QueueWriter:
    """File-like object that pushes writes into a queue instead of a real
    stream -- lets stdout/stderr from the worker thread (Demucs/moviepy's own
    progress output) reach the GUI's log widget instead of the terminal."""

    def __init__(self, q: "queue.Queue"):
        self._queue = q

    def write(self, text: str) -> int:
        if text:
            self._queue.put(("log", text))
        return len(text)

    def flush(self) -> None:
        pass


class LyricVideoGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        current_version = read_local_version(str(_VERSION_FILE_PATH)) or "v0.0.0"
        root.title(f"LyricVideoGen {current_version}")
        root.geometry("760x600")

        self._queue: "queue.Queue" = queue.Queue()
        self._update_queue: "queue.Queue" = queue.Queue()
        self._current_version = current_version
        self._available_update: dict | None = None
        self._running = False

        self.title_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.lyrics_var = tk.StringVar()
        self.tab_pdf_var = tk.StringVar()
        self.chords_text_var = tk.StringVar()
        self.work_dir_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        self.title_var.trace_add("write", self._on_title_changed)

        self._build_widgets()
        self._check_api_keys()
        self._start_update_check()

    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        self.update_banner_var = tk.StringVar()
        self.update_banner = ttk.Label(
            self.root,
            textvariable=self.update_banner_var,
            foreground="#0060c0",
            cursor="hand2",
            anchor="w",
        )
        self.update_banner.bind("<Button-1>", self._on_update_banner_clicked)

        frame = ttk.Frame(self.root)
        frame.pack(fill="x", **pad)
        frame.columnconfigure(1, weight=1)
        self.top_frame = frame

        self._add_row(frame, 0, "Song title:", self.title_var)
        self._add_file_row(
            frame, 1, "Audio file (mp3/wav):", self.audio_var,
            [("Audio files", "*.mp3 *.wav *.m4a *.flac"), ("All files", "*.*")],
        )
        self._add_file_row(
            frame, 2, "Lyrics text file:", self.lyrics_var,
            [("Text files", "*.txt"), ("All files", "*.*")],
        )
        self._add_file_row(
            frame, 3, "Tab/chords PDF:", self.tab_pdf_var,
            [("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        self._add_file_row(
            frame, 4, "Or: chord-over-lyric text file:", self.chords_text_var,
            [("Text files", "*.txt"), ("All files", "*.*")],
        )
        self._add_row(frame, 5, "Work directory:", self.work_dir_var)

        note = ttk.Label(
            frame,
            text="Supply either the tab PDF, or the chord-over-lyric text file (skips\n"
            "Claude/vision entirely for chord placement -- use this if the PDF path fails\n"
            "with a content-filtering error).",
            foreground="#666",
            justify="left",
        )
        note.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 8))

        self.generate_button = ttk.Button(frame, text="Generate Video", command=self._on_generate)
        self.generate_button.grid(row=7, column=0, columnspan=3, pady=8)

        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", **pad)
        ttk.Label(status_frame, text="Status:").pack(side="left")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="#0a6").pack(side="left", padx=6)

        self.log_widget = scrolledtext.ScrolledText(self.root, height=20, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=True, **pad)

    def _add_row(self, frame: ttk.Frame, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=var, width=60).grid(row=row, column=1, sticky="ew", padx=4)

    def _add_file_row(
        self, frame: ttk.Frame, row: int, label: str, var: tk.StringVar, filetypes: list
    ) -> None:
        self._add_row(frame, row, label, var)

        def browse() -> None:
            path = filedialog.askopenfilename(filetypes=filetypes)
            if path:
                var.set(path)

        ttk.Button(frame, text="Browse...", command=browse).grid(row=row, column=2, padx=4)

    def _on_title_changed(self, *_args) -> None:
        if not self._running:
            slug = _slugify(self.title_var.get())
            self.work_dir_var.set(str(PROJECT_ROOT / "work" / slug))

    def _check_api_keys(self) -> None:
        load_dotenv(PROJECT_ROOT / ".env")
        missing = [k for k in ("ANTHROPIC_API_KEY", "REPLICATE_API_TOKEN") if not os.environ.get(k)]
        if missing:
            messagebox.showwarning(
                "Missing API keys",
                f"{', '.join(missing)} not found in {PROJECT_ROOT / '.env'}.\n"
                "Video generation will fail at the images stage without them.",
            )

    def _start_update_check(self) -> None:
        thread = threading.Thread(target=self._update_check_worker, daemon=True)
        thread.start()
        self.root.after(200, self._poll_update_queue)

    def _update_check_worker(self) -> None:
        try:
            release = check_for_update(self._current_version, RELEASES_REPO)
        except Exception:
            # Offline, GitHub hiccup, or no releases cut yet -- the launch-
            # time check must never surface an error or crash the GUI.
            return
        if release is not None:
            self._update_queue.put(("available", release))

    def _poll_update_queue(self) -> None:
        try:
            while True:
                kind, payload = self._update_queue.get_nowait()
                if kind == "available":
                    self._available_update = payload
                    self.update_banner_var.set(
                        f"Update available: {payload['tag_name']} — click for details"
                    )
                    self.update_banner.pack(
                        fill="x", padx=8, pady=(4, 0), before=self.top_frame
                    )
                elif kind == "apply_status":
                    if hasattr(self, "_update_status_var"):
                        self._update_status_var.set(payload)
                elif kind == "apply_done":
                    self._on_apply_update_done(payload)
                elif kind == "apply_error":
                    self._on_apply_update_error(payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_update_queue)

    def _on_update_banner_clicked(self, _event=None) -> None:
        if self._available_update is not None:
            self._open_update_dialog(self._available_update)

    def _open_update_dialog(self, release: dict) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Update available: {release['tag_name']}")
        dialog.geometry("480x360")
        self._update_dialog_window = dialog

        notes_widget = scrolledtext.ScrolledText(dialog, wrap="word", height=14)
        notes_widget.insert("1.0", release.get("notes", "") or "(no release notes)")
        notes_widget.configure(state="disabled")
        notes_widget.pack(fill="both", expand=True, padx=8, pady=8)

        self._update_status_var = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=self._update_status_var, foreground="#666").pack(
            anchor="w", padx=8
        )

        self._update_button_frame = ttk.Frame(dialog)
        self._update_button_frame.pack(fill="x", padx=8, pady=8)

        self._update_apply_button = ttk.Button(
            self._update_button_frame,
            text="Apply Update",
            command=lambda: self._on_apply_update_clicked(release),
        )
        self._update_apply_button.pack(side="left")
        ttk.Button(self._update_button_frame, text="Close", command=dialog.destroy).pack(
            side="right"
        )

        def _on_close() -> None:
            self._update_dialog_window = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", _on_close)

    def _on_apply_update_clicked(self, release: dict) -> None:
        if not messagebox.askyesno(
            "Apply update",
            f"Download and apply {release['tag_name']} now?\n\n"
            "This reinstalls dependencies if they changed and overwrites the "
            "program's own files. Your songs, work files, and .env are never "
            "touched.",
        ):
            return
        self._update_apply_button.state(["disabled"])
        self._update_status_var.set("Downloading...")
        thread = threading.Thread(
            target=self._apply_update_worker, args=(release,), daemon=True
        )
        thread.start()

    def _apply_update_worker(self, release: dict) -> None:
        try:
            self._update_queue.put(("apply_status", "Downloading..."))
            # follow_redirects=True is required: unlike requests, httpx does
            # NOT follow redirects by default, and this URL 302s to
            # codeload.github.com.
            response = httpx.get(release["download_url"], timeout=60, follow_redirects=True)
            response.raise_for_status()

            with tempfile.TemporaryDirectory() as tmp_dir:
                archive_path = Path(tmp_dir) / "release.tar.gz"
                archive_path.write_bytes(response.content)

                self._update_queue.put(("apply_status", "Extracting..."))
                extract_dir = Path(tmp_dir) / "extracted"
                extract_dir.mkdir()
                extracted_root = extract_release_archive(str(archive_path), str(extract_dir))

                old_requirements_path = PROJECT_ROOT / "requirements.txt"
                old_requirements = (
                    old_requirements_path.read_text(encoding="utf-8")
                    if old_requirements_path.exists()
                    else ""
                )
                new_requirements_path = Path(extracted_root) / "requirements.txt"
                new_requirements = (
                    new_requirements_path.read_text(encoding="utf-8")
                    if new_requirements_path.exists()
                    else old_requirements
                )

                if requirements_changed(old_requirements, new_requirements):
                    self._update_queue.put(("apply_status", "Installing dependencies..."))
                    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
                    pip_result = subprocess.run(
                        [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
                        cwd=extracted_root,
                        capture_output=True,
                        text=True,
                    )
                    if pip_result.returncode != 0:
                        self._update_queue.put((
                            "apply_error",
                            f"pip install failed — program left unchanged:\n{pip_result.stderr}",
                        ))
                        return

                self._update_queue.put(("apply_status", "Copying files..."))
                copy_updatable_files(extracted_root, str(PROJECT_ROOT))
                write_local_version(str(_VERSION_FILE_PATH), release["tag_name"])

            self._update_queue.put(("apply_done", release["tag_name"]))
        except Exception as e:
            self._update_queue.put(("apply_error", f"{type(e).__name__}: {e}"))

    def _on_apply_update_done(self, tag_name: str) -> None:
        self._update_status_var.set(f"Updated to {tag_name}. Relaunch to use it.")
        self._update_apply_button.pack_forget()
        ttk.Button(
            self._update_button_frame, text="Relaunch Now", command=self._on_relaunch_clicked
        ).pack(side="left")

    def _on_apply_update_error(self, message: str) -> None:
        self._update_status_var.set(f"Update failed: {message}")
        self._update_apply_button.state(["!disabled"])

    def _on_relaunch_clicked(self) -> None:
        venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
        subprocess.Popen([str(venv_python), "-m", "lyricvideo.gui"], cwd=str(PROJECT_ROOT))
        self.root.destroy()

    def _on_generate(self) -> None:
        if self._running:
            return

        title = self.title_var.get().strip()
        audio = self.audio_var.get().strip()
        lyrics = self.lyrics_var.get().strip()
        tab_pdf = self.tab_pdf_var.get().strip()
        chords_text = self.chords_text_var.get().strip()
        work_dir = self.work_dir_var.get().strip()

        if not title:
            messagebox.showerror("Missing input", "Song title is required.")
            return
        if not audio:
            messagebox.showerror("Missing input", "Audio file is required.")
            return
        if not tab_pdf and not chords_text:
            messagebox.showerror(
                "Missing input", "Supply either a tab PDF or a chord-over-lyric text file."
            )
            return
        if not work_dir:
            messagebox.showerror("Missing input", "Work directory is required.")
            return

        self._running = True
        self.generate_button.state(["disabled"])
        self.status_var.set("Starting...")
        self._clear_log()

        thread = threading.Thread(
            target=self._run_worker,
            args=(
                Path(audio),
                Path(tab_pdf) if tab_pdf else None,
                Path(work_dir),
                title,
                Path(lyrics) if lyrics else None,
                Path(chords_text) if chords_text else None,
            ),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _run_worker(
        self,
        audio_path: Path,
        tab_pdf_path: Path | None,
        work_dir: Path,
        title: str,
        lyrics_file: Path | None,
        chords_text_file: Path | None,
    ) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        try:
            out_path = run_pipeline(
                audio_path,
                tab_pdf_path,
                work_dir,
                title,
                lyrics_file=lyrics_file,
                chords_text_file=chords_text_file,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
            )
            self._queue.put(("done", str(out_path)))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "stage":
                    self.status_var.set(f"Stage: {payload}")
                elif kind == "done":
                    self.status_var.set("Done")
                    self._running = False
                    self.generate_button.state(["!disabled"])
                    messagebox.showinfo("Video ready", f"Wrote {payload}")
                    return
                elif kind == "error":
                    self.status_var.set("Failed")
                    self._running = False
                    self.generate_button.state(["!disabled"])
                    self._append_log(f"\nERROR:\n{payload}\n")
                    messagebox.showerror("Generation failed", payload.splitlines()[0])
                    return
        except queue.Empty:
            pass
        if self._running:
            self.root.after(100, self._poll_queue)

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", text)
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")


def main() -> None:
    root = tk.Tk()
    LyricVideoGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
