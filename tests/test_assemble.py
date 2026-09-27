from pathlib import Path

import pytest
from PIL import Image

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word, line_hash


@pytest.fixture(autouse=True)
def _skip_the_rendered_length_check(monkeypatch):
    """The fake clips below write a few placeholder bytes, not a real video, so the read-back length check has
    nothing to measure; tests/test_render_atomic_output.py covers that check against real ffmpeg output."""
    monkeypatch.setattr("lyricvideo.assemble._check_rendered_video", lambda path, expected_seconds, fps: None)


def _fake_clips(calls):
    class _FakeAudioClip:
        duration = 2.0

        def set_start(self, t):
            calls["audio_set_start"] = t
            return self

        def close(self):
            calls["audio_closed"] = calls.get("audio_closed", 0) + 1

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame
            calls["duration"] = duration

        def set_audio(self, audio_clip):
            calls["audio_clip"] = audio_clip
            return self

        def write_videofile(self, path, fps, codec, audio_codec, ffmpeg_params=None, temp_audiofile=None):
            calls["write_path"] = path
            calls["fps"] = fps
            calls["codec"] = codec
            calls["ffmpeg_params"] = ffmpeg_params
            calls["temp_audiofile"] = temp_audiofile
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
    # moviepy itself adds -pix_fmt yuv420p for libx264; the partial file's container is named explicitly.
    assert calls["ffmpeg_params"] == ["-crf", "20", "-f", "mp4"]
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (1080, 1920)
    assert out_path.read_bytes() == b"fake-mp4"


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


def test_assemble_video_crossfades_between_chord_images_across_the_swap(tmp_path, monkeypatch, test_font_path):
    """Real owner ask: image swaps during an instrumental should dissolve,
    not hard-cut. Two solid-colored chord images, min_hold long enough that
    neither chord gets merged with the other -- a frame mid-transition must
    be a genuine blend of both colors, a frame well before the swap must be
    pure C, and a frame well after must be pure Am."""
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    FakeAudioClip.duration = 12.0
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    chord_track = ChordTrack(events=[ChordEvent(1.0, 6.0, "C"), ChordEvent(6.0, 12.0, "Am")])
    Image.new("RGB", (1920, 1080), (0, 0, 0)).save(
        tmp_path / f"{line_hash('[Instrumental — chord: C]')}.png"
    )
    Image.new("RGB", (1920, 1080), (200, 200, 200)).save(
        tmp_path / f"{line_hash('[Instrumental — chord: Am]')}.png"
    )
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=0, image_transition_seconds=2.0,
    )

    pure_c = calls["make_frame"](2.0)
    mid_transition = calls["make_frame"](6.5)
    pure_am = calls["make_frame"](11.9)

    assert tuple(pure_c[0, 0]) == (0, 0, 0)
    assert tuple(pure_am[0, 0]) == (200, 200, 200)
    r, g, b = mid_transition[0, 0]
    assert 0 < r < 200 and r == g == b


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
    # 4:2:0 + the hvc1 tag: without them libx265 wrote 4:4:4 RGB HEVC tagged hev1 that most players refuse.
    assert calls["ffmpeg_params"] == ["-crf", "24", "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-f", "mp4"]
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


def test_assemble_video_draws_capo_badge_when_capo_is_given(tmp_path, monkeypatch, test_font_path):
    """Owner, 2026-09-23: "i want the CAPO 3 under the chords finger position area" -- an EASY CHORD (capo
    conversion) video only; assemble_video() must never draw this badge unless a real capo run asks for it."""
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    monkeypatch.setattr(assemble_module, "draw_capo_badge", lambda frame, capo, font_path, **kwargs: draw_calls.append(capo) or frame)

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 1.0, "G")])
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        chord_legend_labels=["G"], countdown_beats=0, capo=3,
    )
    calls["make_frame"](0.5)

    assert draw_calls == [3]


def test_assemble_video_passes_the_original_key_to_the_key_badge(tmp_path, monkeypatch, test_font_path):
    """Owner, 2026-09-23: a capo doesn't change the song's key -- the Key badge shows the ORIGINAL key."""
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    bar_keys = []
    monkeypatch.setattr(
        assemble_module, "draw_chord_bar",
        lambda frame, chord_track, t, font_path, **kwargs: bar_keys.append(kwargs.get("key_label")) or frame,
    )

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 1.0, "E")], key="E major")
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        chord_legend_labels=["E"], countdown_beats=0, capo=2, key_label="F# major",
    )
    calls["make_frame"](0.5)

    assert bar_keys == ["F# major"]


