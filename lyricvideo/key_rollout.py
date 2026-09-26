"""Re-checks the key of a song that is already made but NOT on YouTube (owner, 2026-09-26): the videos made before the key
check existed carry the old average-pitch guess, which was wrong for about a third of the songs checked. The saved chords
are re-read (no audio, no Demucs), the chord-based estimate and the second opinion are compared, and the result is recorded
exactly as a fresh run would (key_decision.py). A song already on YouTube is never touched. Used by scripts/settle_keys.py."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .key_decision import settle_song_key
from .key_estimate import estimate_key_from_chords, parse_key
from .models import load_song, save_song
from .pipeline import HELD_MARKER
from .youtube_state import STATE_FILENAME


@dataclass
class RolloutResult:
    slug: str
    action: str            # skipped-uploaded | no-chords | already-right | corrected | review
    old_key: str = ""
    new_key: str = ""
    detail: str = ""


def _set_aside(folder: Path) -> list[str]:
    """Renames every finished video in `folder` to <name>.previous.mp4 (the same convention a Redo hold uses), so the
    old video, with its wrong key burned in, is no longer a song's current video."""
    moved = []
    for video in sorted(folder.glob("*.mp4")):
        if video.name.endswith(".previous.mp4"):
            continue
        video.replace(video.with_name(video.stem + ".previous.mp4"))
        moved.append(video.name)
    return moved


def settle_saved_song(work_dir: Path, anthropic_client, *, apply: bool) -> RolloutResult:
    work_dir = Path(work_dir)
    slug = work_dir.name
    if (work_dir / STATE_FILENAME).exists():
        return RolloutResult(slug, "skipped-uploaded")
    timed_path = work_dir / "lyrics_timed.json"
    song = load_song(timed_path)
    if estimate_key_from_chords(song.chord_track) is None:
        return RolloutResult(slug, "no-chords")
    artist = ""
    try:
        info = json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
        artist = str(info.get("artist") or "")
    except (OSError, ValueError):
        pass
    old_key = song.chord_track.key
    decision, track = settle_song_key(work_dir, song.chord_track, song.title, artist, anthropic_client, save=apply)
    if not decision.confirmed:
        return RolloutResult(slug, "review", old_key, decision.chord_key, decision.concern())
    if parse_key(old_key) == parse_key(decision.key):
        return RolloutResult(slug, "already-right", old_key, decision.key)
    if apply:
        song.chord_track = track
        save_song(song, timed_path)
        moved = _set_aside(work_dir)
        easy = work_dir / "easychords"
        if easy.is_dir():
            moved += [f"easychords/{name}" for name in _set_aside(easy)]
        (work_dir / HELD_MARKER).write_text(json.dumps({
            "reason": f"Key check: the key was corrected from {old_key} to {decision.key}; the video has to be made again "
                      "(Render Anyway -- the images already bought are reused).",
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
    return RolloutResult(slug, "corrected", old_key, decision.key)
