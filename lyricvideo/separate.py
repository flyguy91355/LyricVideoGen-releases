from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class SeparationError(Exception):
    pass


def separate_vocals(audio_path: Path, out_dir: Path) -> Path:
    """Run Demucs two-stem separation (CPU) and return the path to vocals.wav."""
    subprocess.run(
        [
            sys.executable, "-m", "demucs",
            "--two-stems=vocals",
            "-n", "htdemucs",
            "-d", "cpu",  # this machine's GPU (GT 1030) can't run CUDA kernels
            "-o", str(out_dir),
            str(audio_path),
        ],
        check=True,
    )
    stem_name = Path(audio_path).stem
    vocals_path = out_dir / "htdemucs" / stem_name / "vocals.wav"
    if not vocals_path.exists():
        raise SeparationError(f"Demucs did not produce expected output at {vocals_path}")
    return vocals_path
