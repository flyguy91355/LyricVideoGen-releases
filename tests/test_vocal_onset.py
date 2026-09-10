import subprocess

from lyricvideo.audio_decode import find_ffmpeg
from lyricvideo.vocal_onset import vocal_onset_rise


def test_vocal_onset_rise_returns_one_score_per_candidate(tmp_path):
    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=10", "-ar", "22050", str(wav_path)],
        check=True,
    )

    rises = vocal_onset_rise(wav_path, times=[2.0, 5.0, 8.0])

    assert len(rises) == 3
    assert all(isinstance(r, float) for r in rises)


def test_vocal_onset_rise_empty_times_returns_empty_list(tmp_path):
    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=3", str(wav_path)],
        check=True,
    )

    assert vocal_onset_rise(wav_path, times=[]) == []


def test_vocal_onset_rise_never_returns_nan(tmp_path):
    import math

    wav_path = tmp_path / "silence.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "5", str(wav_path)],
        check=True,
    )

    rises = vocal_onset_rise(wav_path, times=[1.0, 3.0])

    assert all(math.isfinite(r) for r in rises)
