#!/bin/bash
# Launches LyricVideoGen. Double-click the desktop icon, or run this
# directly from a terminal.
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate
exec python -m lyricvideo.gui
