"""Issue #7, "long time to start": opening the window must not wait for the heavy libraries only a running pipeline or an
upload needs. Checked in a FRESH interpreter -- this pytest process has long since imported most of them."""

import subprocess
import sys
from pathlib import Path

HEAVY = ("torch", "torchaudio", "moviepy", "anthropic", "googleapiclient", "tensorflow", "crema")


def test_importing_the_gui_loads_none_of_the_heavy_libraries():
    code = (
        "import sys, lyricvideo.gui\n"
        f"print([m for m in {HEAVY!r} if m in sys.modules])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "[]", f"loaded at GUI import: {result.stdout.strip()}"


def test_the_gui_still_reaches_anthropic_and_the_youtube_client_when_it_needs_them(monkeypatch):
    """The lazy stand-ins behave like the real names (and the tests' patch points still work)."""
    import lyricvideo.gui as gui

    monkeypatch.setattr("lyricvideo.gui.anthropic.Anthropic", lambda: "fake-claude")
    assert gui.anthropic.Anthropic() == "fake-claude"
    calls = []
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: calls.append((a, k)) or "client")
    assert gui.build("youtube", "v3", credentials="c") == "client" and calls == [(("youtube", "v3"), {"credentials": "c"})]
