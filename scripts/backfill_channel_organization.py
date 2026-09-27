"""One-off tool: organizes every already-uploaded video into its All/
Artist/Genre playlists and queues its engagement comment for review, via
the same organize_video() every future upload already gets automatically
(see youtube_playlists.py). See
docs/superpowers/specs/2026-09-17-youtube-channel-organization-design.md.

Idempotent -- organize_video() checks before adding to a playlist and
before queuing a comment, so re-running this after uploading a few more
songs, or after approving some pending comments, never duplicates
anything. A video deleted directly on YouTube is skipped and reported,
never treated as an error.

Visits EASY CHORD versions too (`<song>/easychords`, their own uploads).
EASY/3-/4-CHORD membership is reconciled both ways from the song's real
key: a video whose key does not qualify is taken back OUT of those
playlists. The audited true keys in scripts/key_notes_*.json (--key-notes;
default the 2026-09-26 file) override the key saved with a song's chords,
since uploaded songs keep their old saved key (issue #7 review, F148).

  .venv/bin/python scripts/backfill_channel_organization.py --dry-run     # list what it would visit; no YouTube calls
  .venv/bin/python scripts/backfill_channel_organization.py --only the-real-slim-shady
  .venv/bin/python scripts/backfill_channel_organization.py               # every uploaded video
  .venv/bin/python scripts/backfill_channel_organization.py --fix-playlist-descriptions --dry-run

--fix-playlist-descriptions rewrites the All/EASY CHORD/3-/4-CHORD playlists'
public descriptions to the current wording (the EASY CHORD text still
promised F- and B-key songs); the old text is backed up to reports/ first.

Not wired into the GUI -- a manual, re-runnable apply step, same
convention as update_support_description.py.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.youtube import is_quota_exceeded_error, update_playlist_description, video_exists
from lyricvideo.youtube_playlists import organize_video, stale_playlist_descriptions, wanted_easy_like_playlists
from lyricvideo.youtube_state import uploaded_song_dirs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"
DEFAULT_KEY_NOTES = Path(__file__).with_name("key_notes_2026-09-26.json")


def load_key_overrides(path: Path | None) -> dict[str, str]:
    """label -> the audited true key, for ordinary (non-EASY) videos only: an EASY CHORD version's playlists follow its
    own shape key, never the song's."""
    if path is None or not Path(path).exists():
        return {}
    notes = json.loads(Path(path).read_text(encoding="utf-8")).get("notes", [])
    return {n["label"]: n["true"] for n in notes if n.get("label") and n.get("true") and "/" not in n["label"]}


def backfill(youtube_client, anthropic_client, work_root: Path, key_overrides: dict[str, str] | None = None,
             only: str | None = None, dry_run: bool = False) -> dict:
    """Organizes every uploaded video under work_root (EASY CHORD versions included). Stops at a quota error.
    Returns the counts."""
    from lyricvideo.models import load_song

    key_overrides = key_overrides or {}
    counts = {"organized": 0, "missing": 0, "failed": 0, "removed": 0, "stopped_for_quota": False}
    for slug, work_dir, state in uploaded_song_dirs(work_root):
        if only and only not in (slug, state.video_id):
            continue
        override = key_overrides.get(slug)
        if dry_run:
            try:
                wanted = sorted(wanted_easy_like_playlists(work_dir, load_song(work_dir / "lyrics_timed.json"), override))
            except Exception as e:
                wanted = [f"(unreadable: {type(e).__name__})"]
            print(f"  {slug} ({state.video_id}): would organize; easy-key playlists: {', '.join(wanted) or 'none'}")
            continue
        try:
            if not video_exists(youtube_client, state.video_id):
                print(f"  {slug}: video {state.video_id} no longer exists on YouTube -- skipped")
                counts["missing"] += 1
                continue
            removed = organize_video(youtube_client, anthropic_client, work_dir, key_override=override) or []
            counts["removed"] += len(removed)
            print(f"  {slug}: organized" + (f" (taken out of: {', '.join(removed)})" if removed else ""))
            counts["organized"] += 1
        except Exception as e:
            if is_quota_exceeded_error(e):
                # YouTube's daily API quota resets at midnight Pacific Time --
                # every remaining song would fail identically until then, so
                # stop here instead of burning through the rest reporting the
                # same error over and over. Safe to just re-run this script
                # after the reset: organize_video() picks up exactly where it
                # left off (real incident, 2026-09-17: creating a playlist per
                # artist/genre plus adding each video costs enough quota units
                # that a ~50-song backfill exhausted the daily 10,000-unit cap
                # partway through).
                print(f"\n  {slug}: YouTube API daily quota exceeded -- stopping here.")
                print("  Re-run this script after quota resets (midnight Pacific Time) to finish the rest.")
                counts["stopped_for_quota"] = True
                break
            print(f"  {slug}: FAILED -- {type(e).__name__}: {e}")
            counts["failed"] += 1
    return counts


