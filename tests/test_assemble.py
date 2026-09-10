from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word


def _fake_clips(calls):
    class _FakeAudioClip:
        duration = 2.0

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame
            calls["duration"] = duration

        def set_audio(self, audio_clip):
            calls["audio_clip"] = audio_clip
            return self

        def write_videofile(self, path, fps, codec, audio_codec, ffmpeg_params=None):
            calls["write_path"] = path
            calls["fps"] = fps
            calls["codec"] = codec
            calls["ffmpeg_params"] = ffmpeg_params
            Path(path).write_bytes(b"fake-mp4")

    return _FakeAudioClip, _FakeVideoClip


def test_assemble_video_invokes_write_videofile(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path)

    assert calls["duration"] == 2.0
    assert calls["fps"] == 24
    assert calls["codec"] == "libx264"
    assert calls["ffmpeg_params"] == ["-crf", "20"]
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (1080, 1920)


def test_assemble_video_threads_chord_track_into_scene(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    real_build_scene = assemble_module.build_scene

    def spying_build_scene(lines, t, chord_track=None, **kwargs):
        calls["chord_track"] = chord_track
        return real_build_scene(lines, t, chord_track=chord_track, **kwargs)

    monkeypatch.setattr(assemble_module, "build_scene", spying_build_scene)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    chord_track = ChordTrack(events=[ChordEvent(1.0, 2.0, "Em7")])
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](1.5)

    assert calls["chord_track"] == chord_track


def test_assemble_video_draws_chord_bar_on_every_frame(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_chord_bar = assemble_module.draw_chord_bar

    def spying_draw_chord_bar(frame, chord_track, t, font_path, **kwargs):
        draw_calls.append(t)
        return real_draw_chord_bar(frame, chord_track, t, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_bar", spying_draw_chord_bar)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](0.5)

    assert draw_calls == [0.5]


def test_assemble_video_respects_custom_resolution_fps_encoder_crf(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        frame_size=(1280, 720), fps=30, encoder="libx265", crf=24,
    )

    assert calls["fps"] == 30
    assert calls["codec"] == "libx265"
    assert calls["ffmpeg_params"] == ["-crf", "24"]
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (720, 1280)


def test_assemble_video_passes_render_style_kwargs_through_to_draw_scene_and_chord_bar(
    tmp_path, monkeypatch, test_font_path
):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_scene = assemble_module.draw_scene
    real_draw_chord_bar = assemble_module.draw_chord_bar

    def spying_draw_scene(scene, background, font_path, **kwargs):
        captured["draw_scene_kwargs"] = kwargs
        return real_draw_scene(scene, background, font_path, **kwargs)

    def spying_draw_chord_bar(frame, chord_track, t, font_path, **kwargs):
        captured["draw_chord_bar_kwargs"] = kwargs
        return real_draw_chord_bar(frame, chord_track, t, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_scene", spying_draw_scene)
    monkeypatch.setattr(assemble_module, "draw_chord_bar", spying_draw_chord_bar)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        lyric_size=52, text_color=(1, 2, 3), accent_color=(4, 5, 6), show_key_bpm=False,
    )
    calls["make_frame"](0.5)

    assert captured["draw_scene_kwargs"]["font_size"] == 52
    assert captured["draw_scene_kwargs"]["text_color"] == (1, 2, 3)
    assert captured["draw_chord_bar_kwargs"]["accent_color"] == (4, 5, 6)
    assert captured["draw_chord_bar_kwargs"]["show_key_bpm"] is False


def test_assemble_video_draws_chord_legend_with_computed_current_label(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        draw_calls.append((chord_labels, current_label))
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 1.0, "G"), ChordEvent(1.0, 2.0, "D")])
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        chord_legend_labels=["G", "D"],
    )
    calls["make_frame"](1.5)

    assert draw_calls == [(["G", "D"], "D")]


def test_assemble_video_chord_legend_defaults_to_no_labels(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        captured["chord_labels"] = chord_labels
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](0.5)

    assert captured["chord_labels"] == []


def test_assemble_video_respects_show_chord_legend_toggle(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        captured["show_chord_legend"] = kwargs.get("show_chord_legend")
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        show_chord_legend=False,
    )
    calls["make_frame"](0.5)

    assert captured["show_chord_legend"] is False


def test_assemble_video_passes_chord_legend_scale_through(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        captured["size_scale"] = kwargs.get("size_scale")
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        chord_legend_scale=0.6,
    )
    calls["make_frame"](0.5)

    assert captured["size_scale"] == 0.6
