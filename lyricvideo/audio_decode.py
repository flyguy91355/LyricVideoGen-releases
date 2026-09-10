"""Audio decoding via FFmpeg, ported from LyricChord's utils/audio.py + utils/ffmpeg.py.

FFmpeg (not librosa.load/soundfile) decodes so MP3/M4A/OGG all work without relying on
libsndfile's codec support -- the same reasoning LyricChord's own docstring gives.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class FFmpegNotFound(RuntimeError):
    pass


@lru_cache(maxsize=1)
def find_ffmpeg() -> str:
    """Resolution order: PLAYALONGVIDEOPRODUCTION_FFMPEG env var -> `ffmpeg` on PATH ->
    the binary bundled with the already-required `imageio-ffmpeg` package."""
    env = os.environ.get("PLAYALONGVIDEOPRODUCTION_FFMPEG")
    if env and os.path.isfile(env):
        return env

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # ImportError or download failure
        pass

    raise FFmpegNotFound(
        "FFmpeg was not found. Install it and add it to PATH, set "
        "PLAYALONGVIDEOPRODUCTION_FFMPEG to the executable, or `pip install imageio-ffmpeg`."
    )


def decode_audio(
    path: Path, sr: int = 22050, start: float | None = None, duration: float | None = None
) -> np.ndarray:
    """Decode an audio file (or a window of it) to mono float32 at sample rate `sr`.

    `start`/`duration` are seconds; FFmpeg seeks and stops itself, so a short window of
    a long file costs a fraction of a full decode.
    """
    cmd = [find_ffmpeg(), "-v", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{max(0.0, duration):.3f}"]
    cmd += ["-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(sr), "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="ignore")[:400]
        raise RuntimeError(f"FFmpeg failed to decode {path.name}: {err}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def probe_duration(path: Path) -> float:
    """Duration in seconds from FFmpeg's input banner (a header read, not a decode)."""
    cmd = [find_ffmpeg(), "-hide_banner", "-i", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = _DURATION_RE.search(proc.stderr or "")
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)
