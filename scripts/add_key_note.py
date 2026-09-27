"""Puts the one-line key note at the top of the YouTube description of videos whose burned-in Key badge is wrong (owner,
2026-09-26):   📌 Song key: D major (not F# minor as shown in the video)
Replaces the earlier long "📌 Correction: ..." notes with the same short line, and (for the two EASY CHORD videos whose
description itself states the wrong key) rewrites that wording. Everything else in the description, and the title, tags,
category and the private/scheduled/public status, stays exactly as it is.

  .venv/bin/python scripts/add_key_note.py --dry-run                                   # show every change, write nothing
  .venv/bin/python scripts/add_key_note.py --only the-real-slim-shady                  # one video (the test)
  .venv/bin/python scripts/add_key_note.py                                             # every video in the notes file

The list of videos, their true key and what their badge shows is scripts/key_notes_2026-09-26.json. Safe to re-run (a video
already showing the same note is skipped). Each description is read twice and must match before anything is written; the
current snippet of every video about to change is saved to reports/key_note_backup_<time>.json first; every write is read
back to confirm. Stops cleanly when YouTube's daily quota runs out (each update costs 50 units) -- run it again later."""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.key_note import apply_key_fixes
from lyricvideo.youtube import is_quota_exceeded_error
from lyricvideo.youtube_auth import load_credentials
from lyricvideo.youtube_state import load_youtube_state

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"
DEFAULT_NOTES = Path(__file__).with_name("key_notes_2026-09-26.json")


def video_id_for(label: str) -> str | None:
    state = load_youtube_state(WORK_DIR / label)
    return state.video_id if state is not None and state.video_id else None


def read_snippet(youtube, video_id: str) -> dict | None:
    items = youtube.videos().list(part="snippet,status", id=video_id).execute().get("items", [])
    return items[0] if items else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    parser.add_argument("--only", help="just this label (e.g. 'the-real-slim-shady' or 'baba-o-riley/easychords')")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    notes = json.loads(args.notes.read_text(encoding="utf-8"))["notes"]
    if args.only:
        notes = [n for n in notes if n["label"] == args.only]
        if not notes:
            raise SystemExit(f"No entry for {args.only!r} in {args.notes.name}.")
    credentials = load_credentials()
    if credentials is None:
        raise SystemExit("Not connected to YouTube. Use the app's 'Connect to YouTube' button first.")
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    plan = []          # (note, video_id, item, new_description)
    unchanged = missing = 0
    try:
        for note in notes:
            video_id = video_id_for(note["label"])
            item = read_snippet(youtube, video_id) if video_id else None
            if item is None:
                missing += 1
                print(f"  not on YouTube: {note['label']}")
                continue
            again = read_snippet(youtube, video_id)              # read twice: YouTube can serve a stale copy just after an edit
            if again is None or again["snippet"]["description"] != item["snippet"]["description"]:
                print(f"  SKIPPED {note['label']}: two reads of its description disagree -- run again in a minute")
                continue
            fixes = [tuple(pair) for pair in note.get("fixes", [])]
            new = apply_key_fixes(item["snippet"]["description"], note["true"], note["shown"], fixes)
            if new == item["snippet"]["description"].strip():
                unchanged += 1
            else:
                plan.append((note, video_id, item, new))
    except Exception as e:
        if is_quota_exceeded_error(e):
            print("\nYouTube's daily quota is used up -- nothing was written. Run this again after it resets (midnight Pacific).")
            return 0
        raise

    print(f"{len(notes)} videos: {len(plan)} to change, {unchanged} already right, {missing} not on YouTube.")
    for note, video_id, item, new in plan:
        print(f"\n--- {note['label']} ({video_id}, {item['status']['privacyStatus']}) ---\n{new[:420]}{'...' if len(new) > 420 else ''}")
    if args.dry_run or not plan:
        return 0

    backup = PROJECT_ROOT / "reports" / f"key_note_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    backup.parent.mkdir(exist_ok=True)
    backup.write_text(json.dumps({vid: {"label": n["label"], "snippet": it["snippet"]} for n, vid, it, _ in plan},
                                 indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nBacked up the current snippets to {backup}")
    done = failed = 0
    for note, video_id, item, new in plan:
        try:
            youtube.videos().update(part="snippet", body={"id": video_id, "snippet": dict(item["snippet"], description=new)}).execute()
            for _try in range(6):                                # confirm by reading it back (a fresh edit can take a moment)
                time.sleep(4)
                back = read_snippet(youtube, video_id)
                if back is not None and back["snippet"]["description"].strip() == new:
                    break
            else:
                print(f"  WARNING {note['label']}: written, but the read-back did not match yet -- check it in Studio")
        except Exception as e:
            if is_quota_exceeded_error(e):
                print(f"\nYouTube's quota ran out after {done} update(s). Run this again later to finish the rest.")
                return 0
            failed += 1
            print(f"  FAILED {note['label']}: {type(e).__name__}: {e}")
            continue
        done += 1
        print(f"  updated {note['label']} ({video_id})")
    print(f"\nDone. Updated {done}, failed {failed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
