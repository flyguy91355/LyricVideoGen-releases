"""Re-renders the YouTube description of videos ALREADY uploaded with the owner's support template
(Settings.support_description_text: text above the song description, the marker {description}, text below -- see
youtube_schedule.render_description). It removes the old bottom-of-description line (default
"Support: https://ko-fi.com/playalongvideos") and any earlier copy of the template, keeps the song's own text, and
writes the new layout. Owner, 2026-09-26.

  .venv/bin/python scripts/update_support_description.py --only blackbird            # one song
  .venv/bin/python scripts/update_support_description.py --status scheduled --dry-run # preview the scheduled ones
  .venv/bin/python scripts/update_support_description.py --status scheduled          # every scheduled (private + publish date) video
  .venv/bin/python scripts/update_support_description.py                             # every uploaded video

--status: scheduled = private with a publish date, public = already public, all = everything (default).
A "📌 Song key: ..." correction note (scripts/add_key_note.py) always stays the very first paragraph, above the template.
Safe to re-run: a video already in the new layout is skipped. Before changing anything, the current snippet of every
video about to change is saved to reports/support_text_backup_<timestamp>.json so it can be put back. Stops cleanly if
YouTube's daily quota runs out (each update costs 50 units) -- run it again later to finish. Only the description
changes: title, tags, category and the private/scheduled status are left exactly as they are."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.settings import Settings
from lyricvideo.youtube import is_quota_exceeded_error
from lyricvideo.youtube_auth import load_credentials
from lyricvideo.youtube_schedule import rerender_description
from lyricvideo.youtube_state import uploaded_song_dirs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"
DEFAULT_OLD_TEXT = "Support: https://ko-fi.com/playalongvideos"


def find_uploaded_videos(work_dir: Path | None = None) -> dict[str, str]:
    """video_id -> a label (the song's folder, e.g. "blackbird" or "blackbird/easychords")."""
    return {state.video_id: slug for slug, _path, state in uploaded_song_dirs(work_dir or WORK_DIR)}


def new_description(current: str, template: str, old_texts: list[str]) -> str:
    """The description this script writes: the current support-template layout, with a "📌 Song key" note (from
    scripts/add_key_note.py) kept as the very first paragraph -- re-rendering must never push it below the tip line,
    or a later add_key_note run would add a second note (issue #7 review, F054)."""
    return rerender_description(current, template, old_texts)


def video_kind(status: dict) -> str:
    if status.get("privacyStatus") == "private" and status.get("publishAt"):
        return "scheduled"
    return "public" if status.get("privacyStatus") == "public" else "other"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="just this song (its work-folder name) or this YouTube video id")
    parser.add_argument("--status", choices=("all", "scheduled", "public"), default="all")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    parser.add_argument("--old-text", action="append", default=[],
                        help="an extra piece of OLD support text to remove first (repeatable), e.g. the previous "
                             f"sign-off sentence when changing its wording. {DEFAULT_OLD_TEXT!r} is always removed.")
    args = parser.parse_args(argv)

    template = Settings.load().support_description_text.strip()
    old_texts = [DEFAULT_OLD_TEXT, *args.old_text]
    if not template:
        raise SystemExit("Settings.support_description_text is blank -- set it first (Settings popup, or the saved settings file).")
    credentials = load_credentials()
    if credentials is None:
        raise SystemExit("Not connected to YouTube (no valid stored token). Use the app's 'Connect to YouTube' button first.")
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    videos = find_uploaded_videos()
    if args.only:
        videos = {vid: label for vid, label in videos.items() if args.only in (vid, label)}
        if not videos:
            raise SystemExit(f"No uploaded video matches {args.only!r}.")

    live: dict[str, dict] = {}
    ids = list(videos)
    for start in range(0, len(ids), 50):
        response = youtube.videos().list(part="snippet,status", id=",".join(ids[start:start + 50])).execute()
        for item in response.get("items", []):
            live[item["id"]] = item
    missing = [videos[v] for v in ids if v not in live]

    changes: list[tuple[str, str, str, dict, str]] = []          # (video_id, label, kind, snippet, new_description)
    up_to_date = skipped_status = 0
    for video_id in ids:
        item = live.get(video_id)
        if item is None:
            continue
        kind = video_kind(item["status"])
        if args.status != "all" and kind != args.status:
            skipped_status += 1
            continue
        snippet = item["snippet"]
        current = snippet.get("description", "")
        updated = new_description(current, template, old_texts)
        if updated == current.strip():
            up_to_date += 1
        else:
            changes.append((video_id, videos[video_id], kind, snippet, updated))

    print(f"{len(ids)} uploaded videos: {len(changes)} to change, {up_to_date} already in the new layout, "
          f"{skipped_status} skipped by --status {args.status}, {len(missing)} no longer on YouTube.")
    if changes:
        video_id, label, kind, snippet, updated = changes[0]
        print(f"\n--- e.g. {label} ({video_id}, {kind}) would become ---\n{updated}\n---")
    if args.dry_run or not changes:
        return 0

    backup = PROJECT_ROOT / "reports" / f"support_text_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(
        json.dumps({vid: {"label": label, "snippet": snip} for vid, label, _kind, snip, _ in changes},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Backed up the current snippets to {backup}")

    done = failed = 0
    for video_id, label, _kind, snippet, updated in changes:
        snippet = dict(snippet, description=updated)
        try:
            youtube.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()
        except Exception as e:
            if is_quota_exceeded_error(e):
                print(f"\nYouTube's daily quota ran out after {done} update(s). Run this again later to finish the rest.")
                return 0
            failed += 1
            print(f"  FAILED {label} ({video_id}): {type(e).__name__}: {e}")
            continue
        done += 1
        print(f"  updated {label} ({video_id})")
    print(f"\nDone. Updated {done}, failed {failed}, already in the new layout {up_to_date}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
