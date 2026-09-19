from pathlib import Path

import pytest

# Mirrors lyricvideo.pipeline.default_font()'s own candidate list: Linux DejaVu/
# Liberation first, then the bold fonts every Windows install ships with. Without
# the Windows entries, every render/assemble/chord-diagram test silently skipped
# on Windows (73 of them, found 2026-09-14) -- a whole layer of the suite that
# looked green because it never ran.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
]


@pytest.fixture
def test_font_path():
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    pytest.skip("no truetype font available in this environment")


@pytest.fixture(autouse=True)
def _isolate_redo_log(tmp_path_factory, monkeypatch):
    """Tests that redo songs must never write into the owner's real ~/.playalongvideoproduction/redone_songs.json."""
    monkeypatch.setattr("lyricvideo.redo_log.LOG_FILE", tmp_path_factory.mktemp("redolog") / "redone_songs.json")
