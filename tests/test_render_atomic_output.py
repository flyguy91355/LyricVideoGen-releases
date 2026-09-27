"""Real-ffmpeg checks of assemble_video's output handling (issue #7: EASY CHORD videos with ~80 s of picture against
5-6 minutes of audio were listed as finished and uploaded). moviepy writes the whole audio track first and then pipes
frames into an ffmpeg that muxes it in, so a render that raised or was killed partway used to leave a PLAYABLE,
truncated mp4 at the final path. These tests render tiny synthetic songs (made-up words, a sine tone) for real."""

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word

FRAME = (640, 360)  # the chord bar's fixed margins need at least roughly this much room
FPS = 5


def _write_tone(path: Path, seconds: float, rate: int = 8000) -> None:
    t = np.arange(int(seconds * rate)) / rate
    samples = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def _song() -> tuple[list[LyricLine], ChordTrack]:
    words = [Word(word="zorp", start_time=0.5, end_time=1.0), Word(word="blee", start_time=1.0, end_time=1.6)]
    lines = [LyricLine(words=words, start_time=0.5, end_time=1.6)]
    track = ChordTrack(events=[ChordEvent(0.0, 1.5, "G"), ChordEvent(1.5, 3.0, "C")], key="G major", bpm=120.0)
    return lines, track


