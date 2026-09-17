"""Pure persistence for YouTube API quota cooldown state: once an upload
hits the channel's daily quota, this records when it's safe to try again
so the app can back off instead of repeating the same failure, and
automatically resume once the wait is over. See gui.py's
_maybe_upload_to_youtube / _retry_pending_uploads / _youtube_periodic_tick."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
QUOTA_STATE_FILE = CREDENTIALS_DIR / "youtube_quota_state.json"


def load_quota_blocked_until(path: Path = QUOTA_STATE_FILE) -> datetime | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["blocked_until"])
    except (OSError, ValueError, KeyError):
        return None


def save_quota_blocked_until(blocked_until: datetime, path: Path = QUOTA_STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"blocked_until": blocked_until.isoformat()}), encoding="utf-8")
