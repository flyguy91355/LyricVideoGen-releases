from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word


def _fake_clips(calls):
    class _FakeAudioClip:
        duration = 2.0

        def set_start(self, t):
            calls["audio_set_start"] = t
            return self

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

    assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=0,
    )

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
        countdown_beats=0,
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
        countdown_beats=0,
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
        frame_size=(1280, 720), fps=30, encoder="libx265", crf=24, countdown_beats=0,
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
        countdown_beats=0,
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
        chord_legend_labels=["G", "D"], countdown_beats=0,
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
        countdown_beats=0,
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
        show_chord_legend=False, countdown_beats=0,
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
        chord_legend_scale=0.6, countdown_beats=0,
    )
    calls["make_frame"](0.5)

    assert captured["size_scale"] == 0.6


def test_assemble_video_passes_chord_diagram_panel_alpha_through(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    captured = {}
    real_draw_chord_legend = assemble_module.draw_chord_legend

    def spying_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs):
        captured["panel_alpha"] = kwargs.get("panel_alpha")
        return real_draw_chord_legend(frame, chord_labels, current_label, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_chord_legend", spying_draw_chord_legend)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        chord_diagram_panel_alpha=90, countdown_beats=0,
    )
    calls["make_frame"](0.5)

    assert captured["panel_alpha"] == 90


def test_assemble_video_default_countdown_extends_total_duration(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)
    monkeypatch.setattr("lyricvideo.assemble.CompositeAudioClip", lambda clips: clips[0])

    from lyricvideo.assemble import assemble_video

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path)

    # FakeAudioClip.duration is 2.0. ChordTrack() has no bpm (0.0), so the
    # DEFAULT_COUNTDOWN_BPM (120) fallback applies -> beat_duration 0.5s;
    # default countdown_beats is 4 -> countdown_duration 4 * 0.5 = 2.0s.
    assert calls["duration"] == 4.0
    assert calls["audio_set_start"] == 2.0


def test_assemble_video_countdown_disabled_matches_old_behavior(tmp_path, monkeypatch, test_font_path):
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
        countdown_beats=0,
    )

    assert calls["duration"] == 2.0
    assert "audio_set_start" not in calls  # CompositeAudioClip path never touched


def test_assemble_video_frame_during_countdown_uses_draw_countdown_not_the_scene(
    tmp_path, monkeypatch, test_font_path,
):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)
    monkeypatch.setattr("lyricvideo.assemble.CompositeAudioClip", lambda clips: clips[0])

    from lyricvideo import assemble as assemble_module

    countdown_calls = []
    real_draw_countdown = assemble_module.draw_countdown

    def spying_draw_countdown(frame, seconds_remaining, font_path, **kwargs):
        countdown_calls.append(seconds_remaining)
        return real_draw_countdown(frame, seconds_remaining, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_countdown", spying_draw_countdown)

    scene_calls = []
    real_draw_scene = assemble_module.draw_scene

    def spying_draw_scene(scene, background, font_path, **kwargs):
        scene_calls.append(True)
        return real_draw_scene(scene, background, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_scene", spying_draw_scene)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(bpm=120.0), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=4,
    )
    # beat_duration = 60/120 = 0.5s; countdown_duration = 4 * 0.5 = 2.0s.
    # T=0.5 is still inside the lead-in (song_t = 0.5 - 2.0 = -1.5).
    calls["make_frame"](0.5)

    assert countdown_calls == [3]
    assert scene_calls == []


def test_assemble_video_frame_after_countdown_uses_song_relative_time(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)
    monkeypatch.setattr("lyricvideo.assemble.CompositeAudioClip", lambda clips: clips[0])

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
        lines, ChordTrack(bpm=120.0), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=4,
    )
    # countdown_duration = 4 beats * 0.5s = 2.0s; T=2.5 in the outer
    # timeline is 0.5 seconds into the real song.
    calls["make_frame"](2.5)

    assert draw_calls == [0.5]


def test_assemble_video_countdown_number_decreases_each_beat(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)
    monkeypatch.setattr("lyricvideo.assemble.CompositeAudioClip", lambda clips: clips[0])

    from lyricvideo import assemble as assemble_module

    countdown_calls = []
    real_draw_countdown = assemble_module.draw_countdown

    def spying_draw_countdown(frame, seconds_remaining, font_path, **kwargs):
        countdown_calls.append(seconds_remaining)
        return real_draw_countdown(frame, seconds_remaining, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_countdown", spying_draw_countdown)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(bpm=120.0), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=4,
    )
    # beat_duration = 0.5s: beat 0 = [0, 0.5), beat 1 = [0.5, 1.0),
    # beat 2 = [1.0, 1.5), beat 3 (clamped, last) = [1.5, 2.0).
    for t in (0.0, 0.4, 0.5, 0.9, 1.0, 1.4, 1.5, 1.9):
        calls["make_frame"](t)

    assert countdown_calls == [4, 4, 3, 3, 2, 2, 1, 1]


def test_first_available_image_key_prefers_the_preferred_key_when_it_exists(tmp_path):
    from lyricvideo.assemble import _first_available_image_key
    from PIL import Image

    Image.new("RGB", (4, 4)).save(tmp_path / "preferred.png")
    Image.new("RGB", (4, 4)).save(tmp_path / "other.png")

    assert _first_available_image_key(tmp_path, "preferred") == "preferred"


def test_first_available_image_key_falls_back_to_any_real_image(tmp_path):
    from lyricvideo.assemble import _first_available_image_key
    from PIL import Image

    Image.new("RGB", (4, 4)).save(tmp_path / "some_other_image.png")

    # "missing" was never generated for this song, but a real image exists --
    # owner explicitly asked for the beginning frame, never a blank/flat one.
    assert _first_available_image_key(tmp_path, "missing") == "some_other_image"


def test_first_available_image_key_returns_none_when_truly_no_images_exist(tmp_path):
    from lyricvideo.assemble import _first_available_image_key

    assert _first_available_image_key(tmp_path, "missing") is None


def test_assemble_video_countdown_uses_a_real_image_not_the_missing_preferred_one(
    tmp_path, monkeypatch, test_font_path,
):
    """Real owner complaint, 2026-09-10: the countdown background showed as
    blank. Simulates exactly that setup -- the scene's own preferred image
    key has no file, but a real image was generated for the song -- and
    confirms the countdown background comes from that real file, not the
    flat fallback_color."""
    from PIL import Image
    import numpy as np

    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)
    monkeypatch.setattr("lyricvideo.assemble.CompositeAudioClip", lambda clips: clips[0])

    distinctive_color = (10, 200, 30)
    Image.new("RGB", (1920, 1080), distinctive_color).save(tmp_path / "some_real_image.png")

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    from lyricvideo import assemble as assemble_module
    assemble_module.assemble_video(
        lines, ChordTrack(bpm=120.0), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=4, fallback_color=(1, 2, 3),
    )
    frame = calls["make_frame"](0.1)  # inside the countdown lead-in

    # The frame must show the real generated image's color somewhere, and
    # must NOT be uniformly the flat fallback_color.
    assert not np.all(frame.reshape(-1, 3) == (1, 2, 3))
    assert np.any(np.all(np.abs(frame.astype(int) - np.array(distinctive_color)) < 10, axis=-1))
