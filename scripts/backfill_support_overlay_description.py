"""RETIRED (issue #7 review, F088) -- use scripts/update_support_description.py instead.

This older tool appended Settings.support_description_text to the end of every uploaded video's description. Since
2026-09-26 that setting is a TEMPLATE (text above, the marker {description}, text below -- see
youtube_schedule.render_description), so appending it raw wrote the tip line, a literal "{description}" line and a
second sign-off into every live description; and it had no dry run, no backup and no quota stop.
scripts/update_support_description.py does the job correctly: it re-renders each description with the template, keeps a
"📌 Song key" note first, saves a backup of every snippet it changes to reports/, supports --dry-run and --only, and
stops cleanly when the daily quota runs out.

  .venv/bin/python scripts/update_support_description.py --dry-run     # preview first
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPLACEMENT = "scripts/update_support_description.py"


def main(argv: list[str] | None = None) -> None:
    """Never touches YouTube: refuses and points to the replacement script."""
    raise SystemExit(
        "backfill_support_overlay_description.py is retired -- it would append the support TEMPLATE (with its literal "
        "{description} marker) to every live description. Use "
        f"{REPLACEMENT} instead (run it with --dry-run first)."
    )


if __name__ == "__main__":
    main()
