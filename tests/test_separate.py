import subprocess
import sys

import pytest

from lyricvideo import separate as separate_module
from lyricvideo.separate import (
    SeparationError,
    compute_device,
    cuda_build_supports_device,
    separate_vocals,
)


def test_separate_vocals_returns_expected_path(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"

    expected_vocals = out_dir / "htdemucs" / "song" / "vocals.wav"

    def fake_run(cmd):
        expected_vocals.parent.mkdir(parents=True, exist_ok=True)
        expected_vocals.write_bytes(b"fake-wav")

    monkeypatch.setattr("lyricvideo.separate._run_demucs", fake_run)

    result = separate_vocals(audio_path, out_dir)

    assert result == expected_vocals


def test_separate_vocals_raises_if_output_missing(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"

    monkeypatch.setattr("lyricvideo.separate._run_demucs", lambda cmd: None)

    with pytest.raises(SeparationError):
        separate_vocals(audio_path, out_dir)


# The arch list the real torch 2.14.0+cu130 wheel reports (CUDA 13 dropped
# every architecture below Turing/7.5).
_CU130_ARCHES = ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120"]


def _fake_cuda(monkeypatch, available: bool, capability=(12, 0), arch_list=_CU130_ARCHES):
    """Stand in for torch with a GPU of the given compute capability under a
    build compiled for `arch_list` (RTX 5070 on the cu130 wheel by default)."""
    import types
    fake_torch = types.SimpleNamespace(
        __version__="2.14.0+cu130",
        cuda=types.SimpleNamespace(
            is_available=lambda: available,
            get_arch_list=lambda: list(arch_list),
            get_device_capability=lambda index=0: capability,
            get_device_name=lambda index=0: "Fake GPU",
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.delenv("LYRICVIDEO_DEVICE", raising=False)


def test_compute_device_prefers_cuda_when_available(monkeypatch):
    _fake_cuda(monkeypatch, True)
    assert compute_device() == "cuda"


def test_compute_device_falls_back_to_cpu_without_cuda(monkeypatch):
    _fake_cuda(monkeypatch, False)
    assert compute_device() == "cpu"


def test_compute_device_falls_back_to_cpu_when_the_build_has_no_kernels_for_the_gpu(monkeypatch, capsys):
    """Issue #5: a GT 1030 (CC 6.1) under the cu130 wheel. torch.cuda.is_available()
    is True there, yet Demucs died on its first kernel launch ("no kernel image
    is available for execution on the device"). The device pick must look at
    what the build was compiled for, not just whether a GPU exists -- and say
    why it chose CPU, in the log the GUI shows."""
    _fake_cuda(monkeypatch, True, capability=(6, 1))
    assert compute_device() == "cpu"
    out = capsys.readouterr().out
    assert "Fake GPU" in out
    assert "6.1" in out
    assert "not supported by this torch build" in out
    assert "running Demucs on CPU" in out


def test_compute_device_says_nothing_when_the_gpu_is_usable(monkeypatch, capsys):
    _fake_cuda(monkeypatch, True)
    compute_device()
    assert capsys.readouterr().out == ""


def test_compute_device_falls_back_to_cpu_when_the_probe_itself_fails(monkeypatch):
    """A driver so broken that even asking the GPU's capability raises: be
    honest and say cpu rather than crash the stage."""
    _fake_cuda(monkeypatch, True)

    def boom(index=0):
        raise RuntimeError("CUDA driver initialization failed")

    sys.modules["torch"].cuda.get_device_capability = boom
    assert compute_device() == "cpu"


def test_compute_device_env_override_wins(monkeypatch):
    _fake_cuda(monkeypatch, True)
    monkeypatch.setenv("LYRICVIDEO_DEVICE", "cpu")
    assert compute_device() == "cpu"


def test_compute_device_env_override_cuda_skips_the_kernel_check(monkeypatch):
    """The override is the owner's explicit call; it must not be second-guessed."""
    _fake_cuda(monkeypatch, True, capability=(6, 1))
    monkeypatch.setenv("LYRICVIDEO_DEVICE", "cuda")
    assert compute_device() == "cuda"


def test_compute_device_ignores_bogus_env_value(monkeypatch):
    _fake_cuda(monkeypatch, False)
    monkeypatch.setenv("LYRICVIDEO_DEVICE", "tpu")
    assert compute_device() == "cpu"


@pytest.mark.parametrize(
    "arch_list, capability, expected",
    [
        # Issue #5's exact pairing: GT 1030 (Pascal 6.1) on the cu130 wheel.
        (_CU130_ARCHES, (6, 1), False),
        # RTX 5070 (Blackwell 12.0) on the same wheel: fine.
        (_CU130_ARCHES, (12, 0), True),
        # Lowest supported arch is an exact match.
        (_CU130_ARCHES, (7, 5), True),
        # A newer minor within a built major runs the older cubin (8.9 <- sm_86).
        (_CU130_ARCHES, (8, 9), True),
        # ...but not the other way round: an sm_86 cubin can't run on 8.0 hardware.
        (["sm_86"], (8, 0), False),
        # A cu126-style wheel that still ships Pascal kernels.
        (["sm_50", "sm_60", "sm_61", "sm_70", "sm_75", "sm_80", "sm_86", "sm_90"], (6, 1), True),
        # Arch-specific variants ("sm_90a", "sm_100f") count like their base.
        (["sm_90a"], (9, 0), True),
        (["sm_100f"], (10, 0), True),
        # A PTX "compute_XY" entry counts exactly like sm_XY (torch's own
        # startup check treats them the same).
        (["compute_120"], (12, 0), True),
        (["sm_75", "compute_120"], (6, 1), False),
        # Nothing this code can judge (ROCm gfx names, or an empty list): trust
        # torch.cuda.is_available() rather than switch off a working GPU.
        (["gfx90a", "gfx1100"], (9, 0), True),
        ([], (6, 1), True),
    ],
)
def test_cuda_build_supports_device(arch_list, capability, expected):
    assert cuda_build_supports_device(arch_list, capability) is expected


def test_separate_vocals_passes_selected_device_to_demucs(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"
    seen = {}

    def fake_run(cmd):
        seen["device"] = cmd[cmd.index("-d") + 1]
        (out_dir / "htdemucs" / "song").mkdir(parents=True, exist_ok=True)
        (out_dir / "htdemucs" / "song" / "vocals.wav").write_bytes(b"fake-wav")

    monkeypatch.setattr("lyricvideo.separate._run_demucs", fake_run)
    monkeypatch.setattr(separate_module, "compute_device", lambda: "cuda")
    separate_vocals(audio_path, out_dir)
    assert seen["device"] == "cuda"

    separate_vocals(audio_path, out_dir, device="cpu")
    assert seen["device"] == "cpu"


def _fake_demucs_that_fails_on(out_dir, failing_devices, devices_seen):
    """A stand-in _run_demucs: fails (non-zero exit) on any device in
    `failing_devices`, otherwise writes the stems like the real thing."""
    def fake_run(cmd):
        device = cmd[cmd.index("-d") + 1]
        devices_seen.append(device)
        if device in failing_devices:
            raise subprocess.CalledProcessError(1, cmd)
        (out_dir / "htdemucs" / "song").mkdir(parents=True, exist_ok=True)
        (out_dir / "htdemucs" / "song" / "vocals.wav").write_bytes(b"fake-wav")
    return fake_run


def test_separate_vocals_retries_on_cpu_when_the_auto_picked_gpu_run_fails(tmp_path, monkeypatch, capsys):
    """The kernel check covers the known case up front, but a GPU can still
    fail for reasons only the real run reveals (too little memory, a
    driver/runtime mismatch). A slow correct render beats a failed song."""
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"
    devices = []
    monkeypatch.setattr("lyricvideo.separate._run_demucs", _fake_demucs_that_fails_on(out_dir, {"cuda"}, devices))
    monkeypatch.setattr(separate_module, "compute_device", lambda: "cuda")
    monkeypatch.delenv("LYRICVIDEO_DEVICE", raising=False)

    result = separate_vocals(audio_path, out_dir)

    assert devices == ["cuda", "cpu"]
    assert result == out_dir / "htdemucs" / "song" / "vocals.wav"
    out = capsys.readouterr().out
    assert "Demucs failed on the GPU" in out
    assert "retrying on CPU" in out
    assert "LYRICVIDEO_DEVICE=cpu" in out


def test_separate_vocals_does_not_retry_an_explicitly_requested_device(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"
    devices = []
    monkeypatch.setattr("lyricvideo.separate._run_demucs", _fake_demucs_that_fails_on(out_dir, {"cuda"}, devices))
    monkeypatch.delenv("LYRICVIDEO_DEVICE", raising=False)

    with pytest.raises(subprocess.CalledProcessError):
        separate_vocals(audio_path, out_dir, device="cuda")
    assert devices == ["cuda"]


def test_separate_vocals_does_not_retry_a_device_forced_by_the_env_override(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"
    devices = []
    monkeypatch.setattr("lyricvideo.separate._run_demucs", _fake_demucs_that_fails_on(out_dir, {"cuda"}, devices))
    monkeypatch.setenv("LYRICVIDEO_DEVICE", "cuda")

    with pytest.raises(subprocess.CalledProcessError):
        separate_vocals(audio_path, out_dir)
    assert devices == ["cuda"]


def test_separate_vocals_does_not_retry_a_cpu_failure(tmp_path, monkeypatch):
    """CPU is already the fallback; a failure there is a real error (bad
    audio, broken install) and must surface, not run twice."""
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"
    devices = []
    monkeypatch.setattr("lyricvideo.separate._run_demucs", _fake_demucs_that_fails_on(out_dir, {"cpu"}, devices))
    monkeypatch.setattr(separate_module, "compute_device", lambda: "cpu")
    monkeypatch.delenv("LYRICVIDEO_DEVICE", raising=False)

    with pytest.raises(subprocess.CalledProcessError):
        separate_vocals(audio_path, out_dir)
    assert devices == ["cpu"]


def test_run_demucs_relays_child_output_through_the_current_sys_stdout(monkeypatch):
    """The GUI swaps sys.stdout for its log-widget writer; a child process
    inherits the OS-level stdout instead, so Demucs's progress used to bypass
    the log entirely. _run_demucs must pipe it through whatever sys.stdout is
    right now -- including \r-driven progress-bar updates, as they arrive."""
    import io

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    child = (
        "import sys; sys.stderr.write('to-stderr\\n'); sys.stderr.flush(); "
        "sys.stdout.write('12%\\r99%\\rdone\\n')"
    )

    separate_module._run_demucs([sys.executable, "-c", child])

    assert "to-stderr" in captured.getvalue()
    assert "12%\r99%\rdone" in captured.getvalue()


def test_run_demucs_raises_on_a_nonzero_exit():
    with pytest.raises(subprocess.CalledProcessError):
        separate_module._run_demucs([sys.executable, "-c", "raise SystemExit(3)"])
