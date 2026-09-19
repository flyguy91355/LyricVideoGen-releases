"""Which already-uploaded videos may need replacing on YouTube?

    python -m lyricvideo.replace_report [--out FILE]

READ-ONLY: lists the uploaded songs whose lyrics the audio check (and the AI judge) could not confirm,
with what YouTube says about each (still scheduled, or public and how many views), so the owner can
decide. It never deletes, edits or uploads anything -- replacing a video means fixing its lyrics first
(the check can point at the problem but cannot reliably write the right lyrics), re-rendering, uploading
the new one and only then removing the old one, all of which is the owner's call per song."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import load_song
from .verify_lyrics import UNCHECKED_HOLD
from .youtube_state import load_youtube_state

_WATCH_URL = "https://www.youtube.com/watch?v="


@dataclass(frozen=True)
class UploadedSong:
    slug: str
    title: str
    video_id: str
    concern: str


@dataclass(frozen=True)
class VideoFacts:
    privacy: str                       # public | private | unlisted
    publish_at: datetime | None        # a still-scheduled video's go-live time
    published_at: datetime | None
    views: int


def uploaded_songs_needing_review(work_root: Path) -> list[UploadedSong]:
    songs: list[UploadedSong] = []
    for entry in sorted(Path(work_root).iterdir(), key=lambda e: e.name):
        timed = entry / "lyrics_timed.json"
        state = load_youtube_state(entry) if entry.is_dir() else None
        if state is None or not timed.exists():
            continue
        try:
            song = load_song(timed)
        except Exception:
            continue
        if song.lyrics_accuracy_concern and song.lyrics_accuracy_concern != UNCHECKED_HOLD:
            songs.append(UploadedSong(entry.name, song.title, state.video_id, song.lyrics_accuracy_concern))
    return songs


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_video_facts(youtube_client, video_ids: list[str]) -> dict[str, VideoFacts]:
    """Facts for each video that still exists (a deleted one is simply absent). Batches of 50."""
    facts: dict[str, VideoFacts] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        response = youtube_client.videos().list(part="snippet,status,statistics", id=",".join(batch)).execute()
        for item in response.get("items", []):
            status = item.get("status", {})
            facts[item["id"]] = VideoFacts(
                privacy=status.get("privacyStatus", ""),
                publish_at=_parse_time(status.get("publishAt")),
                published_at=_parse_time(item.get("snippet", {}).get("publishedAt")),
                views=int(item.get("statistics", {}).get("viewCount", 0) or 0),
            )
    return facts


def _line(song: UploadedSong, fact: VideoFacts | None) -> str:
    extra = ""
    if fact is not None:
        if fact.publish_at is not None:
            extra = f" -- goes public {fact.publish_at.astimezone():%a %b %d %H:%M}"
        else:
            extra = f" -- {fact.views:,} views"
    return f"- **{song.title}**{extra}\n  {_WATCH_URL}{song.video_id}\n  {song.concern}\n"


def build_report(songs: list[UploadedSong], facts: dict[str, VideoFacts] | None) -> str:
    out = ["# Uploaded videos whose lyrics could not be confirmed", ""]
    out.append(
        "A flag means the audio check and the AI judge could not confirm the lyrics -- it is a reason to "
        "listen, not proof the lyrics are wrong (loud or produced recordings defeat the speech "
        "recognizer). Nothing has been changed on YouTube.\n"
    )
    if facts is None:
        out.append("_The videos' current status could not be checked on YouTube right now (not connected, or in a "
                   "quota cooldown), so every video is listed together._\n")
        out += [_line(s, None) for s in songs]
        return "\n".join(out)

    now = datetime.now(timezone.utc)
    scheduled = [s for s in songs if s.video_id in facts and facts[s.video_id].publish_at and facts[s.video_id].publish_at > now]
    public = [s for s in songs if s.video_id in facts and s not in scheduled]
    missing = [s for s in songs if s.video_id not in facts]

    out.append(f"## Still scheduled -- not public yet, so easiest to fix ({len(scheduled)})\n")
    out += [_line(s, facts[s.video_id]) for s in sorted(scheduled, key=lambda s: facts[s.video_id].publish_at)] or ["None.\n"]
    out.append(f"## Already public -- most viewed first ({len(public)})\n")
    out += [_line(s, facts[s.video_id]) for s in sorted(public, key=lambda s: -facts[s.video_id].views)] or ["None.\n"]
    out.append(f"## Saved as uploaded but no longer on YouTube ({len(missing)})\n")
    out += [_line(s, None) for s in missing] or ["None.\n"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List uploaded videos whose lyrics could not be confirmed.")
    parser.add_argument("--work-root", default="work", type=Path)
    parser.add_argument("--out", type=Path, default=Path.home() / ".playalongvideoproduction" / "lyrics_replacement_report.md")
    args = parser.parse_args(argv)

    from datetime import datetime as _dt

    from googleapiclient.discovery import build

    from . import youtube_auth
    from .youtube_quota_state import load_quota_blocked_until

    songs = uploaded_songs_needing_review(args.work_root)
    facts = None
    credentials = youtube_auth.load_credentials()
    blocked_until = load_quota_blocked_until()
    in_cooldown = blocked_until is not None and _dt.now().astimezone() < blocked_until
    if songs and credentials is not None and not in_cooldown:
        try:
            facts = fetch_video_facts(build("youtube", "v3", credentials=credentials), [s.video_id for s in songs])
        except Exception as e:
            print(f"Could not read YouTube ({type(e).__name__}); listing without it.", file=sys.stderr)
    text = build_report(songs, facts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n(Saved to {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
