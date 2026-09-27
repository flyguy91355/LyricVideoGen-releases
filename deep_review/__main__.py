"""CLI: python -m deep_review [--work work] [--pilot N] [--dry-run] [--songs slug1,slug2,...]

Runs the deep review over songs currently set aside for review (lyricvideo's own Flagged for Lyrics Review
list, by default), reports what it found and did, and states plainly whether the owner's 90%-pass goal was
met. See runner.py for what "pass" actually means here (a real post-redo timing-gate recheck, never a
guess) and diagnosis.py for why a song might be left alone entirely.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from lyricvideo.settings import Settings
from lyricvideo.timing_gate import use_pass_share_from

from .runner import SongResult, run_deep_review

PASS_TARGET = 0.90
_NEEDS_ATTENTION_ACTIONS = ("left for manual review", "failed")
_NEEDS_ATTENTION_CATEGORIES = ("damaged_audio", "alignment_only")


def summarize(results: list[SongResult]) -> str:
    """The full text report -- a pure function of the results, so it's testable without ever touching a
    real song, an API key, or stdout."""
    if not results:
        return "No songs to review."

    by_category: dict[str, list[SongResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)

    lines = [f"{'=' * 70}", f"Deep review: {len(results)} song(s)", f"{'=' * 70}"]
    for category in sorted(by_category):
        rows = by_category[category]
        lines.append(f"\n{category} ({len(rows)}):")
        for r in rows:
            mark = "PASS" if r.passed else ("?   " if r.passed is None else "fail")
            share = f" {r.after_share:.0%}" if r.after_share is not None else ""
            attempts = f" ({r.attempts} attempt{'s' if r.attempts != 1 else ''})" if r.attempts else ""
            cost = f" [${r.cost_usd:.2f}]" if r.cost_usd else ""
            tail = f": {r.detail[:100]}" if r.detail else ""
            lines.append(f"  [{mark}]{share} {r.slug} -- {r.action}{attempts}{cost}{tail}")

    decided = [r for r in results if r.passed is not None]
    passed = [r for r in decided if r.passed]
    rate = len(passed) / len(decided) if decided else 0.0
    lines.append(f"\n{'-' * 70}")
    undecided_note = (
        f"; {len(results) - len(decided)} not yet decided (dry run, waiting for a key, or not reviewed)"
        if len(decided) < len(results) else ""
    )
    lines.append(f"{len(passed)} of {len(decided)} decided song(s) now pass ({rate:.1%}){undecided_note}")
    if decided:
        verdict = "MEETS" if rate >= PASS_TARGET else "DOES NOT YET MEET"
        lines.append(f"Goal is {PASS_TARGET:.0%} pass: {verdict} the goal.")

    total_cost = sum(r.cost_usd for r in results)
    if total_cost:
        # Every research call's own measured cost (tokens + web searches); the redo's own spend (new images, the
        # image prompts, the key's second opinion) is billed by run_pipeline and not counted here.
        lines.append(f"Research API cost this run: ${total_cost:.2f} (web research only; a redo's images are extra)")

    waiting = [r for r in results if r.category == "waiting_for_key" or r.action == "lyrics fixed; waiting for Set Key"]
    if waiting:
        lines.append(f"\n{len(waiting)} song(s) are waiting for their key -- use Set Key in the app:")
        lines.extend(f"  {r.slug}" for r in waiting)

    needs_attention = [
        r for r in results
        if r.action in _NEEDS_ATTENTION_ACTIONS
        or (r.action == "skipped" and r.category in _NEEDS_ATTENTION_CATEGORIES)
    ]
    if needs_attention:
        lines.append(f"\n{len(needs_attention)} song(s) still need a human or a different approach:")
        for r in needs_attention:
            tail = f": {r.detail[:120]}" if r.detail else ""
            lines.append(f"  {r.slug} ({r.category}){tail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="python -m deep_review",
        description="Deep-dive review of every song set aside for lyrics/timing review: diagnose why, "
                    "research and fix genuine lyrics-text problems using the real internet plus the audio, "
                    "and verify each fix with a real redo -- never just a guess.",
    )
    parser.add_argument("--work", default="work", type=Path, help="the work folder (default: work)")
    parser.add_argument("--songs", default=None, help="comma-separated slugs to review instead of every flagged song")
    parser.add_argument("--pilot", type=int, default=None, metavar="N",
                         help="only review the first N songs -- for judging cost/quality before running the whole backlog")
    parser.add_argument("--dry-run", action="store_true",
                         help="diagnose and research (research calls still cost money) but never write a real file or trigger a redo")
    args = parser.parse_args(argv)

    songs = [s.strip() for s in args.songs.split(",") if s.strip()] if args.songs else None
    client = anthropic.Anthropic()
    settings = Settings.load()
    # The owner's timing bar, exactly as the GUI registers it: every gate check in this process (the listing's holds,
    # the diagnosis, the post-redo recheck) then judges at the same bar the redo's own render does, never the default.
    use_pass_share_from(lambda: settings.timing_pass_percent / 100)
    try:
        results = run_deep_review(args.work, client, songs=songs, limit=args.pilot, settings=settings, dry_run=args.dry_run)
    finally:
        use_pass_share_from(None)
    print(summarize(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
