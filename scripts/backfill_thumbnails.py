"""Gives every video ALREADY on YouTube a custom thumbnail (owner, 2026-10-04), and sets it on the channel.

  .venv/bin/python scripts/backfill_thumbnails.py --dry-run          # lists what would be made and set, spends nothing
  .venv/bin/python scripts/backfill_thumbnails.py --limit 5          # the first 5 only
  .venv/bin/python scripts/backfill_thumbnails.py --only blackbird   # one song (or <song>/easychords)
  .venv/bin/python scripts/backfill_thumbnails.py --redraw-long-artists --dry-run   # see below

--redraw-long-artists (owner, 2026-11-15): every video already on YouTube whose thumbnail shrank a long artist onto one line
("Bob Seger & The Silver Bullet Band" at 44 px) is drawn again with the artist on two lines at full size, from the picture it
already has (no new picture, no Claude call, no cost), and set again (50 quota units each). Done ones are recorded in
thumbnail_set.json, so a re-run after the quota runs out continues where it stopped. Banned folders are never touched.

Per video: a thumbnail made once (lyricvideo/thumbnail.py: about 1.3 cents -- a Claude prompt and two ~0.3 cent pictures;
an EASY CHORD version reuses its song's picture and costs nothing) and set with thumbnails.set (50 YouTube quota units).
A video whose thumbnail is already on YouTube (thumbnail_set.json) is skipped, so a re-run continues where it stopped; the
run stops cleanly when the daily quota is used up. Needs ANTHROPIC_API_KEY, REPLICATE_API_TOKEN and a connected channel."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DROPPED = {"ain-t-talkin-bout-love", "bohemian-rhapsody", "ironic"}      # blocked/not on YouTube; the owner dropped them


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="list what would happen; spend and change nothing")
    parser.add_argument("--only", help="one song's work-folder name, or <song>/easychords")
    parser.add_argument("--limit", type=int, help="stop after this many videos")
    parser.add_argument("--redraw-long-artists", action="store_true",
                        help="draw again (no cost) and re-set thumbnails whose long artist was shrunk onto one line")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    from lyricvideo import thumbnail as th
    from lyricvideo.banned import is_banned
    from lyricvideo.settings import Settings
    from lyricvideo.thumbnail_job import mark_artist_redrawn, needs_artist_redraw, redraw_thumbnail
    from lyricvideo.youtube_state import uploaded_song_dirs
    settings = Settings.load()
    font_path = getattr(settings, "font_path", None) or None

    work_root = PROJECT_ROOT / "work"
    todo = []
    for name, folder, state in uploaded_song_dirs(work_root):
        if name.replace("\\", "/").split("/")[0] in DROPPED:       # the owner dropped these: never touch them
            continue
        if args.only and name.replace("\\", "/") != args.only.replace("\\", "/"):
            continue
        if args.redraw_long_artists:
            if not is_banned(folder) and needs_artist_redraw(folder, font_path):
                todo.append((name, folder, state))
            continue
        marker = folder / th.THUMBNAIL_SET_FILE
        try:
            done = json.loads(marker.read_text(encoding="utf-8")).get("video_id") == state.video_id
        except (OSError, ValueError, AttributeError):
            done = False
        if not done:
            todo.append((name, folder, state))
    if args.limit:
        todo = todo[: args.limit]
    what = "a thumbnail drawn again with the artist on two lines" if args.redraw_long_artists else "a thumbnail"
    print(f"{len(todo)} video(s) need {what}")
    if args.dry_run:
        if args.redraw_long_artists:
            for name, folder, state in todo:
                print(f"  {name:45} {state.video_id}")
            print(f"DRY RUN: nothing drawn, nothing set. A real run sets {len(todo)} thumbnail(s): {len(todo) * 50} quota units, no other cost.")
            return 0
        for name, folder, state in todo:
            have = "has a thumbnail file" if (folder / th.THUMBNAIL_FILE).exists() else "needs one made"
            easy = " (EASY: reuses its song's picture)" if folder.name == "easychords" else ""
            print(f"  {name:45} {state.video_id}  {have}{easy}")
        print("DRY RUN: nothing made, nothing set.")
        return 0

    import anthropic
    from lyricvideo.models import load_song
    from lyricvideo.thumbnail_job import ensure_thumbnail
    from lyricvideo.youtube import is_quota_exceeded_error, set_thumbnail
    from lyricvideo.youtube_auth import load_credentials
    from googleapiclient.discovery import build

    credentials = load_credentials()
    if credentials is None:
        print("YouTube is not connected -- connect it in the app first.")
        return 1
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    client = None if args.redraw_long_artists else anthropic.Anthropic()      # a redraw never calls Claude
    token = os.environ.get("REPLICATE_API_TOKEN", "")
    made = set_ok = failed = gone = 0
    for name, folder, state in todo:
        try:
            if args.redraw_long_artists:
                path = redraw_thumbnail(folder, font_path=font_path, show_chords=settings.thumbnail_show_chords)
            else:
                path = ensure_thumbnail(folder, client, token, font_path=font_path,
                                        show_chords=settings.thumbnail_show_chords,
                                        use_song_images=settings.thumbnail_use_song_images)
            if path is None:
                print(f"  SKIP {name}: no thumbnail could be made")
                failed += 1
                continue
            made += 1
            set_thumbnail(youtube, state.video_id, path)
            if args.redraw_long_artists:
                mark_artist_redrawn(folder, state.video_id)
            else:
                (folder / th.THUMBNAIL_SET_FILE).write_text(json.dumps({"video_id": state.video_id}), encoding="utf-8")
            set_ok += 1
            print(f"  ok   {name}")
        except Exception as e:  # quota stops the run; anything else skips just this video
            if getattr(getattr(e, "resp", None), "status", None) == 404:     # the video is no longer on the channel: note it, skip it from now on
                (folder / th.THUMBNAIL_SET_FILE).write_text(json.dumps({"video_id": state.video_id, "video_missing": True}), encoding="utf-8")
                print(f"  GONE {name}: {state.video_id} is no longer on YouTube")
                gone += 1
                continue
            if "uploadRateLimitExceeded" in str(e):        # YouTube's separate daily limit on thumbnail uploads (not the API quota)
                print(f"\nYouTube's daily CUSTOM THUMBNAIL limit is reached ({set_ok} set so far); it lifts after about 24 hours. Run again later.")
                break
            if is_quota_exceeded_error(e):
                print(f"\nYouTube's daily quota is used up. {set_ok} set so far; run again after it resets.")
                break
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{set_ok} thumbnail(s) set on YouTube, {failed} failed, {gone} video(s) no longer on YouTube.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
