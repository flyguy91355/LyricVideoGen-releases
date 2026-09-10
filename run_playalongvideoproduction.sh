#!/bin/bash
# Launches PlayAlongVideoProduction. Double-click the desktop icon, or run
# this directly from a terminal.
cd "$(dirname "$0")" || exit 1
# Invoke the venv's own python directly rather than "source .venv/bin/activate" --
# the venv was created at this project's pre-rename path (LyricVideoGen), and venvs
# bake absolute paths into bin/activate (VIRTUAL_ENV=/home/doug/LyricVideoGen/.venv)
# at creation time; sourcing it after the rename prepends that now-nonexistent path
# onto PATH, so "python" silently resolves to something outside this venv entirely
# (confirmed live: ModuleNotFoundError on torchaudio, which IS installed here).
# The venv's own python binary locates its site-packages relative to itself, so
# calling it directly sidesteps the stale activate script altogether.
exec .venv/bin/python -m lyricvideo.gui
