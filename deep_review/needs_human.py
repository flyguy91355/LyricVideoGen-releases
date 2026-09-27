"""Tracks which songs this program could not get to a real pass, as one small text file per song in its
own folder -- never inside the song's own work/<slug>/ folder, which the rest of the app expects to find at
its usual path, so this never interferes with the main program. Lets the owner review just the songs that
need a human, in one place, instead of hunting through the whole flagged list again. A song a later run
does fix has its marker removed -- the folder always reflects the current, real state."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import SongResult   # type hint only -- runner.py imports this module, not the other way

DEFAULT_DIR = Path("deep_review_needs_human")


def marker_path(root: Path, slug: str) -> Path:
    """One flat file per song: a nested slug ("<song>/easychords") becomes "<song>__easychords.txt" -- a "/" in
    the name once made the write fail on a folder that did not exist and abort the whole run."""
    flat = slug.replace("\\", "/").strip("/").replace("/", "__")
    return Path(root) / f"{flat}.txt"


def write_marker(root: Path, result: "SongResult") -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"song: {result.slug}", f"category: {result.category}", f"action: {result.action}"]
    if result.before_share is not None:
        lines.append(f"before: {result.before_share:.1%}")
    if result.after_share is not None:
        lines.append(f"after: {result.after_share:.1%}")
    if result.attempts:
        lines.append(f"attempts: {result.attempts}")
    if result.cost_usd:
        lines.append(f"cost: ${result.cost_usd:.2f}")
    if result.detail:
        lines.append(f"detail: {result.detail}")
    marker_path(root, result.slug).write_text("\n".join(lines) + "\n", encoding="utf-8")


def clear_marker(root: Path, slug: str) -> None:
    marker_path(Path(root), slug).unlink(missing_ok=True)
