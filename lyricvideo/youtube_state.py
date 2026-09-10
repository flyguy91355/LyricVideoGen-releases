"""Per-song YouTube upload tracking -- lets the app know which of its own
uploads exist, so Redo can skip auto-re-uploading a song it already posted.
See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("playalongvideoproduction")

STATE_FILENAME = "youtube_state.json"


@dataclass(frozen=True)
class YoutubeState:
    video_id: str
    uploaded_at: str
    title: str


def load_youtube_state(work_dir: Path) -> YoutubeState | None:
    path = work_dir / STATE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return YoutubeState(**data)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Could not read YouTube state at %s: %s", path, exc)
        return None


def save_youtube_state(work_dir: Path, state: YoutubeState) -> None:
    path = work_dir / STATE_FILENAME
    path.write_text(json.dumps(asdict(state)), encoding="utf-8")
