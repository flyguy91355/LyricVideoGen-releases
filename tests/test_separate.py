from pathlib import Path

import pytest

from lyricvideo import separate as separate_module
from lyricvideo.separate import compute_device, separate_vocals, SeparationError


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


def _fake_cuda(monkeypatch, available: bool):
    import types
    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: available))
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    monkeypatch.delenv("LYRICVIDEO_DEVICE", raising=False)


def test_compute_device_prefers_cuda_when_available(monkeypatch):
    _fake_cuda(monkeypatch, True)
    assert compute_device() == "cuda"


def test_compute_device_falls_back_to_cpu_without_cuda(monkeypatch):
    _fake_cuda(monkeypatch, False)
    assert compute_device() == "cpu"


def test_compute_device_env_override_wins(monkeypatch):
    _fake_cuda(monkeypatch, True)
    monkeypatch.setenv("LYRICVIDEO_DEVICE", "cpu")
    assert compute_device() == "cpu"


def test_compute_device_ignores_bogus_env_value(monkeypatch):
    _fake_cuda(monkeypatch, False)
    monkeypatch.setenv("LYRICVIDEO_DEVICE", "tpu")
    assert compute_device() == "cpu"


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


def test_run_demucs_relays_child_output_through_the_current_sys_stdout(monkeypatch):
    """The GUI swaps sys.stdout for its log-widget writer; a child process
    inherits the OS-level stdout instead, so Demucs's progress used to bypass
    the log entirely. _run_demucs must pipe it through whatever sys.stdout is
    right now -- including \r-driven progress-bar updates, as they arrive."""
    import io
    import sys

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
    import subprocess
    import sys

    with pytest.raises(subprocess.CalledProcessError):
        separate_module._run_demucs([sys.executable, "-c", "raise SystemExit(3)"])
