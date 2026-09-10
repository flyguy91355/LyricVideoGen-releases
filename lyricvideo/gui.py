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
from tkinter import filedialog, messagebox

import anthropic
import customtkinter as ctk
import httpx
from dotenv import load_dotenv
from googleapiclient.discovery import build

from .batch import (
    find_audio_files,
    load_last_batch_folder,
    resolve_batch_items,
    resolve_existing_folder,
    save_last_batch_folder,
)
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
from .settings import Settings
from .settings_panel import SettingsPanel
from .settings_preview import SettingsPreviewFrame
from .update.apply import copy_updatable_files, extract_release_archive, requirements_changed
from .update.release_client import RELEASES_REPO, check_for_update
from .update.version import read_local_version, write_local_version
from . import youtube_auth
from .youtube import list_new_comments, post_reply
from .youtube_comment_state import (
    PendingReply,
    add_pending_reply,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_reply,
)
from .youtube_metadata import draft_comment_reply
from .youtube_schedule import schedule_upload
from .youtube_state import load_youtube_state

_CR_LF_RE = re.compile(r"[\r\n]")


def _split_log_text(pending: str, text: str) -> tuple[str, str]:
    """Terminal-style \\r/\\n handling for the log widget: \\n commits the
    current line permanently, \\r discards it and starts the line over (this
    is what tqdm-style progress bars send on every update). Returns
    (new_pending_line, text_to_commit) -- text_to_commit is zero or more
    complete newline-terminated lines safe to insert verbatim; new_pending
    is the trailing not-yet-terminated content that should currently be
    showing as the widget's last, still-changeable line.
    """
    if not text:
        return pending, ""
    committed: list[str] = []
    current = pending
    start = 0
    for m in _CR_LF_RE.finditer(text):
        idx = m.start()
        current += text[start:idx]
        if m.group() == "\n":
            committed.append(current + "\n")
        current = ""
        start = idx + 1
    current += text[start:]
    return current, "".join(committed)


