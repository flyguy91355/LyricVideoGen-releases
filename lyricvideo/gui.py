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
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox

import anthropic
import customtkinter as ctk
import httpx
from dotenv import load_dotenv
from googleapiclient.discovery import build

from .venv import venv_python
from .batch import (
    find_audio_files,
    load_last_batch_folder,
    resolve_batch_items,
    resolve_existing_folder,
    save_last_batch_folder,
)
from .identify import extract_metadata
from .dismissed_songs import dismiss_song, load_dismissed
from .pipeline import (
    STAGES,
    run_pipeline,
    slugify as _slugify,
    list_redoable_songs,
    list_pending_uploads,
    list_rendered_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
    song_video_path,
)
from .settings import Settings
from .settings_panel import SettingsPanel
from .settings_preview import SettingsPreviewFrame
from .update.apply import (
    copy_updatable_files,
    extract_release_archive,
    is_source_commit_already_applied,
    read_release_source_commit,
    requirements_changed,
)
from .update.release_client import RELEASES_REPO, check_for_update
from .update.version import read_local_version, write_local_version
from . import youtube_auth
from .youtube import is_video_public, list_new_comments, post_reply, post_top_level_comment, video_exists
from .youtube_comment_state import (
    PendingComment,
    PendingReply,
    add_pending_reply,
    load_pending_comments,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_comment,
    remove_pending_reply,
)
from .youtube_metadata import build_play_along_title, draft_comment_reply
from .youtube_playlists import organize_video
from .youtube_schedule import schedule_upload
from .youtube_state import load_youtube_state, save_youtube_state

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


def _mark_engagement_comment_posted(video_id: str) -> None:
    for work_dir in (PROJECT_ROOT / "work").glob("*"):
        state = load_youtube_state(work_dir)
        if state is not None and state.video_id == video_id:
            save_youtube_state(work_dir, replace(state, engagement_comment_posted=True))
            return


