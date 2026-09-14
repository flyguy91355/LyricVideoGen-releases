"""Where this project's own virtualenv interpreter lives, on any OS.

A venv puts its python at ``.venv/bin/python`` on Linux/macOS but at
``.venv/Scripts/python.exe`` on Windows. Every place that needs to re-invoke
the venv (self-update's ``pip install``, the Relaunch button, the launcher
scripts) goes through ``venv_python()`` instead of spelling one of those
paths out, so the same code runs on both.
"""
from __future__ import annotations

import sys
from pathlib import Path


def venv_python(project_root: Path, platform: str | None = None) -> Path:
    """The interpreter inside ``project_root/.venv`` for this platform.

    Falls back to the running interpreter (``sys.executable``) if no venv
    interpreter exists there -- e.g. a developer running straight from a
    system Python -- so callers always get something runnable.
    """
    platform = platform or sys.platform
    if platform.startswith("win"):
        candidate = project_root / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = project_root / ".venv" / "bin" / "python"
    if candidate.exists():
        return candidate
    return Path(sys.executable)