def _maybe_upload_to_youtube(work_dir: Path, settings: Settings) -> None:
    """Uploads work_dir's finished video to YouTube if auto-upload is on,
    YouTube is connected, and this song has never been uploaded before --
    Redo of an already-uploaded song is deliberately skipped here to avoid
    duplicate videos piling up (owner's explicit choice). Any failure is
    caught and logged -- an upload problem must never make an
    otherwise-successful video generation look like it failed."""
    if not settings.youtube_auto_upload:
        return
    if load_youtube_state(work_dir) is not None:
        return
    credentials = youtube_auth.load_credentials()
    if credentials is None:
        return

    try:
        youtube_client = build("youtube", "v3", credentials=credentials)
        anthropic_client = anthropic.Anthropic()
        schedule_upload(youtube_client, anthropic_client, work_dir, settings)
        print(f"Uploaded to YouTube: {work_dir.name}")
    except Exception as e:
        print(f"WARNING: YouTube upload failed for {work_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE_PATH = PROJECT_ROOT / "VERSION"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


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
    def __init__(self, root: ctk.CTk):
        self.root = root
        current_version = read_local_version(str(_VERSION_FILE_PATH)) or "v0.0.0"
        root.title(f"PlayAlongVideoProduction {current_version}")
        root.geometry("1400x820")

        self._queue: "queue.Queue" = queue.Queue()
        self._update_queue: "queue.Queue" = queue.Queue()
        self._current_version = current_version
        self._available_update: dict | None = None
        self._running = False
        self._log_pending = ""
        self._log_has_uncommitted_line = False
        self._suppress_settings_save = True  # True while load_from() is populating widgets on launch

        self.settings = Settings.load()

        self.title_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.work_dir_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.redo_song_var = tk.StringVar()
        self.redo_new_images_var = tk.BooleanVar(value=False)
        self.batch_folder_var = tk.StringVar(value=load_last_batch_folder())
        self._batch_items: list = []  # list[BatchItem] once resolved
        self._batch_index = 0
        self._batch_results = {"succeeded": [], "skipped_already_done": [], "failed": []}
        self._last_work_dir: Path | None = None

        self.title_var.trace_add("write", self._on_title_changed)

        self._build_widgets()
        self._suppress_settings_save = False
        self._check_api_keys()
        self._start_update_check()
        self._refresh_youtube_status()

    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        self.update_banner_var = tk.StringVar()
        self.update_banner = ctk.CTkLabel(
            self.root,
            textvariable=self.update_banner_var,
            text_color="#4da3ff",
            cursor="hand2",
            anchor="w",
        )
        self.update_banner.bind("<Button-1>", self._on_update_banner_clicked)

        # Two-column body: left = single-song form + Redo, right = SettingsPanel.
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, **pad)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)
        # `body` (not `left`) is the update banner's `before=` anchor: pack()'s
        # `before=` requires a widget managed by pack() in the SAME master as
        # the widget being packed. `left` is grid()-managed inside `body`, not
        # pack()-managed inside `self.root` like the banner is -- using it
        # here always raised TclError: window isn't packed, silently (a real
        # bug from the CustomTkinter rebuild, found live 2026-09-09: the
        # banner never once appeared across multiple relaunches with a real
        # update available, because Tkinter callback exceptions print to a
        # log the desktop-launched app's owner never sees, not to any visible
        # UI). `body` itself IS pack()-managed directly under `self.root`,
        # exactly like the banner, so `before=body` inserts the banner right
        # above it as intended.
        self.top_frame = body

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        form = ctk.CTkFrame(left, fg_color="transparent")
        form.pack(fill="x", padx=10, pady=10)
        form.grid_columnconfigure(1, weight=1)

        self._add_row(form, 0, "Song title (auto-filled, editable):", self.title_var)
        self._add_file_row(
            form, 1, "Audio file (mp3/wav):", self.audio_var,
            [("Audio files", "*.mp3 *.wav *.m4a *.flac"), ("All files", "*.*")],
            on_selected=self._on_audio_selected,
        )
        self._add_row(form, 2, "Work directory:", self.work_dir_var)

        note = ctk.CTkLabel(
            form,
            text="Title, artist, and lyrics are identified automatically from the audio\n"
            "file's tags and online lookup -- edit the title above if it's wrong. Chords\n"
            "are detected directly from the audio; no tab or chord sheet is needed.",
            text_color="gray60",
            justify="left",
            anchor="w",
        )
        note.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))

        button_row = ctk.CTkFrame(form, fg_color="transparent")
        button_row.grid(row=4, column=0, columnspan=3, pady=8)
        self.generate_button = ctk.CTkButton(button_row, text="Generate Video", command=self._on_generate)
        self.generate_button.pack(side="left", padx=4)
        self.new_song_button = ctk.CTkButton(
            button_row, text="New Song", command=self._on_new_song, fg_color="gray30", hover_color="gray20",
        )
        self.new_song_button.pack(side="left", padx=4)

        status_frame = ctk.CTkFrame(left, fg_color="transparent")
        status_frame.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(status_frame, text="Status:").pack(side="left")
        ctk.CTkLabel(status_frame, textvariable=self.status_var, text_color="#3ecf8e").pack(
            side="left", padx=6
        )
        self.upload_button = ctk.CTkButton(
            status_frame, text="Upload to YouTube", command=self._on_manual_upload, state="disabled", width=140,
        )
        self.upload_button.pack(side="left", padx=(12, 0))

        self.progress_bar = ctk.CTkProgressBar(left)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 10))

        redo_frame = ctk.CTkFrame(left)
        redo_frame.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(redo_frame, text="Redo an Existing Song", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        redo_controls = ctk.CTkFrame(redo_frame, fg_color="transparent")
        redo_controls.pack(fill="x", padx=8, pady=(0, 8))
        self.redo_combo = ctk.CTkComboBox(
            redo_controls, variable=self.redo_song_var,
            values=list_redoable_songs(PROJECT_ROOT / "work"), width=260, state="readonly",
        )
        self.redo_combo.pack(side="left", padx=(0, 8))
        ctk.CTkCheckBox(
            redo_controls, text="Generate new images", variable=self.redo_new_images_var,
        ).pack(side="left", padx=8)
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
        self.log_widget.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew")
        self.settings_preview = SettingsPreviewFrame(right, self.settings)
        self.settings_preview.pack(fill="x", padx=6, pady=(6, 0))

        youtube_connect_frame = ctk.CTkFrame(right, fg_color="transparent")
        youtube_connect_frame.pack(fill="x", padx=4, pady=(6, 0))
        self.youtube_status_var = tk.StringVar(value="YouTube: not connected")
        ctk.CTkLabel(youtube_connect_frame, textvariable=self.youtube_status_var, anchor="w").pack(side="left")
        ctk.CTkButton(
            youtube_connect_frame, text="Connect to YouTube", command=self._on_connect_youtube, width=160,
        ).pack(side="right")

        self._build_youtube_panel(right)

        self.settings_panel = SettingsPanel(right, self.settings, on_change=self._on_settings_changed)
        self.settings_panel.pack(fill="both", expand=True, padx=6, pady=6)

    def _add_row(self, frame: ctk.CTkFrame, row: int, label: str, var: tk.StringVar) -> None:
        ctk.CTkLabel(frame, text=label).grid(row=row, column=0, sticky="w")
        ctk.CTkEntry(frame, textvariable=var, width=360).grid(row=row, column=1, sticky="ew", padx=4)

    def _add_file_row(
        self, frame: ctk.CTkFrame, row: int, label: str, var: tk.StringVar, filetypes: list,
        on_selected=None,
    ) -> None:
        self._add_row(frame, row, label, var)

        def browse() -> None:
            path = filedialog.askopenfilename(filetypes=filetypes)
            if path:
                var.set(path)
                if on_selected is not None:
                    on_selected(path)

        ctk.CTkButton(frame, text="Browse...", command=browse, width=90).grid(row=row, column=2, padx=4)

    def _on_settings_changed(self) -> None:
        """SettingsPanel's on_change fires on every keystroke/slider-move/color-pick.
        Suppressed while the panel is still being populated on launch (Settings.load()
        itself is already the source of truth then -- saving mid-load would just
        write back the same file it was read from, harmlessly but pointlessly)."""
        if self._suppress_settings_save:
            return
        self.settings = self.settings_panel.collect()
        self.settings.save()
        self.settings_preview.update_preview(self.settings)

    def _on_title_changed(self, *_args) -> None:
        if not self._running:
            slug = _slugify(self.title_var.get())
            self.work_dir_var.set(str(PROJECT_ROOT / "work" / slug))

    def _on_audio_selected(self, path: str) -> None:
        """Best-effort auto-fill of the title once an audio file is picked --
        never overwrites a title the owner already typed, and any failure
        (offline, unreadable file) is silently ignored: Generate still works
        with an auto-identified title computed fresh inside run_pipeline's own
        identify stage regardless of whether this GUI-side preview succeeds."""
        if self.title_var.get().strip():
            return
        thread = threading.Thread(target=self._identify_worker, args=(path,), daemon=True)
        thread.start()

    def _identify_worker(self, path: str) -> None:
        try:
            info = extract_metadata(Path(path))
        except Exception:
            return
        self.root.after(0, lambda: self._apply_identified_title(info.title))

    def _apply_identified_title(self, title: str) -> None:
        if not self.title_var.get().strip():  # still empty -- no manual edit arrived meanwhile
            self.title_var.set(title)

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
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Update available: {release['tag_name']}")
        dialog_w, dialog_h = 480, 360
        self.root.update_idletasks()
        # Centered over the main window and kept above it (transient +
        # grab_set + lift/focus_force) -- a plain Toplevel can otherwise open
        # behind the main window with no visible indication, which is
        # exactly how the owner missed the Relaunch Now button appearing.
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(x, 0)}+{max(y, 0)}")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        self._update_dialog_window = dialog

        notes_widget = ctk.CTkTextbox(dialog, wrap="word", height=220)
        notes_widget.insert("1.0", release.get("notes", "") or "(no release notes)")
        notes_widget.configure(state="disabled")
        notes_widget.pack(fill="both", expand=True, padx=8, pady=8)

        self._update_status_var = tk.StringVar(value="")
        ctk.CTkLabel(dialog, textvariable=self._update_status_var, text_color="gray60").pack(
            anchor="w", padx=8
        )

        self._update_button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        self._update_button_frame.pack(fill="x", padx=8, pady=8)

        self._update_apply_button = ctk.CTkButton(
            self._update_button_frame,
            text="Apply Update",
            command=lambda: self._on_apply_update_clicked(release),
        )
        self._update_apply_button.pack(side="left")
        ctk.CTkButton(self._update_button_frame, text="Close", command=dialog.destroy).pack(
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
        self._update_apply_button.configure(state="disabled")
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
        ctk.CTkButton(
            self._update_button_frame, text="Relaunch Now", command=self._on_relaunch_clicked
        ).pack(side="left")

    def _on_apply_update_error(self, message: str) -> None:
        self._update_status_var.set(f"Update failed: {message}")
        self._update_apply_button.configure(state="normal")

    def _on_relaunch_clicked(self) -> None:
        venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
        subprocess.Popen([str(venv_python), "-m", "lyricvideo.gui"], cwd=str(PROJECT_ROOT))
        self.root.destroy()

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
        current = self.batch_folder_var.get()
        initialdir = current if current and Path(current).is_dir() else None
        folder = filedialog.askdirectory(initialdir=initialdir) if initialdir else filedialog.askdirectory()
        if folder:
            # resolve_existing_folder recovers a trailing/leading space the dialog
            # itself silently drops (see resolve_existing_folder's docstring) --
            # saving the corrected path keeps next time's initialdir usable too.
            folder = str(resolve_existing_folder(Path(folder)))
            self.batch_folder_var.set(folder)
            save_last_batch_folder(folder)

    def _on_start_batch(self) -> None:
        if self._running:
            return
        # NOT .strip()'d -- unlike the typed title/audio/work-dir fields, this
        # value comes verbatim from a real folder the OS file dialog resolved,
        # and a real folder name can legitimately have leading/trailing
        # whitespace (confirmed live, 2026-09-10: a folder literally named
        # "batch music " with a trailing space -- stripping it here made the
        # app look for a folder that doesn't exist).
        folder = self.batch_folder_var.get()
        if not folder.strip():
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
            folder = resolve_existing_folder(folder)
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
                    _maybe_upload_to_youtube(item.work_dir, self.settings)
                    results["succeeded"].append(item.title)
                except Exception as e:
                    results["failed"].append((item.title, f"{type(e).__name__}: {e}"))
                    print(f"Batch item {item.title!r} failed: {type(e).__name__}: {e}")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        self._queue.put(("batch_done", results))

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

    def _on_generate(self) -> None:
        if self._running:
            return

        title = self.title_var.get().strip()  # optional -- run_pipeline's own
        audio = self.audio_var.get().strip()   # identify stage falls back if blank
        work_dir = self.work_dir_var.get().strip()

        if not audio:
            messagebox.showerror("Missing input", "Audio file is required.")
            return
        if not work_dir:
            messagebox.showerror("Missing input", "Work directory is required.")
            return

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.status_var.set("Starting...")
        self.progress_bar.set(0.0)
        self._clear_log()

        self._last_work_dir = Path(work_dir)
        thread = threading.Thread(
            target=self._run_worker,
            args=(Path(audio), Path(work_dir), title or None),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _on_redo(self) -> None:
        if self._running:
            return

        slug = self.redo_song_var.get().strip()
        if not slug:
            messagebox.showerror("No song selected", "Pick a song from the dropdown to redo.")
            return

        song_dir = PROJECT_ROOT / "work" / slug
        try:
            audio_path, title = load_redo_inputs(song_dir)
        except Exception as e:
            messagebox.showerror("Could not load song", f"{type(e).__name__}: {e}")
            return

        generate_new_images = self.redo_new_images_var.get()
        if not messagebox.askyesno(
            "Redo song",
            f'Redo "{title}" using the current program?\n\n'
            "This re-syncs chords/lyrics with today's code and re-renders the "
            "video, overwriting it in place -- the current video and timing "
            "data are backed up first. "
            + ("New AI images will be generated." if generate_new_images
               else "Existing images will be reused (no AI cost)."),
        ):
            return

        backup_song_outputs(song_dir, _slugify(title))
        if generate_new_images:
            prepare_images_for_fresh_regeneration(song_dir / "images")

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.status_var.set("Starting...")
        self.progress_bar.set(0.0)
        self._clear_log()

        self._last_work_dir = song_dir
        thread = threading.Thread(
            target=self._run_worker,
            args=(audio_path, song_dir, title, "fetch_lyrics"),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _refresh_youtube_status(self) -> None:
        credentials = youtube_auth.load_credentials()
        if credentials is None:
            self.youtube_status_var.set("YouTube: not connected")
            return
        try:
            channel = youtube_auth.get_channel_title(credentials)
            self.youtube_status_var.set(f"YouTube: connected as {channel}")
        except Exception:
            self.youtube_status_var.set("YouTube: connected (channel name unavailable)")

    def _on_connect_youtube(self) -> None:
        secrets_path = self.settings.youtube_client_secrets_path
        if not secrets_path:
            messagebox.showerror(
                "No client secrets file", "Choose your client_secret_*.json file in Settings first.",
            )
            return

        def worker():
            try:
                youtube_auth.connect(Path(secrets_path))
                self.root.after(0, self._refresh_youtube_status)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Could not connect to YouTube", f"{type(e).__name__}: {e}",
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_manual_upload(self) -> None:
        if self._last_work_dir is None:
            return
        work_dir = self._last_work_dir
        self.upload_button.configure(state="disabled")

        def worker():
            credentials = youtube_auth.load_credentials()
            if credentials is None:
                self.root.after(0, lambda: messagebox.showerror(
                    "Not connected", "Connect to YouTube in Settings first.",
                ))
                self.root.after(0, lambda: self.upload_button.configure(state="normal"))
                return
            try:
                youtube_client = build("youtube", "v3", credentials=credentials)
                anthropic_client = anthropic.Anthropic()
                schedule_upload(youtube_client, anthropic_client, work_dir, self.settings)
                self.root.after(0, lambda: messagebox.showinfo("Uploaded", "Video uploaded to YouTube."))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Upload failed", f"{type(e).__name__}: {e}"))
            finally:
                self.root.after(0, lambda: self.upload_button.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _build_youtube_panel(self, parent) -> None:
        frame = ctk.CTkFrame(parent)
        frame.pack(side="bottom", fill="x", padx=4, pady=(0, 6))
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(header, text="YouTube Comments", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="Check Now", command=self._on_check_youtube_comments, width=100).pack(side="right")
        self.youtube_replies_frame = ctk.CTkScrollableFrame(frame, height=200)
        self.youtube_replies_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._render_pending_replies()
        self.root.after(20 * 60 * 1000, self._schedule_youtube_comment_check)

    def _render_pending_replies(self) -> None:
        for child in self.youtube_replies_frame.winfo_children():
            child.destroy()
        for reply in load_pending_replies():
            self._render_one_pending_reply(reply)

    def _render_one_pending_reply(self, reply: PendingReply) -> None:
        row = ctk.CTkFrame(self.youtube_replies_frame)
        row.pack(fill="x", pady=4)
        badge = " ⚠ possible error report" if reply.is_error_report else ""
        ctk.CTkLabel(
            row, text=f"{reply.author}: {reply.comment_text}{badge}", anchor="w", wraplength=400, justify="left",
        ).pack(fill="x", padx=6, pady=(6, 2))
        text_box = ctk.CTkTextbox(row, height=60)
        text_box.insert("1.0", reply.draft_reply)
        text_box.pack(fill="x", padx=6, pady=(0, 4))
        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkButton(
            buttons, text="Approve", width=80, command=lambda: self._on_approve_reply(reply, text_box),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            buttons, text="Dismiss", width=80, fg_color="gray30", hover_color="gray20",
            command=lambda: self._on_dismiss_reply(reply),
        ).pack(side="left")

    def _on_approve_reply(self, reply: PendingReply, text_box) -> None:
        text = text_box.get("1.0", "end").strip()

        def worker():
            credentials = youtube_auth.load_credentials()
            if credentials is None:
                return
            try:
                youtube_client = build("youtube", "v3", credentials=credentials)
                post_reply(youtube_client, reply.comment_id, text)
                remove_pending_reply(reply.comment_id)
                self.root.after(0, self._render_pending_replies)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Could not post reply", f"{type(e).__name__}: {e}",
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_dismiss_reply(self, reply: PendingReply) -> None:
        remove_pending_reply(reply.comment_id)
        self._render_pending_replies()

    def _on_check_youtube_comments(self) -> None:
        threading.Thread(target=self._check_youtube_comments_worker, daemon=True).start()

    def _check_youtube_comments_worker(self) -> None:
        credentials = youtube_auth.load_credentials()
        if credentials is None:
            return
        youtube_client = build("youtube", "v3", credentials=credentials)
        anthropic_client = anthropic.Anthropic()
        seen_ids = load_seen_comment_ids()
        new_ids = []
        for work_dir in sorted((PROJECT_ROOT / "work").glob("*")):
            state = load_youtube_state(work_dir)
            if state is None:
                continue
            for comment in list_new_comments(youtube_client, state.video_id, seen_ids):
                draft, is_error_report = draft_comment_reply(anthropic_client, comment.text, state.title)
                add_pending_reply(PendingReply(
                    comment_id=comment.comment_id, video_id=comment.video_id, author=comment.author,
                    comment_text=comment.text, draft_reply=draft, is_error_report=is_error_report,
                ))
                new_ids.append(comment.comment_id)
        if new_ids:
            mark_comments_seen(new_ids)
        self.root.after(0, self._render_pending_replies)

    def _schedule_youtube_comment_check(self) -> None:
        threading.Thread(target=self._youtube_periodic_tick, daemon=True).start()
        self.root.after(20 * 60 * 1000, self._schedule_youtube_comment_check)

    def _youtube_periodic_tick(self) -> None:
        """Runs every 20 minutes on a background thread (never the GUI thread,
        since both load_credentials()'s token refresh and get_channel_title()
        can make a real network call). Keeps the connection status label
        current even across a long-running session -- otherwise a token that
        expires mid-session (Google's own 7-day limit on an unverified/
        Testing-mode app, which this one always is for personal use) would
        only be noticed at next app launch."""
        credentials = youtube_auth.load_credentials()
        if credentials is None:
            self.root.after(0, lambda: self.youtube_status_var.set("YouTube: not connected"))
            return
        try:
            channel = youtube_auth.get_channel_title(credentials)
            status_text = f"YouTube: connected as {channel}"
        except Exception:
            status_text = "YouTube: connected (channel name unavailable)"
        self.root.after(0, lambda: self.youtube_status_var.set(status_text))
        self._check_youtube_comments_worker()

    def _run_worker(
        self,
        audio_path: Path,
        work_dir: Path,
        title: str | None = None,
        start_stage: str = "identify",
    ) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        try:
            out_path = run_pipeline(
                audio_path,
                work_dir,
                title,
                start_stage=start_stage,
                settings=self.settings,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
            )
            _maybe_upload_to_youtube(work_dir, self.settings)
            self._queue.put(("done", str(out_path)))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    def _poll_queue(self) -> None:
        # Drain everything queued since the last tick up front, rather than
        # handling each item with its own widget update -- moviepy/tqdm can
        # write dozens of progress-bar chunks within a single 100ms tick, and
        # one insert+see() per chunk against a growing Text widget is what
        # made the log pane (and the whole GUI) grind to a crawl.
        items: list[tuple[str, str]] = []
        try:
            while True:
                items.append(self._queue.get_nowait())
        except queue.Empty:
            pass

        log_chunks: list[str] = []

        def flush_log() -> None:
            if log_chunks:
                self._append_log("".join(log_chunks))
                log_chunks.clear()

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
                self.upload_button.configure(
                    state="normal" if youtube_auth.load_credentials() is not None else "disabled"
                )
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

        if self._running:
            self.root.after(100, self._poll_queue)

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")
        self._log_pending = ""
        self._log_has_uncommitted_line = False

    def _append_log(self, text: str) -> None:
        self._log_pending, to_commit = _split_log_text(self._log_pending, text)

        self.log_widget.configure(state="normal")
        if self._log_has_uncommitted_line:
            # The widget's current last line was left showing a still-in-
            # progress update (e.g. a tqdm percentage) -- replace it rather
            # than appending, so a burst of \r updates collapses into one
            # line instead of piling up a new permanent line per update.
            self.log_widget.delete("end-1c linestart", "end-1c")
        if to_commit:
            self.log_widget.insert("end", to_commit)
        if self._log_pending:
            self.log_widget.insert("end", self._log_pending)
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")
        self._log_has_uncommitted_line = bool(self._log_pending)


def main() -> None:
    root = ctk.CTk()
    LyricVideoGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
