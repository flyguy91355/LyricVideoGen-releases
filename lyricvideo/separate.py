from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class SeparationError(Exception):
    pass


# Set LYRICVIDEO_DEVICE=cpu (or cuda) to force a device; otherwise Demucs runs on
# CUDA whenever torch can see a working GPU and silently falls back to CPU
# on machines without one, so the same install works for everybody.
_DEVICE_ENV = "LYRICVIDEO_DEVICE"


def compute_device() -> str:
    """'cuda' when a CUDA-capable GPU is usable, else 'cpu'. Honors
    LYRICVIDEO_DEVICE as an explicit override (e.g. to force CPU for a GPU
    that is too small or too old to run Demucs's kernels)."""
    forced = os.environ.get(_DEVICE_ENV, "").strip().lower()
    if forced in ("cpu", "cuda"):
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # torch missing/broken -> Demucs would fail anyway; be honest and say cpu
        pass
    return "cpu"


def _run_demucs(cmd: list[str]) -> None:
    """Runs the Demucs command, relaying everything it prints (its tqdm
    progress bars included, `\r` updates and all) through THIS process's
    own `sys.stdout` as it arrives. A plain subprocess.run() inherits the
    real OS-level stdout, not the Python-level `sys.stdout` object -- so
    when the GUI swaps `sys.stdout` for its log-widget writer, Demucs's
    output (the slowest stage of the whole pipeline) never reached the log
    at all; it went to the console, which a desktop-launched app doesn't
    even show (found by code review, 2026-09-14). Reads whatever bytes are
    available rather than whole lines so a `\r`-driven progress bar streams
    through live instead of arriving in one lump when the stage ends.
    Raises subprocess.CalledProcessError on a non-zero exit, exactly like
    check=True did."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            out = sys.stdout
            if out is not None:
                out.write(chunk.decode("utf-8", errors="replace"))
                out.flush()
    finally:
        proc.stdout.close()
        returncode = proc.wait()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd)


def separate_vocals(audio_path: Path, out_dir: Path, device: str | None = None) -> Path:
    """Run Demucs two-stem separation and return the path to vocals.wav.
    Uses CUDA when available (see compute_device), CPU otherwise."""
    _run_demucs([
        sys.executable, "-m", "demucs",
        "--two-stems=vocals",
        "-n", "htdemucs",
        "-d", device or compute_device(),
        "-o", str(out_dir),
        str(audio_path),
    ])
    stem_name = Path(audio_path).stem
    vocals_path = out_dir / "htdemucs" / stem_name / "vocals.wav"
    if not vocals_path.exists():
        raise SeparationError(f"Demucs did not produce expected output at {vocals_path}")
    return vocals_path