def _maybe_upload_to_youtube(work_dir: Path, settings: Settings) -> None:
    """Uploads work_dir's finished video to YouTube if auto-upload is on,
    YouTube is connected, and this song has never been (verifiably) uploaded
    before -- Redo of an already-uploaded song is deliberately skipped to
    avoid duplicate videos piling up (owner's explicit choice), UNLESS the
    saved video_id no longer exists on YouTube (the owner deleted it there
    directly -- real incident 2026-09-10, "Come As You Are" stayed stuck
    showing "already uploaded" forever after its manual deletion, since
    nothing ever re-checked the saved state against YouTube's real state).
    Any failure is caught and logged -- an upload problem must never make an
    otherwise-successful video generation look like it failed."""
    if not settings.youtube_auto_upload:
        return
    credentials = youtube_auth.load_credentials()
    if credentials is None:
        return

    try:
        youtube_client = build("youtube", "v3", credentials=credentials)
        state = load_youtube_state(work_dir)
        if state is not None:
            try:
                if video_exists(youtube_client, state.video_id):
                    return
            except Exception:
                return  # can't verify right now -- fail closed, don't risk a duplicate
        anthropic_client = anthropic.Anthropic()
        schedule_upload(youtube_client, anthropic_client, work_dir, settings)
        print(f"Uploaded to YouTube: {work_dir.name}")
        try:
            organize_video(youtube_client, anthropic_client, work_dir)
        except Exception as e:
            print(
                f"WARNING: could not organize {work_dir.name} into playlists/comment: "
                f"{type(e).__name__}: {e}", file=sys.stderr,
            )
    except Exception as e:
        print(f"WARNING: YouTube upload failed for {work_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)


def _retry_pending_uploads(
    work_root: Path, settings: Settings, slugs: list[str] | None = None,
) -> dict:
    """Uploads every song named in `slugs` (default: every list_pending_uploads()
    result) via the same schedule_upload() code path as auto-upload and the
    manual Upload button -- backs the GUI's retry-upload controls for a song
    that failed to upload (e.g. YouTube's daily uploadLimitExceeded cap) and
    isn't self._last_work_dir. One song's failure is logged and skipped,
    never aborting the rest -- same "keep going" behavior as the batch
    pipeline worker, since a still-active daily cap would otherwise fail
    every remaining song in the same way anyway."""
    credentials = youtube_auth.load_credentials()
    if credentials is None:
        raise RuntimeError("Not connected to YouTube.")
    youtube_client = build("youtube", "v3", credentials=credentials)
    anthropic_client = anthropic.Anthropic()

    if slugs is None:
        slugs = list_pending_uploads(work_root)

    results: dict = {"succeeded": [], "failed": []}
    for slug in slugs:
        try:
            schedule_upload(youtube_client, anthropic_client, work_root / slug, settings)
            try:
                organize_video(youtube_client, anthropic_client, work_root / slug)
            except Exception as e:
                print(
                    f"WARNING: could not organize {slug} into playlists/comment: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
            results["succeeded"].append(slug)
        except Exception as e:
            results["failed"].append((slug, f"{type(e).__name__}: {e}"))
    return results


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE_PATH = PROJECT_ROOT / "VERSION"

# Height (px) of each scrollable song-list panel (Redo / Upload to YouTube /
# Pending Uploads) -- replaces the old CTkComboBox dropdown (a native OS menu
# that could run off-screen once a song list got long enough; owner request,
# 2026-09-15). First tried wrapping all three lists in an outer
# CTkScrollableFrame so the whole page would scroll -- reverted the same day:
# nesting a CTkScrollableFrame inside another one is unreliable in this
# customtkinter version (its scrollbar/mouse-wheel handling is keyed off a
# single bind_all per instance, and the outer one never actually scrolled for
# the owner, trapping everything below the Redo list). Each section now
# starts CLOSED (see _make_collapsible_section) and is opened on demand, so
# there's no more fight for vertical space -- ~15 rows visible per list,
# scrolling (via that list's own, proven-working scrollbar) for the rest.
SONG_LIST_HEIGHT = 420


def _open_with_default_app(path: Path) -> None:
    """Hands a rendered video off to whatever the OS already uses to play
    mp4s -- no in-app player, just a preview-before-uploading convenience
    (owner request, 2026-09-15)."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _default_work_dir_from_audio(audio_path: str) -> str:
    """Fallback work directory derived straight from the audio file's own
    name, used when Generate is clicked before (or despite) the GUI's own
    background title-identification (_on_audio_selected/_identify_worker)
    finishing -- that lookup can take a moment (it can fall back to a real
    network call when tags are missing) or fail silently by design, and
    neither should ever block Generate: run_pipeline's own identify stage
    re-resolves the real title from scratch regardless of what this GUI-side
    preview did or didn't manage first."""
    return str(PROJECT_ROOT / "work" / _slugify(Path(audio_path).stem))

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class _QueueWriter:
    """File-like object that pushes writes into a queue instead of a real
    stream -- lets stdout/stderr from the worker thread (moviepy's own
    progress output, and Demucs's, which separate._run_demucs relays through
    sys.stdout precisely so it lands here) reach the GUI's log widget
    instead of the terminal."""

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
        root.geometry("1400x1000")
        root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        self._queue: "queue.Queue" = queue.Queue()
        self._update_queue: "queue.Queue" = queue.Queue()
        self._current_version = current_version
        self._available_update: dict | None = None
        self._running = False
        self._identified_artist = ""  # set by _apply_identified_title, used for the YouTube title preview
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
        self.retry_upload_song_var = tk.StringVar()
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

        self.youtube_title_preview_var = tk.StringVar()
        ctk.CTkLabel(
            form, textvariable=self.youtube_title_preview_var, text_color="gray60",
            justify="left", anchor="w",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 8))

        button_row = ctk.CTkFrame(form, fg_color="transparent")
        button_row.grid(row=5, column=0, columnspan=3, pady=8)
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

        self.progress_bar = ctk.CTkProgressBar(left)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 10))

        def _populate_redo_list_now() -> None:
            self._populate_song_radio_list(
                self.redo_list_frame, "redo", list_redoable_songs(PROJECT_ROOT / "work"), self.redo_song_var,
            )

        redo_content, self._invalidate_redo_list = self._make_collapsible_section(
            left, "Redo an Existing Song", on_first_expand=_populate_redo_list_now,
        )
        self.redo_list_frame = ctk.CTkScrollableFrame(redo_content, height=SONG_LIST_HEIGHT)
        self.redo_list_frame.pack(fill="x", padx=8, pady=(0, 4))
        redo_controls = ctk.CTkFrame(redo_content, fg_color="transparent")
        redo_controls.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkCheckBox(
            redo_controls, text="Generate new images", variable=self.redo_new_images_var,
        ).pack(side="left", padx=8)
        self.redo_button = ctk.CTkButton(redo_controls, text="Redo", command=self._on_redo, width=80)
        self.redo_button.pack(side="left", padx=8)

        def _populate_upload_list_now() -> None:
            # auto_select_first=True unconditionally (not just on a later
            # refresh): this only ever runs when the owner actually opens the
            # section, never automatically at launch, so there's no "blank
            # dropdown at startup" state left to preserve here.
            self._populate_song_radio_list(
                self.retry_upload_list_frame, "upload", list_rendered_songs(PROJECT_ROOT / "work"),
                self.retry_upload_song_var, auto_select_first=True,
            )

        upload_content, self._invalidate_upload_list = self._make_collapsible_section(
            left, "Upload to YouTube", on_first_expand=_populate_upload_list_now,
        )
        self.retry_upload_list_frame = ctk.CTkScrollableFrame(upload_content, height=SONG_LIST_HEIGHT)
        self.retry_upload_list_frame.pack(fill="x", padx=8, pady=(0, 4))
        retry_upload_controls = ctk.CTkFrame(upload_content, fg_color="transparent")
        retry_upload_controls.pack(fill="x", padx=8, pady=(0, 8))
        self.retry_upload_button = ctk.CTkButton(
            retry_upload_controls, text="Upload", command=self._on_retry_upload, width=80,
        )
        self.retry_upload_button.pack(side="left", padx=8)

        def _add_pending_select_all(header: ctk.CTkFrame) -> None:
            self.pending_select_all_var = tk.BooleanVar(value=True)
            ctk.CTkCheckBox(
                header, text="Select All", variable=self.pending_select_all_var,
                command=self._on_toggle_pending_select_all,
            ).pack(side="right", padx=8)

        self._pending_upload_vars: dict[str, tk.BooleanVar] = {}
        pending_content, self._invalidate_pending_list = self._make_collapsible_section(
            left, "Pending YouTube Uploads", header_extra=_add_pending_select_all,
            on_first_expand=self._refresh_pending_uploads_list,
        )
        self.pending_uploads_list_frame = ctk.CTkScrollableFrame(pending_content, height=SONG_LIST_HEIGHT)
        self.pending_uploads_list_frame.pack(fill="x", padx=8, pady=(0, 4))
        pending_controls = ctk.CTkFrame(pending_content, fg_color="transparent")
        pending_controls.pack(fill="x", padx=8, pady=(0, 8))
        self.upload_selected_button = ctk.CTkButton(
            pending_controls, text="Upload Selected", command=self._on_upload_selected_pending, width=140,
        )
        self.upload_selected_button.pack(side="left", padx=8)

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

        # Settings moved into its own popup window (owner feedback,
        # 2026-09-11: the embedded tab felt cramped/"ugly") -- this column is
        # now just the YouTube panel plus the button that opens it. The panel
        # and its live preview are built fresh each time the window opens
        # (see _open_settings_window) rather than kept around permanently.
        self._settings_window: ctk.CTkToplevel | None = None
        settings_button_row = ctk.CTkFrame(right, fg_color="transparent")
        settings_button_row.pack(fill="x", padx=4, pady=(4, 0))
        ctk.CTkButton(settings_button_row, text="⚙ Settings", command=self._open_settings_window).pack(
            side="left"
        )

        youtube_connect_frame = ctk.CTkFrame(right, fg_color="transparent")
        youtube_connect_frame.pack(fill="x", padx=4, pady=(10, 0))
        self.youtube_status_var = tk.StringVar(value="YouTube: not connected")
        ctk.CTkLabel(youtube_connect_frame, textvariable=self.youtube_status_var, anchor="w").pack(side="left")
        ctk.CTkButton(
            youtube_connect_frame, text="Connect to YouTube", command=self._on_connect_youtube, width=160,
        ).pack(side="right")

        self._build_youtube_panel(right)
        self._build_pending_comments_panel(right)

    def _open_settings_window(self) -> None:
        if self._settings_window is not None:
            # Already open -- bring it to front rather than building a second
            # SettingsPanel bound to the same Settings object (which would
            # double-fire on_change and diverge from the real dirty baseline).
            self._settings_window.lift()
            self._settings_window.focus_force()
            return

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Settings")
        dialog_w, dialog_h = 640, 780
        self.root.update_idletasks()
        # Same transient+grab_set+lift/focus_force+brief-topmost treatment as
        # the Update Available dialog -- a plain Toplevel can open silently
        # behind the main window on some Linux window managers (Cinnamon
        # included; a documented recurring bug class on this project).
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(x, 0)}+{max(y, 0)}")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        dialog.attributes("-topmost", True)
        dialog.after(300, lambda: dialog.attributes("-topmost", False))
        self._settings_window = dialog

        # Suppressed while THIS popup's own SettingsPanel populates itself
        # (SettingsPanel.__init__'s trailing load_from() fires on_change()
        # once before this method's own `self.settings_panel = ...`
        # assignment below has completed) -- the same guard startup already
        # uses for the identical reason, just re-armed here since startup
        # only resets it once. Real bug found live, 2026-09-11: without
        # this, on_change -> self.settings_panel.collect() raised
        # AttributeError (attribute not yet assigned) partway through
        # SettingsPanel's own __init__, silently swallowed by Tkinter with
        # no visible error on a desktop-launched app -- aborting
        # construction before .pack() ever ran, leaving the whole panel
        # invisible below the live preview.
        self._suppress_settings_save = True
        self.settings_preview = SettingsPreviewFrame(dialog, self.settings)
        self.settings_preview.pack(fill="x", padx=6, pady=(6, 0))
        self.settings_panel = SettingsPanel(dialog, self.settings, on_change=self._on_settings_changed)
        self.settings_panel.pack(fill="both", expand=True, padx=6, pady=6)
        self._suppress_settings_save = False

        def _on_close() -> None:
            if self.settings_panel.has_unsaved_changes():
                if not messagebox.askyesno(
                    "Discard changes", "You have unsaved settings changes. Discard them and close?",
                ):
                    return
                # Reverts the widgets (and, via on_change, self.settings
                # itself) back to what's actually on disk -- calling the
                # panel's own Discard button would pop a SECOND confirmation
                # on top of this one, so this reuses its underlying action
                # directly instead.
                self.settings_panel.load_from(self.settings_panel._baseline)
            self._settings_window = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", _on_close)

    def _make_collapsible_section(
        self, parent, title: str, header_extra=None, on_first_expand=None,
    ):
        """A section that starts CLOSED (owner request, 2026-09-15, after the
        three song lists being open by default made the window unmanageably
        tall) -- clicking the header toggles a content frame the caller packs
        its own widgets into. `header_extra(header_row)`, if given, adds
        something that stays visible whether the section is open or not (the
        Pending list's Select All checkbox). `on_first_expand`, if given, is
        called once -- only the first time the section is actually opened,
        never during startup -- to build its (possibly expensive) contents
        lazily: real owner complaint, 2026-09-15, "always extremely slow" to
        launch -- eagerly building a CTkRadioButton/CTkCheckBox row (plus a
        Watch and a Remove button each) for every song in a 65-song work/
        folder across all three lists, whether or not anyone ever opens them,
        measured at 28 SECONDS of `LyricVideoGUI.__init__` alone (vs. 0.05s
        for the actual filesystem scan behind them -- CustomTkinter widget
        construction, not I/O, is what's slow here).

        Returns (content_frame, invalidate) -- `invalidate()` is for a caller
        whose underlying data changed (e.g. after an upload) to call INSTEAD
        of rebuilding the list itself: if the section is open it rebuilds
        right away (via `on_first_expand` again), but if it's closed it just
        marks the content stale for the next real open. Real owner
        complaint, 2026-09-15: rebuilding a still-CLOSED, never-opened list
        after every single upload was the exact same expensive-widget-
        construction cost as the launch-time bug above, just re-triggered on
        a different event -- freezing the window for a stretch even though
        nobody was even looking at that list."""
        section = ctk.CTkFrame(parent)
        section.pack(fill="x", padx=10, pady=(0, 10))
        header = ctk.CTkFrame(section, fg_color="transparent")
        header.pack(fill="x")
        content = ctk.CTkFrame(section, fg_color="transparent")
        state = {"expanded": False, "populated": on_first_expand is None}

        def populate_now() -> None:
            if on_first_expand is not None:
                on_first_expand()
            state["populated"] = True

        def toggle() -> None:
            if state["expanded"]:
                content.pack_forget()
                toggle_button.configure(text=f"▶ {title}")
            else:
                if not state["populated"]:
                    populate_now()
                content.pack(fill="x")
                toggle_button.configure(text=f"▼ {title}")
            state["expanded"] = not state["expanded"]

        def invalidate() -> None:
            if state["expanded"]:
                populate_now()
            else:
                state["populated"] = False

        toggle_button = ctk.CTkButton(
            header, text=f"▶ {title}", command=toggle, anchor="w",
            fg_color="transparent", hover_color=("gray80", "gray25"),
            font=ctk.CTkFont(weight="bold"),
        )
        toggle_button.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        if header_extra is not None:
            header_extra(header)
        return content, invalidate

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
        """SettingsPanel's on_change fires on every keystroke/slider-move/color-pick,
        live-updating the preview and the in-memory settings this session's own
        Generate/Redo/Batch will use -- but never the settings FILE on disk.
        SettingsPanel owns persistence entirely itself now (its own "Save Settings"
        button, with an itemized confirm dialog first): a change here becoming
        permanent the instant a slider gets nudged is exactly the real incident
        (2026-09-10) this split was built to prevent. Suppressed while the panel
        is still being populated on launch (Settings.load() itself is already the
        source of truth then)."""
        if self._suppress_settings_save:
            return
        self.settings = self.settings_panel.collect()
        self.settings_preview.update_preview(self.settings)

    def _on_title_changed(self, *_args) -> None:
        if not self._running:
            slug = _slugify(self.title_var.get())
            self.work_dir_var.set(str(PROJECT_ROOT / "work" / slug))
        self._update_youtube_title_preview()

    def _update_youtube_title_preview(self) -> None:
        """Shows the exact title that will be used on YouTube upload
        (build_play_along_title -- see youtube_metadata.py) next to the
        editable Song title field. This is a PREVIEW only: unlike the Song
        title field itself, it never feeds into identify/lyrics-search/the
        video filename -- those all need the bare title, not this combined
        display string (owner request, 2026-09-14, wants to see the real
        upload title without the two getting tangled together)."""
        title = self.title_var.get().strip()
        if not title:
            self.youtube_title_preview_var.set("")
            return
        self.youtube_title_preview_var.set(
            f"Will upload to YouTube as: {build_play_along_title(title, self._identified_artist)}"
        )

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
        self.root.after(0, lambda: self._apply_identified_title(info.title, info.artist))

    def _apply_identified_title(self, title: str, artist: str = "") -> None:
        self._identified_artist = artist
        if not self.title_var.get().strip():  # still empty -- no manual edit arrived meanwhile
            self.title_var.set(title)
        else:
            self._update_youtube_title_preview()  # title unchanged, but artist just arrived

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
                elif kind == "apply_up_to_date":
                    self._on_apply_update_up_to_date(payload)
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
        # lift()/focus_force() alone are NOT reliably honored by every Linux
        # window manager -- several (Cinnamon included) block focus-stealing
        # outright, so the dialog can still open silently behind the main
        # window despite the calls above (confirmed live, 2026-09-10, on
        # Linux Mint -- this is a second, harder recurrence of the exact bug
        # this function's docstring already describes once). Briefly forcing
        # "always on top" is the one approach every window manager actually
        # respects; clearing it a moment later avoids permanently pinning
        # this dialog above every other window on the desktop.
        dialog.attributes("-topmost", True)
        dialog.after(300, lambda: dialog.attributes("-topmost", False))
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
        # Real recurrence, 2026-09-10 (screenshots): with no `parent=` given,
        # this confirmation isn't WM-recognized as belonging to the "Update
        # available" dialog it's actually triggered from -- on the same
        # flaky window manager already found for that dialog, it could open
        # behind it instead of on top. `parent=dialog` gives it the correct
        # transient relationship; briefly forcing `dialog` itself topmost
        # around the call is the same belt-and-suspenders fix already used
        # for that dialog's own visibility.
        dialog = self._update_dialog_window
        if dialog is not None:
            dialog.attributes("-topmost", True)
        try:
            confirmed = messagebox.askyesno(
                "Apply update",
                f"Download and apply {release['tag_name']} now?\n\n"
                "This reinstalls dependencies if they changed and overwrites the "
                "program's own files. Your songs, work files, and .env are never "
                "touched.",
                parent=dialog,
            )
        finally:
            if dialog is not None:
                dialog.attributes("-topmost", False)
        if not confirmed:
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

                source_commit = read_release_source_commit(extracted_root)
                if is_source_commit_already_applied(source_commit, str(PROJECT_ROOT)):
                    write_local_version(str(_VERSION_FILE_PATH), release["tag_name"])
                    self._update_queue.put(("apply_up_to_date", release["tag_name"]))
                    return

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
                    pip_result = subprocess.run(
                        [str(venv_python(PROJECT_ROOT)), "-m", "pip", "install", "-r", "requirements.txt"],
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

    def _on_apply_update_up_to_date(self, tag_name: str) -> None:
        """This checkout's own git history already contains the commit
        release tag_name was built from -- no files were touched, only
        VERSION was recorded, so a stale/out-of-order release can never
        silently revert newer local commits (see
        is_source_commit_already_applied)."""
        self._update_status_var.set(
            f"Already up to date ({tag_name}) -- this checkout's own commits "
            "already include it, so no files were changed."
        )
        self._update_apply_button.pack_forget()
        ctk.CTkButton(
            self._update_button_frame, text="Relaunch Now", command=self._on_relaunch_clicked
        ).pack(side="left")

    def _on_apply_update_error(self, message: str) -> None:
        self._update_status_var.set(f"Update failed: {message}")
        self._update_apply_button.configure(state="normal")

    def _on_relaunch_clicked(self) -> None:
        subprocess.Popen([str(venv_python(PROJECT_ROOT)), "-m", "lyricvideo.gui"], cwd=str(PROJECT_ROOT))
        self.root.destroy()

    def _on_new_song(self) -> None:
        if self._running:
            return
        self._identified_artist = ""
        self.title_var.set("")
        self.audio_var.set("")
        self.work_dir_var.set("")  # after title_var -- overrides its own auto-fill trace
        self.status_var.set("Ready")
        self.progress_bar.set(0.0)
        self._clear_log()
        self._last_work_dir = None

    def _on_close_window(self) -> None:
        """Bound to the window's own close (X) button, the only way to quit
        this app -- if nothing is running, closes immediately; if a
        Generate/Redo/Batch is in progress, confirms first, since closing
        mid-run kills the pipeline (and any in-flight Demucs/render/upload
        work) partway through with no way to resume it. Owner-requested,
        2026-09-10."""
        if self._running and not messagebox.askyesno(
            "Quit while running?",
            "A video is currently being generated. Quitting now stops the "
            "process partway through -- it will not resume from where it left off.\n\n"
            "Quit anyway?",
        ):
            return
        self.root.destroy()

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
        if not Path(audio).is_file():
            # A typo'd/moved path used to surface only minutes later, as an
            # obscure Demucs or ffmpeg failure deep in the log.
            messagebox.showerror("Audio file not found", f"No such file:\n{audio}")
            return
        if not work_dir:
            work_dir = _default_work_dir_from_audio(audio)
            self.work_dir_var.set(work_dir)

        self._running = True
        self.generate_button.configure(state="disabled")
        self.redo_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
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
        if not audio_path.is_file():
            # The redo re-reads the ORIGINAL audio (sidecar lyrics check, and
            # the final render muxes it in) -- if the owner has since moved or
            # renamed that file, fail here with the path, before backing
            # anything up or starting a run that would die at render time.
            messagebox.showerror(
                "Original audio file not found",
                f'"{title}" was generated from:\n{audio_path}\n\n'
                "That file no longer exists. Put it back (or generate the song "
                "again from the moved file as a new song).",
            )
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
        self.batch_button.configure(state="disabled")
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
        """Refreshes the "YouTube: ..." status label on a background thread.
        Both load_credentials() (a token refresh is a real network call) and
        get_channel_title() (an API call) can block for seconds -- or for a
        whole network timeout when offline -- and this used to run them
        straight on the GUI thread from __init__, so the main window couldn't
        even finish appearing until YouTube answered (found by code review,
        2026-09-14). The 20-minute periodic tick already did it this way;
        the launch-time and post-connect refreshes now match it."""
        threading.Thread(target=self._refresh_youtube_status_worker, daemon=True).start()

    def _youtube_status_text(self) -> str:
        """The connect-status label's text, computed with real network calls
        -- only ever call this from a background thread."""
        credentials = youtube_auth.load_credentials()
        if credentials is None:
            return "YouTube: not connected"
        try:
            return f"YouTube: connected as {youtube_auth.get_channel_title(credentials)}"
        except Exception:
            return "YouTube: connected (channel name unavailable)"

    def _refresh_youtube_status_worker(self) -> None:
        text = self._youtube_status_text()
        self.root.after(0, lambda: self.youtube_status_var.set(text))

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
                # The message is formatted HERE, not inside the lambda: Python
                # unbinds `e` the moment this except block ends, so a lambda
                # that reads `e` later (from the Tk event loop, via after())
                # raised NameError instead of ever showing the dialog -- the
                # owner saw nothing at all when a connect failed (found by
                # static analysis, 2026-09-14; same latent bug in the manual
                # upload and Approve-reply workers below).
                message = f"{type(e).__name__}: {e}"
                self.root.after(0, lambda: messagebox.showerror("Could not connect to YouTube", message))

        threading.Thread(target=worker, daemon=True).start()

    def _on_retry_upload(self) -> None:
        if self._running:
            return
        slug = self.retry_upload_song_var.get().strip()
        if not slug:
            messagebox.showerror("No song selected", "Pick a song from the list.")
            return
        if load_youtube_state(PROJECT_ROOT / "work" / slug) is not None:
            # Already uploaded -- reachable for any past song now, not just
            # the one just generated, so a stray click shouldn't silently
            # duplicate a video already live on the channel.
            if not messagebox.askyesno(
                "Already uploaded",
                f'"{slug}" was already uploaded to YouTube. Upload again and create a duplicate video?',
            ):
                return
        self._start_retry_upload([slug])

    def _on_toggle_pending_select_all(self) -> None:
        value = self.pending_select_all_var.get()
        for var in self._pending_upload_vars.values():
            var.set(value)

    def _on_upload_selected_pending(self) -> None:
        if self._running:
            return
        slugs = [slug for slug, var in self._pending_upload_vars.items() if var.get()]
        if not slugs:
            messagebox.showerror("No songs selected", "Check at least one pending song to upload.")
            return
        self._start_retry_upload(slugs)

    def _start_retry_upload(self, slugs: list[str] | None) -> None:
        self.retry_upload_button.configure(state="disabled")
        self.upload_selected_button.configure(state="disabled")

        def worker():
            try:
                results = _retry_pending_uploads(PROJECT_ROOT / "work", self.settings, slugs)
            except Exception as e:
                message = f"{type(e).__name__}: {e}"  # see _on_connect_youtube: never read `e` inside the lambda
                self.root.after(0, lambda: messagebox.showerror("Upload failed", message))
                self.root.after(0, self._refresh_retry_upload_options)
                return
            self.root.after(0, lambda: self._on_retry_upload_done(results))

        threading.Thread(target=worker, daemon=True).start()

    def _on_retry_upload_done(self, results: dict) -> None:
        self._refresh_retry_upload_options()
        lines = [f"Uploaded {len(results['succeeded'])} song(s)."]
        if results["failed"]:
            lines.append(f"{len(results['failed'])} failed:")
            lines.extend(f"  {slug}: {reason}" for slug, reason in results["failed"])
        messagebox.showinfo("Retry upload results", "\n".join(lines))

    def _refresh_retry_upload_options(self) -> None:
        # invalidate(), not a direct rebuild: real owner complaint,
        # 2026-09-15 -- rebuilding a CLOSED, never-opened list of up to ~50
        # CTk widgets after every single upload was the same expensive-
        # widget-construction cost as the launch-time slowness fixed the
        # same day, just re-triggered by a different event. A closed
        # section just gets marked stale, rebuilt lazily next time it's
        # actually opened; an OPEN one still rebuilds immediately, same as
        # before.
        self._invalidate_upload_list()
        self.retry_upload_button.configure(state="normal")
        self.upload_selected_button.configure(state="normal")
        self._invalidate_pending_list()

    def _refresh_song_list(self, list_name: str) -> None:
        """Rebuilds a single list (by its dismissed_songs.py list_name) after
        a Remove click -- deliberately scoped to just that one list, since
        removal is per-list (owner request, 2026-09-15: a song dismissed from
        Pending Uploads should still be reachable via Upload to YouTube).
        Removing a row is only ever possible from a list that's already
        open, so these always rebuild immediately -- no need to route
        through invalidate() here."""
        if list_name == "redo":
            self._populate_song_radio_list(
                self.redo_list_frame, "redo", list_redoable_songs(PROJECT_ROOT / "work"),
                self.redo_song_var, auto_select_first=True,
            )
        elif list_name == "upload":
            self._populate_song_radio_list(
                self.retry_upload_list_frame, "upload", list_rendered_songs(PROJECT_ROOT / "work"),
                self.retry_upload_song_var, auto_select_first=True,
            )
        elif list_name == "pending":
            self._refresh_pending_uploads_list()

    def _populate_song_radio_list(
        self, frame: ctk.CTkScrollableFrame, list_name: str, all_songs: list[str], variable: tk.StringVar,
        auto_select_first: bool = False,
    ) -> None:
        """Rebuilds `frame`'s rows as a single-select list of radio buttons
        bound to `variable`, replacing the old CTkComboBox dropdown (a native
        OS menu that could run off-screen once a song list got long enough --
        owner request, 2026-09-15). `auto_select_first` reproduces the old
        _refresh_retry_upload_options behavior of snapping to the first song
        when the current selection no longer exists; the initial build never
        auto-selects, matching the old dropdown's blank starting state."""
        songs = [s for s in all_songs if s not in load_dismissed(list_name)]
        for child in frame.winfo_children():
            child.destroy()
        if variable.get() not in songs:
            variable.set((songs[0] if songs and auto_select_first else ""))
        for song in songs:  # already alphabetical -- see list_redoable_songs/list_rendered_songs
            try:
                self._build_song_list_row(
                    frame, list_name, song,
                    lambda row, song=song: ctk.CTkRadioButton(row, text=song, variable=variable, value=song),
                )
            except Exception as e:
                # One bad row must never blank the WHOLE list silently --
                # real owner-observed symptom, 2026-09-15: a "Pending
                # YouTube Uploads" list showed nothing at all despite real
                # pending songs on disk, with no visible error anywhere
                # (a Tkinter callback exception just gets printed to
                # stderr, easy to miss on a desktop launch).
                print(f"WARNING: could not build a list row for {song!r}: {type(e).__name__}: {e}", file=sys.stderr)
        if not songs:
            ctk.CTkLabel(frame, text="(none)", text_color="gray60").pack(anchor="w", padx=6, pady=6)

    def _refresh_pending_uploads_list(self) -> None:
        """Rebuilds the Pending Uploads checklist from the filesystem (never
        cached) -- schedule_upload() only writes youtube_state.json AFTER a
        successful upload, so a song simply stops appearing here once it
        succeeds, with no separate bookkeeping needed for "leaving the list"
        (owner request, 2026-09-15)."""
        for child in self.pending_uploads_list_frame.winfo_children():
            child.destroy()
        dismissed = load_dismissed("pending")
        select_all = self.pending_select_all_var.get()
        pending_slugs = [s for s in list_pending_uploads(PROJECT_ROOT / "work") if s not in dismissed]
        self._pending_upload_vars = {}
        for slug in pending_slugs:
            var = tk.BooleanVar(value=select_all)
            self._pending_upload_vars[slug] = var
            try:
                self._build_song_list_row(
                    self.pending_uploads_list_frame, "pending", slug,
                    lambda row, var=var: ctk.CTkCheckBox(row, text=slug, variable=var),
                )
            except Exception as e:
                # See the identical guard in _populate_song_radio_list --
                # one bad row must never blank the whole list silently.
                print(f"WARNING: could not build a pending-upload row for {slug!r}: {type(e).__name__}: {e}",
                      file=sys.stderr)
        if not pending_slugs:
            ctk.CTkLabel(self.pending_uploads_list_frame, text="(none)", text_color="gray60").pack(
                anchor="w", padx=6, pady=6
            )

    def _build_song_list_row(self, frame, list_name: str, song: str, selector_factory) -> None:
        """One row in a song list: the selector (radio or checkbox, built by
        `selector_factory`) on the left, a Watch and a Remove button on the
        right (owner request, 2026-09-15 -- preview a video before deciding
        whether to upload it, and get a long-since-handled song out of the
        way without touching its files)."""
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x", padx=2, pady=1)
        ctk.CTkButton(
            row, text="✕", width=28, fg_color="gray30", hover_color="#8b2020",
            command=lambda: self._on_remove_song(list_name, song),
        ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            row, text="▶ Watch", width=70, fg_color="gray30", hover_color="gray20",
            command=lambda: self._on_watch_song(song),
        ).pack(side="right", padx=(4, 0))
        selector_factory(row).pack(side="left", anchor="w")

    def _on_watch_song(self, slug: str) -> None:
        video_path = song_video_path(PROJECT_ROOT / "work" / slug)
        if video_path is None:
            messagebox.showerror("No video yet", f'"{slug}" has not been rendered yet.')
            return
        try:
            _open_with_default_app(video_path)
        except Exception as e:
            messagebox.showerror("Could not open video", f"{type(e).__name__}: {e}")

    def _on_remove_song(self, list_name: str, slug: str) -> None:
        if not messagebox.askyesno(
            "Remove from list",
            f'Remove "{slug}" from this list?\n\n'
            "This only changes what shows up here -- the song's files aren't "
            "touched, and it can still be found in this app's other lists.",
        ):
            return
        dismiss_song(list_name, slug)
        self._refresh_song_list(list_name)

    def _build_youtube_panel(self, parent) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=4, pady=(6, 6))
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(header, text="YouTube Comments", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="Check Now", command=self._on_check_youtube_comments, width=100).pack(side="right")
        self.youtube_replies_frame = ctk.CTkScrollableFrame(frame)
        self.youtube_replies_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
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
                message = f"{type(e).__name__}: {e}"  # see _on_connect_youtube: never read `e` inside the lambda
                self.root.after(0, lambda: messagebox.showerror("Could not post reply", message))

        threading.Thread(target=worker, daemon=True).start()

    def _on_dismiss_reply(self, reply: PendingReply) -> None:
        remove_pending_reply(reply.comment_id)
        self._render_pending_replies()

    def _build_pending_comments_panel(self, parent) -> None:
        content, self._invalidate_pending_comments = self._make_collapsible_section(
            parent, "Pending Engagement Comments", on_first_expand=self._refresh_pending_comments,
        )
        self.pending_comments_frame = ctk.CTkScrollableFrame(content, height=SONG_LIST_HEIGHT)
        self.pending_comments_frame.pack(fill="x", padx=8, pady=(0, 8))

    def _refresh_pending_comments(self) -> None:
        for child in self.pending_comments_frame.winfo_children():
            child.destroy()
        comments = load_pending_comments()
        for comment in comments:
            try:
                self._render_one_pending_comment(comment)
            except Exception as e:
                print(
                    f"WARNING: could not build a pending-comment row for {comment.video_id!r}: "
                    f"{type(e).__name__}: {e}", file=sys.stderr,
                )
        if not comments:
            ctk.CTkLabel(self.pending_comments_frame, text="(none)", text_color="gray60").pack(
                anchor="w", padx=6, pady=6
            )

    def _render_one_pending_comment(self, comment: PendingComment) -> None:
        row = ctk.CTkFrame(self.pending_comments_frame)
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(row, text=comment.song_title, anchor="w", font=ctk.CTkFont(weight="bold")).pack(
            fill="x", padx=6, pady=(6, 2)
        )
        text_box = ctk.CTkTextbox(row, height=60)
        text_box.insert("1.0", comment.draft_text)
        text_box.pack(fill="x", padx=6, pady=(0, 4))
        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkButton(
            buttons, text="Approve", width=80, command=lambda: self._on_approve_comment(comment, text_box),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            buttons, text="Dismiss", width=80, fg_color="gray30", hover_color="gray20",
            command=lambda: self._on_dismiss_comment(comment),
        ).pack(side="left")

    def _on_approve_comment(self, comment: PendingComment, text_box) -> None:
        text = text_box.get("1.0", "end").strip()

        def worker():
            credentials = youtube_auth.load_credentials()
            if credentials is None:
                return
            try:
                youtube_client = build("youtube", "v3", credentials=credentials)
                post_top_level_comment(youtube_client, comment.video_id, text)
                remove_pending_comment(comment.video_id)
                _mark_engagement_comment_posted(comment.video_id)
                self.root.after(0, self._invalidate_pending_comments)
                self.root.after(0, lambda: messagebox.showinfo(
                    "Comment posted",
                    "Posted. Remember to pin it from YouTube Studio -- the API has no way to do that part.",
                ))
            except Exception as e:
                # See _on_connect_youtube: never read `e` inside the lambda.
                message = f"{type(e).__name__}: {e}"
                self.root.after(0, lambda: messagebox.showerror("Could not post comment", message))

        threading.Thread(target=worker, daemon=True).start()

    def _on_dismiss_comment(self, comment: PendingComment) -> None:
        remove_pending_comment(comment.video_id)
        self._invalidate_pending_comments()

    def _on_check_youtube_comments(self) -> None:
        threading.Thread(target=self._check_youtube_comments_worker, daemon=True).start()

    def _check_youtube_comments_worker(self) -> None:
        """Runs on a background thread, both on-demand (Check Now) and every
        20 minutes via _youtube_periodic_tick -- an unhandled exception here
        would otherwise recur forever on every future tick with no visible
        indication beyond a scary traceback in the log, so the whole body is
        one last defensive layer on top of the per-video isolation below."""
        try:
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
                try:
                    # A still-scheduled (private) or otherwise non-public video
                    # always returns commentsDisabled for a comment read -- not
                    # an error, just not applicable yet -- so skip it instead of
                    # making (and logging a warning for) a call known to fail.
                    if not is_video_public(youtube_client, state.video_id):
                        continue
                    comments = list_new_comments(youtube_client, state.video_id, seen_ids)
                except Exception as e:
                    # Real live crash, 2026-09-10: a video with comments disabled
                    # (a completely normal state, not an error) raised
                    # HttpError 403 here, uncaught -- one bad video was silently
                    # aborting the check for every OTHER video too, forever,
                    # since the same failure recurs every 20-minute tick. One
                    # video's failure must never block checking the rest.
                    print(
                        f"WARNING: could not check comments for {work_dir.name} "
                        f"({state.video_id}): {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    continue
                for comment in comments:
                    draft, is_error_report = draft_comment_reply(anthropic_client, comment.text, state.title)
                    add_pending_reply(PendingReply(
                        comment_id=comment.comment_id, video_id=comment.video_id, author=comment.author,
                        comment_text=comment.text, draft_reply=draft, is_error_report=is_error_report,
                    ))
                    new_ids.append(comment.comment_id)
            if new_ids:
                mark_comments_seen(new_ids)
            self.root.after(0, self._render_pending_replies)
        except Exception as e:
            print(f"WARNING: YouTube comment check failed: {type(e).__name__}: {e}", file=sys.stderr)

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
        status_text = self._youtube_status_text()
        self.root.after(0, lambda: self.youtube_status_var.set(status_text))
        # Returns immediately on its own when there are no credentials.
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
                self._refresh_retry_upload_options()
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
