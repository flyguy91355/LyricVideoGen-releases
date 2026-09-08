from pathlib import Path

import pytest

from lyricvideo.separate import separate_vocals, SeparationError


def test_separate_vocals_returns_expected_path(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"

    expected_vocals = out_dir / "htdemucs" / "song" / "vocals.wav"

    def fake_run(cmd, check):
        expected_vocals.parent.mkdir(parents=True, exist_ok=True)
        expected_vocals.write_bytes(b"fake-wav")

    monkeypatch.setattr("lyricvideo.separate.subprocess.run", fake_run)

    result = separate_vocals(audio_path, out_dir)

    assert result == expected_vocals


def test_separate_vocals_raises_if_output_missing(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"

    monkeypatch.setattr("lyricvideo.separate.subprocess.run", lambda cmd, check: None)

    with pytest.raises(SeparationError):
        separate_vocals(audio_path, out_dir)
