from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class SeparationError(Exception):
    pass


# Set LYRICVIDEO_DEVICE=cpu (or cuda) to force a device; otherwise Demucs runs on
# CUDA whenever torch can see a GPU that its own build actually has kernels for,
# and silently falls back to CPU otherwise, so the same install works for everybody.
_DEVICE_ENV = "LYRICVIDEO_DEVICE"


def _forced_device() -> str | None:
    """The LYRICVIDEO_DEVICE override ('cpu' or 'cuda'), or None when it is
    unset or holds anything else."""
    forced = os.environ.get(_DEVICE_ENV, "").strip().lower()
    return forced if forced in ("cpu", "cuda") else None


def cuda_build_supports_device(arch_list: list[str], capability: tuple[int, int]) -> bool:
    """Whether a torch build compiled for `arch_list` (torch.cuda.get_arch_list(),
    e.g. ['sm_75', 'sm_80', 'sm_86', 'sm_90', 'sm_100', 'sm_120']) carries a
    kernel that can run on a GPU of compute capability `capability`
    (torch.cuda.get_device_capability()).

    torch.cuda.is_available() only says a CUDA device and driver exist; it says
    nothing about whether the installed wheel was built for that device's
    architecture. Issue #5: the original GT 1030 box (Pascal, CC 6.1) running the
    cu130 wheel (built for sm_75 and up -- CUDA 13 dropped Maxwell/Pascal/Volta
    entirely) passed is_available(), and Demucs then died on its very first
    kernel launch: "CUDA error: no kernel image is available for execution on
    the device".

    Rule: NVIDIA's cubin compatibility, the same one torch's own startup
    warning (`torch.cuda._check_capability`) applies -- code built for sm_XY
    runs on hardware of the same major X whose minor is >= Y; a "compute_XY"
    PTX entry is treated exactly like sm_XY, as torch treats it. A list with
    no such entries at all (ROCm's "gfx..." names, or a format this code
    doesn't know) can't be judged here and is trusted as supported rather
    than turning a working GPU off."""
    major, minor = capability
    built: list[tuple[int, int]] = []
    for arch in arch_list:
        kind, _, cc = arch.partition("_")
        if kind not in ("sm", "compute"):
            continue
        cc = cc.rstrip("af")  # "sm_90a" / "sm_100f" arch-specific variants
        if not cc.isdigit() or len(cc) < 2:
            continue
        built.append((int(cc[:-1]), int(cc[-1])))
    if not built:
        return True
    return any(b_major == major and b_minor <= minor for b_major, b_minor in built)


def compute_device() -> str:
    """'cuda' when a CUDA GPU is present AND this torch build has kernels for
    it (see cuda_build_supports_device), else 'cpu'. Honors LYRICVIDEO_DEVICE
    as an explicit override -- e.g. to force CPU for a GPU with too little
    memory for the model, or to force cuda past the kernel check."""
    forced = _forced_device()
    if forced:
        return forced
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        arch_list = list(torch.cuda.get_arch_list())
        capability = tuple(torch.cuda.get_device_capability(0))
        if cuda_build_supports_device(arch_list, capability):
            return "cuda"
        print(
            f"GPU {torch.cuda.get_device_name(0)} (compute capability "
            f"{capability[0]}.{capability[1]}) is not supported by this torch build "
            f"({torch.__version__}, kernels for: {' '.join(arch_list) or 'none'}); "
            f"running Demucs on CPU. Install a torch build with kernels for this "
            f"GPU to use it, or set {_DEVICE_ENV}=cuda to try anyway.",
            flush=True,
        )
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


def _demucs_command(audio_path: Path, out_dir: Path, device: str) -> list[str]:
    return [
        sys.executable, "-m", "demucs",
        "--two-stems=vocals",
        "-n", "htdemucs",
        "-d", device,
        "-o", str(out_dir),
        str(audio_path),
    ]


def separate_vocals(audio_path: Path, out_dir: Path, device: str | None = None) -> Path:
    """Run Demucs two-stem separation and return the path to vocals.wav.
    Uses CUDA when available (see compute_device), CPU otherwise.

    When the GPU was picked automatically (no `device` argument and no
    LYRICVIDEO_DEVICE) and Demucs fails on it, the run is retried once on
    CPU: compute_device()'s kernel check rules out the known "no kernel
    image" case up front, but a GPU can still fail for reasons only the real
    run reveals (too little memory for the model, a driver/runtime mismatch),
    and a slow correct render beats a failed song. An explicitly requested
    device is never second-guessed -- it fails loudly, as asked."""
    chosen = device or compute_device()
    auto_cuda = device is None and _forced_device() is None and chosen == "cuda"
    try:
        _run_demucs(_demucs_command(audio_path, out_dir, chosen))
    except subprocess.CalledProcessError as e:
        if not auto_cuda:
            raise
        print(
            f"Demucs failed on the GPU (exit status {e.returncode}); retrying on CPU. "
            f"Set {_DEVICE_ENV}=cpu to skip the GPU attempt next time.",
            flush=True,
        )
        _run_demucs(_demucs_command(audio_path, out_dir, "cpu"))
    stem_name = Path(audio_path).stem
    vocals_path = out_dir / "htdemucs" / stem_name / "vocals.wav"
    if not vocals_path.exists():
        raise SeparationError(f"Demucs did not produce expected output at {vocals_path}")
    return vocals_path