def fix_playlist_descriptions(youtube_client, dry_run: bool, backup_dir: Path) -> int:
    """Rewrites stale fixed-playlist descriptions (backing the old text up first). Returns how many it changed."""
    stale = stale_playlist_descriptions(youtube_client)
    for key, playlist_id, title, current, wanted in stale:
        print(f"  {title} ({playlist_id}):\n    now:    {current!r}\n    becomes: {wanted!r}")
    if dry_run or not stale:
        print(f"{len(stale)} playlist description(s) to update" + (" (dry run -- nothing written)." if dry_run else "."))
        return 0
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"playlist_description_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    backup.write_text(json.dumps({pid: {"key": key, "title": title, "description": current}
                                  for key, pid, title, current, _ in stale}, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    print(f"Backed up the current descriptions to {backup}")
    done = 0
    for key, playlist_id, title, _current, wanted in stale:
        try:
            update_playlist_description(youtube_client, playlist_id, title, wanted)
        except Exception as e:
            if is_quota_exceeded_error(e):
                print("YouTube's daily quota ran out -- run this again later to finish.")
                break
            print(f"  FAILED {title}: {type(e).__name__}: {e}")
            continue
        done += 1
        print(f"  updated {title}")
    return done


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Organize every uploaded video into its playlists.")
    parser.add_argument("--dry-run", action="store_true", help="list what would be done; change nothing")
    parser.add_argument("--only", help="just this song (its work-folder name, e.g. 'x' or 'x/easychords') or video id")
    parser.add_argument("--key-notes", type=Path, default=DEFAULT_KEY_NOTES,
                        help="audited true keys (scripts/key_notes_*.json) used for EASY/3-/4-CHORD membership")
    parser.add_argument("--no-key-notes", action="store_true", help="use only the keys saved with each song")
    parser.add_argument("--fix-playlist-descriptions", action="store_true",
                        help="rewrite the All/EASY/3-/4-CHORD playlists' public descriptions to the current wording")
    args = parser.parse_args(argv)
    key_overrides = {} if args.no_key_notes else load_key_overrides(args.key_notes)

    if args.dry_run and not args.fix_playlist_descriptions:
        backfill(None, None, WORK_DIR, key_overrides, only=args.only, dry_run=True)
        return

    import anthropic
    from dotenv import load_dotenv
    from googleapiclient.discovery import build

    from lyricvideo.youtube_auth import load_credentials

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(f"ANTHROPIC_API_KEY not found in {PROJECT_ROOT / '.env'}.")

    credentials = load_credentials()
    if credentials is None:
        raise SystemExit(
            "Not connected to YouTube (no valid stored token). "
            "Connect via the app's 'Connect to YouTube' button first."
        )

    youtube_client = build("youtube", "v3", credentials=credentials)
    anthropic_client = anthropic.Anthropic()

    channel_resp = youtube_client.channels().list(part="snippet", mine=True).execute()
    items = channel_resp.get("items", [])
    if not items:
        raise SystemExit("Connected, but no channel found for this account.")
    print(f"Connected channel: {items[0]['snippet']['title']}\n")

    if args.fix_playlist_descriptions:
        fix_playlist_descriptions(youtube_client, args.dry_run, PROJECT_ROOT / "reports")
        return

    counts = backfill(youtube_client, anthropic_client, WORK_DIR, key_overrides, only=args.only)
    print(f"\nDone. Organized: {counts['organized']}, missing/deleted: {counts['missing']}, failed: {counts['failed']}, "
          f"easy-key playlist memberships removed: {counts['removed']}")


if __name__ == "__main__":
    main()