def test_assemble_video_does_not_draw_a_capo_badge_by_default(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    monkeypatch.setattr(assemble_module, "draw_capo_badge", lambda frame, capo, font_path, **kwargs: draw_calls.append(capo) or frame)

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 1.0, "G")])
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        chord_legend_labels=["G"], countdown_beats=0,
    )
    calls["make_frame"](0.5)

    assert draw_calls == [None]


def test_assemble_video_draws_support_overlay_within_the_lead_window_before_the_end(
    tmp_path, monkeypatch, test_font_path,
):
    """Owner request, 2026-09-11: not the whole video -- just the last
    support_overlay_lead_seconds before the song ends."""
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    FakeAudioClip.duration = 100.0
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_support_overlay = assemble_module.draw_support_overlay

    def spying_draw_support_overlay(frame, text, font_path, **kwargs):
        draw_calls.append((text, kwargs.get("scale")))
        return real_draw_support_overlay(frame, text, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_support_overlay", spying_draw_support_overlay)

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=0, support_overlay_text="Support: ko-fi.com/x", support_overlay_scale=1.5,
        support_overlay_lead_seconds=20.0,
    )
    calls["make_frame"](90.0)  # 10s from the end -- inside the 20s window

    assert draw_calls == [("Support: ko-fi.com/x", 1.5)]


def test_assemble_video_does_not_draw_support_overlay_before_the_lead_window(
    tmp_path, monkeypatch, test_font_path,
):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    FakeAudioClip.duration = 100.0
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_support_overlay = assemble_module.draw_support_overlay

    def spying_draw_support_overlay(frame, text, font_path, **kwargs):
        draw_calls.append(text)
        return real_draw_support_overlay(frame, text, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_support_overlay", spying_draw_support_overlay)

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=0, support_overlay_text="Support: ko-fi.com/x", support_overlay_lead_seconds=20.0,
    )
    calls["make_frame"](50.0)  # well before the last 20s

    assert draw_calls == []


def test_assemble_video_support_overlay_defaults_to_blank_text(tmp_path, monkeypatch, test_font_path):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_support_overlay = assemble_module.draw_support_overlay

    def spying_draw_support_overlay(frame, text, font_path, **kwargs):
        draw_calls.append(text)
        return real_draw_support_overlay(frame, text, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_support_overlay", spying_draw_support_overlay)

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=0,
    )
    calls["make_frame"](0.5)

    assert draw_calls == [""]


def test_assemble_video_does_not_draw_support_overlay_during_the_countdown(tmp_path, monkeypatch, test_font_path):
    """Owner request, 2026-09-11: only the last N seconds before the song
    ends -- never the intro/countdown lead-in."""
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)
    monkeypatch.setattr("lyricvideo.assemble.CompositeAudioClip", lambda clips: clips[0])

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_support_overlay = assemble_module.draw_support_overlay

    def spying_draw_support_overlay(frame, text, font_path, **kwargs):
        draw_calls.append(text)
        return real_draw_support_overlay(frame, text, font_path, **kwargs)

    monkeypatch.setattr(assemble_module, "draw_support_overlay", spying_draw_support_overlay)

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
        countdown_beats=4, support_overlay_text="Support: ko-fi.com/x",
    )
    calls["make_frame"](0.1)  # well within the countdown lead-in

    assert draw_calls == []


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


