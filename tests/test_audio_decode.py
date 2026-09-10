import numpy as np
import pytest

from lyricvideo.audio_decode import decode_audio, find_ffmpeg, probe_duration


def test_find_ffmpeg_returns_an_existing_path():
    exe = find_ffmpeg()
    assert exe  # non-empty string; either "ffmpeg" resolved via PATH or an absolute path


def test_decode_audio_returns_mono_float32_array(tmp_path):
    import subprocess

    wav_path = tmp_path / "tone.wav"
    # Generate 1 second of a 440Hz sine tone with ffmpeg itself, so this test has
    # no dependency on a real song file.
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ar", "22050", "-ac", "1", str(wav_path)],
        check=True,
    )

    y = decode_audio(wav_path, sr=22050)

    assert y.dtype == np.float32
    assert y.ndim == 1
    assert 22000 < y.size < 22100  # ~1 second at 22050 Hz


def test_decode_audio_respects_start_and_duration(tmp_path):
    import subprocess

    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-ar", "22050", "-ac", "1", str(wav_path)],
        check=True,
    )

    full = decode_audio(wav_path, sr=22050)
    windowed = decode_audio(wav_path, sr=22050, start=1.0, duration=1.0)

    assert windowed.size < full.size
    assert 22000 < windowed.size < 22100


def test_probe_duration_reads_real_duration(tmp_path):
    import subprocess

    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         str(wav_path)],
        check=True,
    )

    duration = probe_duration(wav_path)

    assert 1.9 < duration < 2.1
