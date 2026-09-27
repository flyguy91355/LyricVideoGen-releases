"""Issue #7 ("long time to start and to x out"): the GUI imports lyricvideo.pipeline (for the song lists) and, through it,
lyricvideo.align -- which loaded torch, torchaudio and the Anthropic SDK before the window could appear, and made every exit
tear them down again. Those now load only when a pipeline stage actually needs them."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
HEAVY = ("torch", "torchaudio", "anthropic")


@pytest.mark.parametrize("module", ["lyricvideo.pipeline", "lyricvideo.align", "lyricvideo.timing_gate"])
def test_importing_the_module_loads_no_torch_torchaudio_or_anthropic(module):
    # A fresh interpreter: this test process has long since imported torch for other tests.
    code = (
        f"import sys, {module}; "
        f"loaded = [m for m in {HEAVY!r} if m in sys.modules]; "
        "print(loaded); sys.exit(1 if loaded else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, f"{module} imported {result.stdout.strip()} {result.stderr[-2000:]}"


def test_the_lazy_anthropic_name_still_reaches_the_real_sdk():
    import anthropic

    from lyricvideo import pipeline

    assert pipeline.anthropic.Anthropic is anthropic.Anthropic


def test_the_align_stage_reads_the_stems_length_from_its_header(tmp_path):
    """The align stage used to decode the whole vocal stem (about 85 MB as float32 for four minutes) only for its length,
    and keep it alive through chords, images and the render."""
    import soundfile

    from lyricvideo.pipeline import _vocal_stem_seconds

    path = tmp_path / "vocals.wav"
    soundfile.write(str(path), np.zeros((int(2.5 * 8000), 2)), 8000)

    assert _vocal_stem_seconds(path) == pytest.approx(2.5)