def test_assemble_video_missing_image_key_falls_back_to_a_real_image_not_a_flat_color(
    tmp_path, monkeypatch, test_font_path,
):
    """Same rule the countdown already follows, applied to every frame: a key
    with no file behind it must show some real generated image for the song,
    never the flat fallback_color (real owner complaint class: a blank
    background mid-video)."""
    from PIL import Image
    import numpy as np

    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)
    monkeypatch.setattr("lyricvideo.assemble.CompositeAudioClip", lambda clips: clips[0])

    distinctive_color = (10, 200, 30)
    Image.new("RGB", (1920, 1080), distinctive_color).save(tmp_path / f"{line_hash('hi')}.png")

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=0.5)], start_time=0.0, end_time=0.5)
    ]
    # An instrumental stretch after the line whose chord image was never generated.
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "Am")], bpm=120.0)

    from lyricvideo import assemble as assemble_module
    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", tmp_path / "final.mp4", font_path=test_font_path,
        countdown_beats=0, fallback_color=(1, 2, 3),
    )
    frame = calls["make_frame"](1.5)  # inside the Am stretch, past the sung line

    assert not np.all(frame.reshape(-1, 3) == (1, 2, 3))
    assert np.any(np.all(np.abs(frame.astype(int) - np.array(distinctive_color)) < 10, axis=-1))


def test_assemble_video_closes_the_audio_clip_after_writing(tmp_path, monkeypatch, test_font_path):
    """AudioFileClip keeps an ffmpeg reader subprocess (and the source file)
    open until close() -- moviepy never does that itself, so every render
    leaked one for the rest of the session: a whole batch run's worth of
    zombie ffmpeg processes (found by code review, 2026-09-14)."""
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", tmp_path / "final.mp4", font_path=test_font_path,
        countdown_beats=0,
    )

    assert calls["audio_closed"] == 1


def test_assemble_video_closes_the_audio_clip_even_when_the_write_fails(tmp_path, monkeypatch, test_font_path):
    import pytest

    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)

    class _FailingVideoClip(FakeVideoClip):
        def write_videofile(self, *args, **kwargs):
            raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", _FailingVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    with pytest.raises(RuntimeError, match="ffmpeg exploded"):
        assemble_video(
            lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", tmp_path / "final.mp4",
            font_path=test_font_path, countdown_beats=0,
        )

    assert calls["audio_closed"] == 1


def test_assemble_video_hands_ken_burns_an_already_frame_sized_background(tmp_path, monkeypatch, test_font_path):
    """Backgrounds are cached scaled to the output frame ONCE, so the
    per-frame Ken Burns pan never re-resamples the raw generation (which is
    a different size) on every single frame."""
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    import lyricvideo.assemble as assemble_module
    from lyricvideo.assemble import assemble_video

    lines = [LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)]
    Image.new("RGB", (640, 360), (200, 30, 30)).save(tmp_path / f"{line_hash('hi')}.png")

    seen_sizes = []
    real_apply_ken_burns = assemble_module.apply_ken_burns

    def spying_apply_ken_burns(image, *args, **kwargs):
        seen_sizes.append(image.size)
        return real_apply_ken_burns(image, *args, **kwargs)

    monkeypatch.setattr(assemble_module, "apply_ken_burns", spying_apply_ken_burns)

    assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", tmp_path / "final.mp4", font_path=test_font_path,
        countdown_beats=0, frame_size=(1280, 720),
    )
    calls["make_frame"](0.5)

    assert seen_sizes and all(size == (1280, 720) for size in seen_sizes)


# --- issue #7 review: outgoing Ken Burns progress, bounded background cache, lazy moviepy -------------------------

def _two_lines_with_a_long_break():
    """Line 1 sung 10-14 s, a 20 s instrumental break, line 2 sung 34-38 s (made-up words)."""
    def line(words, start):
        ws = [Word(word=w, start_time=start + i, end_time=start + i + 1.0) for i, w in enumerate(words)]
        return LyricLine(words=ws, start_time=ws[0].start_time, end_time=ws[-1].end_time)
    return [line(["zorp", "blee", "quix", "vosh"], 10.0), line(["mip", "tandle", "keem", "plin"], 34.0)]


def _ken_burns_progress_per_frame(tmp_path, monkeypatch, test_font_path, times):
    calls = {}
    FakeAudioClip, FakeVideoClip = _fake_clips(calls)
    FakeAudioClip.duration = 60.0
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    progress_calls = []
    real_apply_ken_burns = assemble_module.apply_ken_burns

    def spying_apply_ken_burns(image, progress, *args, **kwargs):
        progress_calls.append(progress)
        return real_apply_ken_burns(image, progress, *args, **kwargs)

    monkeypatch.setattr(assemble_module, "apply_ken_burns", spying_apply_ken_burns)
    assemble_module.assemble_video(
        _two_lines_with_a_long_break(), ChordTrack(), tmp_path, tmp_path / "audio.wav", tmp_path / "final.mp4",
        font_path=test_font_path, countdown_beats=0, frame_size=(1280, 720),
    )
    per_frame = []
    for t in times:
        progress_calls.clear()
        calls["make_frame"](t)
        per_frame.append(list(progress_calls))
    return per_frame


