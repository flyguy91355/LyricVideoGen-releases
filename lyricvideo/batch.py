"""Finds audio files in a folder and resolves each one's identity ahead of
actually running the pipeline on any of them -- backs the GUI's "Batch: Process
a Folder" feature. See
docs/superpowers/specs/2026-09-10-batch-folder-processing-design.md."""

from __future__ import annotations

import ctypes
import gc
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from .identify import extract_metadata
from .dismissed_songs import load_dismissed
from .pipeline import held_before_video, slugify
from .text_clean import artist_key

log = logging.getLogger("playalongvideoproduction")

_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac"}

# Separate from Settings (lyricvideo/settings.py) on purpose: Settings is
# owner-tunable render config, wholesale-replaced from the Settings panel's
# widgets on every change (SettingsPanel.collect()) -- a field with no widget
# behind it would get silently reset to its default the next time any slider
# moves. This is just remembered GUI convenience state, so it gets its own
# tiny file instead.
_STATE_FILE = Path.home() / ".playalongvideoproduction" / "batch_state.json"


def load_last_batch_folder(path: Path = _STATE_FILE) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("last_folder", "")
    except (OSError, ValueError):
        return ""


def save_last_batch_folder(folder: str, path: Path = _STATE_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_folder": folder}), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues
        log.warning("Could not save last batch folder: %s", exc)


def resolve_existing_folder(folder: Path) -> Path:
    """Returns `folder` unchanged if it exists. Otherwise, looks for a sibling
    in its parent whose name matches once leading/trailing whitespace is
    stripped from both sides -- confirmed live 2026-09-10: the OS folder-picker
    dialog silently drops a trailing space from a real folder named
    "batch music ", so the path handed to this app no longer matches the real
    directory on disk even though the user picked it correctly. If exactly one
    sibling matches, that real path is returned; otherwise `folder` is returned
    unchanged so the original FileNotFoundError still surfaces normally."""
    if folder.is_dir():
        return folder
    target = folder.name.strip()
    try:
        candidates = [p for p in folder.parent.iterdir() if p.is_dir() and p.name.strip() == target]
    except OSError:
        return folder
    if len(candidates) == 1:
        return candidates[0]
    return folder


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
    resume_stage: str = "identify"


# --- one work folder per recording (issue #7 review, F027/F034) -------------------------------------------------------
# A work folder used to be keyed on the title slug alone, so a second recording with the same title (a cover: two
# "Hurt"s, or "Hurt (Live)" once identify strips the "(Live)") ran in the first one's folder: it overwrote that song's
# video and timing, inherited its youtube_state.json (so it never uploaded) and its key_owner.json / lyrics_owner.txt
# (the wrong key and lyrics, both "always win"). A folder that already holds a DIFFERENT recording is now left alone.

def stored_song_identity(folder: Path) -> tuple[str, str, str] | None:
    """(title, artist, audio file name) of the song a work folder already holds -- from song_info.json and the audio
    path lyrics_timed.json records -- or None when the folder holds no song yet (missing, or nothing saved in it)."""
    folder = Path(folder)
    info_path, timed_path = folder / "song_info.json", folder / "lyrics_timed.json"
    if not info_path.exists() and not timed_path.exists():
        return None
    title = artist = audio_name = ""
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if isinstance(info, dict):
            title, artist = str(info.get("title") or ""), str(info.get("artist") or "")
    except (OSError, ValueError):
        pass
    try:
        timed = json.loads(timed_path.read_text(encoding="utf-8"))
        if isinstance(timed, dict):
            audio_name = Path(str(timed.get("audio_path") or "")).name
            title = title or str(timed.get("title") or "")
    except (OSError, ValueError):
        pass
    return title, artist, audio_name


def _same_artist(a: str, b: str) -> bool | None:
    """True/False when both names are known; None when either is unknown. 'The Beatles' is 'Beatles', and a credit
    that only adds names ('Johnny Cash' / 'Johnny Cash & June Carter') still counts as the same act."""
    ka, kb = artist_key(a or ""), artist_key(b or "")
    if not ka or not kb:
        return None
    wa, wb = set(ka.split()), set(kb.split())
    return ka == kb or wa <= wb or wb <= wa


def _holds_this_audio_file(folder: Path, audio_path: Path | None) -> bool:
    """True when `folder` holds run_pipeline's copy of this very file (same name, same size) -- proof it is this song,
    whatever a title/artist lookup answers this time (lrclib/MusicBrainz can name a tagless file's artist differently
    from one run to the next, and a Batch rerun must never move a finished song to a new folder and upload it twice)."""
    if audio_path is None:
        return False
    try:
        audio_path = Path(audio_path)
        copy = Path(folder) / audio_path.name
        return copy.is_file() and audio_path.is_file() and copy.stat().st_size == audio_path.stat().st_size
    except OSError:
        return False


def holds_other_recording(folder: Path, artist: str, audio_path: Path | None = None) -> bool:
    """True when `folder` already holds a song that is NOT this recording: its artist and `artist` are both known and
    differ. The same artist, or an artist unknown on either side, counts as this song -- so every existing folder keeps
    mapping to itself, and Redo / a Batch rerun of the same song still find their folder. A folder holding this very
    audio file (`audio_path`: its copy, same name and size) is always this song. (A different file name proves nothing:
    a rip of the same song is often renamed.)"""
    stored = stored_song_identity(folder)
    if stored is None:
        return False
    if _holds_this_audio_file(folder, audio_path):
        return False
    _title, stored_artist, _audio_name = stored
    return _same_artist(stored_artist, artist) is False


def work_dir_for(
    work_root: Path, title: str, artist: str, audio_path: Path, taken: set[str] | None = None,
) -> Path:
    """The work folder for this recording: work_root/<title slug> when it is free or already this song's; otherwise
    <title-artist> (<title-file name> when the artist is unknown), then -2, -3... `taken` holds folder names already
    handed to other files in the same batch -- two files are never given one folder."""
    taken = taken if taken is not None else set()
    audio_path = Path(audio_path)
    base = slugify(title)
    fallback = slugify(f"{title} {artist}") if (artist or "").strip() else slugify(f"{title} {audio_path.stem}")
    candidates = [base, fallback] + [f"{fallback}-{n}" for n in range(2, 100)]
    for name in dict.fromkeys(candidates):
        if name in taken:
            continue
        if not holds_other_recording(work_root / name, artist, audio_path):
            return work_root / name
    return work_root / f"{fallback}-{len(taken) + 100}"


def resolve_batch_items(files: list[Path], work_root: Path) -> list[BatchItem]:
    """Resolves each file's title (free: tags/filename/lrclib/MusicBrainz, no
    Claude/Replicate spend -- same cost profile as the single-song title
    auto-fill), work directory, and whether it already has a finished video.
    A file whose metadata extraction itself raises still gets a usable,
    unique identity -- its own filename stem, never a shared constant --
    so two different failing files in the same batch never collide on the
    same work_dir. Order is preserved from `files`.

    resume_stage is "fetch_lyrics" instead of the default "identify" when
    Demucs stems already exist for this item (same htdemucs/<audio_stem>/
    path convention run_pipeline() itself uses) -- a song interrupted after
    separate() completed (app closed/crashed mid-batch) would otherwise
    redo the slowest stage in the whole pipeline from scratch on the next
    Start Batch, exactly like a completed song's own reprocessing already
    resumes past it (real owner complaint, 2026-09-18: closing mid-batch
    left "no way to resume" the interrupted song).

    Each file gets its own work folder (work_dir_for): a folder that already holds a different recording with the same
    title, or one handed to an earlier file of this batch by another (or an unknown) artist, is never reused -- so
    already_done means "THIS recording
    already has a video" (issue #7 review, F027/F034)."""
    items: list[BatchItem] = []
    assigned: dict[str, str] = {}     # folder name -> the artist of the file this batch gave it to
    for audio_path in files:
        try:
            info = extract_metadata(audio_path)
        except Exception:
            title, artist = audio_path.stem, ""
        else:
            title, artist = info.title, str(getattr(info, "artist", "") or "")
        # A folder already handed to the SAME known artist's file is shared, as it always was: two rips of one song
        # ("Song.mp3", "Song (1).mp3") in their own folders would each be made and uploaded -- a duplicate video.
        taken = {name for name, other in assigned.items() if _same_artist(other, artist) is not True}
        work_dir = work_dir_for(work_root, title, artist, audio_path, taken)
        assigned.setdefault(work_dir.name, artist)
        final_video = work_dir / f"{slugify(title)}.mp4"
        demucs_dir = work_dir / "htdemucs" / audio_path.stem
        has_stems = (demucs_dir / "vocals.wav").exists() and (demucs_dir / "no_vocals.wav").exists()
        items.append(BatchItem(
            audio_path=audio_path, title=title, work_dir=work_dir,
            # A held-before-video song, or one the owner removed from review, counts as processed: a Batch leaves it alone.
            already_done=final_video.exists() or held_before_video(work_dir) or work_dir.name in load_dismissed("flagged"),
            resume_stage="fetch_lyrics" if has_stems else "identify",
        ))
    return items


def release_memory() -> None:
    """Called after every song in a long Batch run. CPython's own refcounting
    frees most per-song objects immediately, but gc.collect() is still needed
    for reference cycles (torch tensors/models commonly form them), and even
    after that, glibc's malloc doesn't hand freed arenas back to the OS on its
    own -- so a long-lived process's RSS climbs across dozens of songs even
    though nothing is actually leaking live references. Real incident,
    2026-09-18: earlyoom killed the app on song #24 of an overnight 100-song
    run after available memory crept from 46% down to 2% over several hours,
    then a normal-length song's own align-stage usage tipped it over."""
    gc.collect()
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except OSError:
            pass
