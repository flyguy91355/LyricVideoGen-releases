from pathlib import Path

from lyricvideo.models import ChordWord, InstrumentalChord, LyricLine


def test_assemble_video_invokes_write_videofile(tmp_path, monkeypatch, test_font_path):
    calls = {}

    class _FakeAudioClip:
        duration = 2.0

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame
            calls["duration"] = duration

        def set_audio(self, audio_clip):
            calls["audio_clip"] = audio_clip
            return self

        def write_videofile(self, path, fps, codec, audio_codec):
            calls["write_path"] = path
            calls["fps"] = fps
            Path(path).write_bytes(b"fake-mp4")

    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: _FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", _FakeVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [
        LyricLine(
            words=[ChordWord(word="hi", chord="G", start_time=0.0, end_time=1.0)],
            start_time=0.0, end_time=1.0,
        )
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(lines, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path)

    assert calls["duration"] == 2.0
    assert calls["fps"] == 24
    # exercise the real make_frame once to confirm the wiring works end to end
    # (no cached image on disk here, so it exercises the fallback-color path)
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (1080, 1920)


def test_assemble_video_threads_instrumental_chords_into_scene(tmp_path, monkeypatch, test_font_path):
    class _FakeAudioClip:
        duration = 2.0

    calls = {}

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame

        def set_audio(self, audio_clip):
            return self

        def write_videofile(self, path, fps, codec, audio_codec):
            Path(path).write_bytes(b"fake-mp4")

    captured = {}
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: _FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", _FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    real_build_scene = assemble_module.build_scene

    def spying_build_scene(lines, t, instrumental_chords=None, **kwargs):
        captured["instrumental_chords"] = instrumental_chords
        return real_build_scene(lines, t, instrumental_chords=instrumental_chords, **kwargs)

    monkeypatch.setattr(assemble_module, "build_scene", spying_build_scene)

    lines = [
        LyricLine(
            words=[ChordWord(word="hi", chord="G", start_time=0.0, end_time=1.0)],
            start_time=0.0, end_time=1.0,
        )
    ]
    instrumental_chords = [InstrumentalChord(chord="Em7", start_time=1.0, end_time=2.0)]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, tmp_path, tmp_path / "audio.wav", out_path,
        font_path=test_font_path, instrumental_chords=instrumental_chords,
    )
    calls["make_frame"](1.5)  # exercise it once inside the instrumental chord's span

    assert captured["instrumental_chords"] == instrumental_chords
