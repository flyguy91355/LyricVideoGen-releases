"""Re-checks the key of every song that is made but NOT on YouTube, and puts right the ones that are wrong (owner,
2026-09-26). Songs already on YouTube are never touched.

  .venv/bin/python scripts/settle_keys.py                 # dry run: shows what would change, writes nothing
  .venv/bin/python scripts/settle_keys.py --apply         # records each song's key decision and corrects wrong keys
  .venv/bin/python scripts/settle_keys.py --only blackbird

For each song the key from its saved chords and a second opinion (one small Claude call, a fraction of a cent) must agree.
A song whose key is already confirmed (agreed, an earlier --apply, or your Set Key) is not asked about again, so a re-run
costs nothing for it and can never demote it.
  already-settled  its key was confirmed before and the video shows it: nothing changes, no Claude call.
  already-right    the video's key was correct: its key decision is recorded.
  corrected        the key was wrong: the saved key and chord spelling are fixed, the old video is set aside as
                   *.previous.mp4, its EASY CHORD folder (made for the old key) is moved to easychords_prior_<time>/, and
                   the song shows in Flagged for Lyrics Review -- press Render Anyway to make the video again (no new image
                   cost), then Generate EASY CHORD Versions for the EASY one. An EASY CHORD video already on YouTube is left alone.
  review           the two disagree: the song waits for you -- press Set Key in Flagged for Lyrics Review.
  no-chords / skipped-uploaded  nothing to do.
Chords are spelled per Settings' "Use flats in flat keys" box. A report of the run is saved to reports/key_rollout_<time>.json."""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--only", help="just this song's work-folder name")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    import anthropic
    from lyricvideo.key_rollout import settle_saved_song
    from lyricvideo.settings import Settings

    client = anthropic.Anthropic()
    prefer_flats = Settings.load().prefer_flats
    folders = sorted(p.parent for p in (PROJECT_ROOT / "work").glob("*/lyrics_timed.json") if p.parent.name != "easychords")
    if args.only:
        folders = [f for f in folders if f.name == args.only]
    results = []
    for folder in folders:
        try:
            r = settle_saved_song(folder, client, apply=args.apply, prefer_flats=prefer_flats)
        except Exception as e:  # one unreadable song must not stop the rest
            print(f"  ERROR {folder.name}: {type(e).__name__}: {e}")
            continue
        results.append(r)
        if r.action in ("corrected", "review"):
            print(f"  {r.action:9} {r.slug:40} {r.old_key:11} -> {r.new_key or '?'}")
            if r.action == "corrected" and r.detail:
                print(f"            {r.detail}")
    counts = Counter(r.action for r in results)
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN (nothing written)'}: " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items())))
    report = PROJECT_ROOT / "reports" / f"key_rollout_{datetime.now():%Y%m%d_%H%M%S}.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