def test_outgoing_line_image_keeps_its_own_pan_position_into_an_instrumental_crossfade(
    tmp_path, monkeypatch, test_font_path,
):
    """A sung line's pan is paced to the NEXT line's start (34 s), but its image segment ends at 14 s when a break
    follows, so it is only 1/6 through its pan there. The first crossfade frame is 100% the outgoing image; drawing
    it at progress 1.0 snapped zoom and pan by up to 15% / ~160 px in one frame at every verse-to-break handoff."""
    (last_line_frame,), (current, outgoing) = _ken_burns_progress_per_frame(
        tmp_path, monkeypatch, test_font_path, [13.99, 14.0],
    )

    assert last_line_frame == pytest.approx(3.99 / 24, abs=1e-6)
    assert current == pytest.approx(0.0)
    assert outgoing == pytest.approx(4.0 / 24, abs=1e-4)  # where line 1's pan really stood -- not 1.0


def test_outgoing_instrumental_image_still_ends_at_the_end_of_its_own_pan(tmp_path, monkeypatch, test_font_path):
    """An instrumental block's pan is paced to its own segment, so handing off to the next line keeps ~1.0."""
    ((current, outgoing),) = _ken_burns_progress_per_frame(tmp_path, monkeypatch, test_font_path, [34.0])

    assert current == pytest.approx(0.0)
    assert outgoing == pytest.approx(1.0, abs=1e-4)


def test_background_cache_shares_one_image_across_missing_keys_and_stays_bounded(tmp_path, monkeypatch):
    """Every missing key (e.g. an instrumental caption with no generated image) used to decode and keep its OWN
    frame-sized copy of the same fallback file, and nothing was ever evicted until the render ended."""
    from lyricvideo import assemble as assemble_module
    from lyricvideo.assemble import _IMAGE_CACHE_SIZE, _BackgroundCache

    for name in "abcdef":
        Image.new("RGB", (8, 8), (ord(name), 0, 0)).save(tmp_path / f"{name}.png")
    opened = []
    real_open = assemble_module.Image.open

    def counting_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(assemble_module.Image, "open", counting_open)
    cache = _BackgroundCache(tmp_path, (32, 18), (1, 2, 3))

    first = cache.get("missing-one")  # resolves to the first real image, a.png
    assert cache.get("missing-two") is first
    assert cache.get("a") is first
    assert opened == ["a.png"]
    assert first.size == (32, 18)

    for name in "bcdef":
        cache.get(name)
    assert len(cache) <= _IMAGE_CACHE_SIZE
    assert opened == ["a.png", "b.png", "c.png", "d.png", "e.png", "f.png"]


def test_background_cache_falls_back_to_one_shared_flat_image_when_the_song_has_none(tmp_path):
    from lyricvideo.assemble import _BackgroundCache

    cache = _BackgroundCache(tmp_path, (32, 18), (1, 2, 3))

    flat = cache.get("missing-one")
    assert cache.get("missing-two") is flat
    assert flat.getpixel((0, 0)) == (1, 2, 3)


def test_importing_the_renderer_does_not_import_moviepy():
    """pipeline.py (and so gui.py) imports this module; moviepy.editor alone took ~0.6 s of every GUI launch."""
    import subprocess
    import sys

    probe = "import sys, lyricvideo.assemble; print('moviepy' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_render_temp_files_sit_next_to_the_output_under_names_that_are_not_videos(tmp_path):
    from lyricvideo.assemble import _partial_render_paths

    out_path = tmp_path / "song-easychords.mp4"
    partial, temp_audio, params = _partial_render_paths(out_path)

    assert partial.parent == temp_audio.parent == tmp_path
    assert not partial.name.endswith(".mp4")  # never mistaken for a finished video by a glob("*.mp4")
    assert not temp_audio.name.endswith(".mp4")
    assert partial.name.startswith(out_path.name) and temp_audio.name.startswith(out_path.name)
    assert params == ["-f", "mp4"]
