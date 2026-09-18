"""Pure persistence for how many times THIS APP has successfully called
upload_video() on the current local calendar day -- backs
Settings.youtube_max_uploads_per_day, a real ceiling on raw upload
volume (each upload costs ~1600 of YouTube's default 10,000-unit daily
quota, so even 6-7 in a row can exhaust it), deliberately a SEPARATE
field from youtube_uploads_per_day (which only sizes the Settings
panel's publish-time-slot generator in youtube_schedule.py, unrelated).
A local counter is safe here, unlike the 2026-09-13 publish-slot counter
this must not be confused with: that one broke because it tried to
PREDICT a schedule that could also be mutated out-of-band (a manual
publish, a Studio edit); this one only records a fact this app alone
ever produces -- its own successful upload calls -- so nothing outside
the app can make it drift. Rolls over automatically at the local
calendar-day boundary, no explicit reset needed. See gui.py's
_maybe_upload_to_youtube / _retry_pending_uploads."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
UPLOAD_COUNT_FILE = CREDENTIALS_DIR / "youtube_upload_count.json"


def load_uploads_today(path: Path = UPLOAD_COUNT_FILE) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["date"] != date.today().isoformat():
            return 0  # a stale count from a previous day -- today starts fresh
        return int(data["count"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def record_upload(path: Path = UPLOAD_COUNT_FILE) -> None:
    count = load_uploads_today(path) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": date.today().isoformat(), "count": count}), encoding="utf-8")
