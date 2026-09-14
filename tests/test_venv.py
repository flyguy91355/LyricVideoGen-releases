import sys
from pathlib import Path

from lyricvideo.venv import venv_python


def test_venv_python_windows_layout(tmp_path):
    exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert venv_python(tmp_path, platform="win32") == exe


def test_venv_python_posix_layout(tmp_path):
    exe = tmp_path / ".venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert venv_python(tmp_path, platform="linux") == exe


def test_venv_python_falls_back_to_running_interpreter_when_no_venv(tmp_path):
    assert venv_python(tmp_path, platform="linux") == Path(sys.executable)
    assert venv_python(tmp_path, platform="win32") == Path(sys.executable)


def test_venv_python_defaults_to_current_platform(tmp_path):
    sub = "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
    exe = tmp_path / ".venv" / sub
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert venv_python(tmp_path) == exe
