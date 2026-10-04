"""Re-checks the key of every song that is made but NOT on YouTube, and puts right the ones that are wrong (owner,
2026-09-26). Songs already on YouTube are never touched.

  .venv/bin/python scripts/settle_keys.py                 # dry run: shows what would change, writes nothing
  .venv/bin/python scripts/settle_keys.py --apply         # records each song's key decision and corrects wrong keys
  .venv/bin/python scripts/settle_keys.py --only blackbird
  .venv/bin/python scripts/settle_keys.py --apply --no-render   # correct and hold, like before 2026-10-01

For each song the key from its saved chords and a second opinion (one small Claude call, a fraction of a cent) must agree;
when they do not, a web search (key_research.py: Haiku 4.5, up to 2 searches) settles it if two cited sources agree.
A song whose key is already confirmed (agreed, an earlier --apply, or your Set Key) is not asked about again, so a re-run
costs nothing for it and can never demote it.
  already-settled  its key was confirmed before and the video shows it: nothing changes, no Claude call.
  already-right    the video's key was correct: its key decision is recorded.
  corrected        the key was wrong: the saved key and chord spelling are fixed, the old video is set aside as
                   *.previous.mp4, its EASY CHORD folder (made for the old key) is moved to easychords_prior_<time>/, then
                   (owner, 2026-10-01) the video is made again right here, from the images stage on (no new image cost,
                   no owner step) -- a failed re-render falls back to the old behavior: held in Flagged for Lyrics
                   Review for a manual Render Anyway, with the reason printed. Pass --no-render to always leave it held
                   instead. Either way, its EASY CHORD version (if any) still needs Generate EASY CHORD Versions. An EASY
                   CHORD video already on YouTube is left alone.
                   A song held before its video for its key (no video yet) that comes out confirmed is made right here too.
  review           research could not settle it either: the song waits for you -- press Set Key in Flagged for Lyrics Review.
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


def _rerender(folder: Path, settings) -> None:
    """Makes `folder`'s video again right after settle_saved_song() has corrected its key and set the old one aside --
    resuming at the images stage (chords and the key decision are already saved; images already bought are carried
    over to any respelled chord names at no cost) through render. Raises on any failure; the caller falls back to the
    held-for-Render-Anyway behavior and reports why."""
    from lyricvideo.pipeline import load_redo_inputs, run_pipeline

    audio_path, title = load_redo_inputs(folder)
    run_pipeline(audio_path, folder, title, start_stage="images", settings=settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--only", help="just this song's work-folder name")
    parser.add_argument("--no-render", dest="render", action="store_false",
                        help="leave a corrected song held for a manual Render Anyway instead of making its video again now")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    import anthropic
    from lyricvideo.key_rollout import key_hold_released, settle_saved_song
    from lyricvideo.settings import Settings

    client = anthropic.Anthropic()
    settings = Settings.load()
    folders = sorted(p.parent for p in (PROJECT_ROOT / "work").glob("*/lyrics_timed.json") if p.parent.name != "easychords")
    if args.only:
        folders = [f for f in folders if f.name == args.only]
    results = []
    rendered = 0
    for folder in folders:
        try:
            r = settle_saved_song(folder, client, apply=args.apply, prefer_flats=settings.prefer_flats)
        except Exception as e:  # one unreadable song must not stop the rest
            print(f"  ERROR {folder.name}: {type(e).__name__}: {e}")
            continue
        results.append(r)
        if r.action in ("corrected", "review"):
            print(f"  {r.action:9} {r.slug:40} {r.old_key:11} -> {r.new_key or '?'}")
            if r.action == "corrected" and r.detail:
                print(f"            {r.detail}")
        if args.apply and args.render and r.action in ("corrected", "already-right", "already-settled") and key_hold_released(folder):
            try:
                _rerender(folder, settings)
            except Exception as e:
                print(f"            WARNING: could not be made again automatically ({type(e).__name__}: {e}); "
                      "it stays held -- use Render Anyway in Flagged for Lyrics Review.")
            else:
                print("            video made again at its corrected key")
                rendered += 1
    counts = Counter(r.action for r in results)
    if rendered and not counts.get("corrected"):
        print(f"{rendered} song(s) held for their key were released and made")
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN (nothing written)'}: " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items())))
    if args.apply and args.render and counts.get("corrected"):
        print(f"{rendered}/{counts['corrected']} corrected song(s) made again automatically")
    report = PROJECT_ROOT / "reports" / f"key_rollout_{datetime.now():%Y%m%d_%H%M%S}.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