@pytest.fixture
def render_dirs(tmp_path, monkeypatch):
    """(work dir, an empty CWD). moviepy used to drop <stem>TEMP_MPY_wvf_snd.mp4 into the CWD -- the repo root
    when the app is launched normally -- and never removed it after a failed render."""
    work = tmp_path / "work"
    (work / "images").mkdir(parents=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    _write_tone(work / "song.wav", 3.0)
    return work, cwd


def _render(work: Path, font_path: str, **kwargs) -> Path:
    from lyricvideo.assemble import assemble_video

    lines, track = _song()
    out_path = work / "song.mp4"
    assemble_video(
        lines, track, work / "images", work / "song.wav", out_path, font_path,
        frame_size=FRAME, fps=FPS, chord_legend_labels=["G", "C"], **kwargs,
    )
    return out_path


def _stream_info(path: Path) -> str:
    from lyricvideo.assemble import _ffmpeg_binary

    proc = subprocess.run([_ffmpeg_binary(), "-hide_banner", "-i", str(path)], capture_output=True)
    return proc.stderr.decode("utf-8", "replace")


def test_a_successful_render_leaves_only_the_finished_video(render_dirs, test_font_path):
    from lyricvideo.assemble import _rendered_video_frame_count, rendered_stream_seconds

    work, cwd = render_dirs
    out_path = _render(work, test_font_path, countdown_beats=2)  # 2 beats at 120 BPM = 1 s of count-in

    assert out_path.exists()
    # countdown (1 s) + song (3 s), every frame present
    assert _rendered_video_frame_count(out_path) == 4 * FPS
    picture, sound = rendered_stream_seconds(out_path)
    assert picture == pytest.approx(4.0)
    assert sound == pytest.approx(4.0, abs=0.2)
    assert sorted(p.name for p in work.iterdir()) == ["images", "song.mp4", "song.wav"]
    assert list(cwd.iterdir()) == []


def test_a_render_that_fails_partway_keeps_the_previous_video_and_leaves_nothing_behind(
    render_dirs, test_font_path, monkeypatch,
):
    import lyricvideo.assemble as assemble_module

    work, cwd = render_dirs
    out_path = work / "song.mp4"
    out_path.write_bytes(b"the previous, complete video")

    real_draw_chord_bar = assemble_module.draw_chord_bar
    frames_drawn = []

    def failing_draw_chord_bar(*args, **kwargs):
        frames_drawn.append(1)
        if len(frames_drawn) > 4:
            raise MemoryError("out of memory partway through the render")
        return real_draw_chord_bar(*args, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_bar", failing_draw_chord_bar)

    with pytest.raises(MemoryError):
        _render(work, test_font_path, countdown_beats=0)

    # Before: ffmpeg finalized ~1 s of picture + all 3 s of audio AT out_path (and -y had already wiped the old one).
    assert out_path.read_bytes() == b"the previous, complete video"
    assert sorted(p.name for p in work.iterdir()) == ["images", "song.mp4", "song.wav"]
    assert list(cwd.iterdir()) == []


def test_a_failed_first_render_leaves_no_video_at_all(render_dirs, test_font_path, monkeypatch):
    import lyricvideo.assemble as assemble_module

    work, cwd = render_dirs
    real_draw_chord_legend = assemble_module.draw_chord_legend
    frames_drawn = []

    def failing_draw_chord_legend(*args, **kwargs):
        # moviepy draws frame 0 once while building the clip; fail a few frames into the real write
        frames_drawn.append(1)
        if len(frames_drawn) > 3:
            raise RuntimeError("frame failed")
        return real_draw_chord_legend(*args, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", failing_draw_chord_legend)

    with pytest.raises(RuntimeError, match="frame failed"):
        _render(work, test_font_path, countdown_beats=0)

    assert not (work / "song.mp4").exists()
    assert sorted(p.name for p in work.iterdir()) == ["images", "song.wav"]
    assert list(cwd.iterdir()) == []


def test_a_render_whose_picture_comes_back_short_is_not_kept(render_dirs, test_font_path, monkeypatch):
    """Belt and braces: even if a writer ever stopped early WITHOUT raising, the read-back check refuses it."""
    import lyricvideo.assemble as assemble_module

    work, _cwd = render_dirs
    monkeypatch.setattr(assemble_module, "_rendered_video_frame_count", lambda path: 2)

    with pytest.raises(RuntimeError, match="cut short"):
        _render(work, test_font_path, countdown_beats=0)

    assert not (work / "song.mp4").exists()
    assert sorted(p.name for p in work.iterdir()) == ["images", "song.wav"]


def test_the_length_check_sees_through_a_truncated_mp4s_full_length_container(tmp_path, monkeypatch):
    """Reproduces the damaged files themselves: moviepy's writer, killed partway, still finalizes an mp4 whose
    container reports the full AUDIO length. The frame count read back from the video stream is what exposes it."""
    from lyricvideo.assemble import _check_rendered_video, _load_moviepy, _rendered_video_frame_count

    _load_moviepy()
    from lyricvideo import assemble as assemble_module

    monkeypatch.chdir(tmp_path)
    _write_tone(tmp_path / "tone.wav", 4.0)
    audio = assemble_module.AudioFileClip(str(tmp_path / "tone.wav"))

    def make_frame(t):
        if t > 1.0:
            raise RuntimeError("killed")
        return np.zeros((64, 64, 3), dtype=np.uint8)

    truncated = tmp_path / "truncated.mp4"
    try:
        clip = assemble_module.VideoClip(make_frame, duration=4.0).set_audio(audio)
        with pytest.raises(RuntimeError, match="killed"):
            clip.write_videofile(str(truncated), fps=FPS, codec="libx264", audio_codec="aac", logger=None)
    finally:
        audio.close()

    assert truncated.exists()  # the playable-but-truncated file the old code left at the final path
    assert _rendered_video_frame_count(truncated) <= 2 * FPS
    with pytest.raises(RuntimeError, match="cut short"):
        _check_rendered_video(truncated, 4.0, FPS)
    picture, sound = assemble_module.rendered_stream_seconds(truncated)
    assert picture <= 2.0
    assert sound == pytest.approx(4.0, abs=0.2)


def test_libx265_output_is_4_2_0_and_tagged_hvc1(render_dirs, test_font_path):
    """libx265 used to get moviepy's raw rgb24 frames unchanged: 4:4:4 RGB HEVC (gbrp) tagged hev1, which most
    players and browsers refuse."""
    work, _cwd = render_dirs
    out_path = _render(work, test_font_path, countdown_beats=0, encoder="libx265", crf=28)

    info = _stream_info(out_path)
    video_line = next(line for line in info.splitlines() if "Video:" in line)
    assert "hevc" in video_line
    assert "hvc1" in video_line
    assert "yuv420p" in video_line
    assert "gbrp" not in video_line
