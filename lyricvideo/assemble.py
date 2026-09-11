from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

try:
    from moviepy.editor import AudioFileClip, CompositeAudioClip, VideoClip  # moviepy < 2.0
except ImportError:
    from moviepy import AudioFileClip, CompositeAudioClip, VideoClip  # moviepy >= 2.0 dropped .editor

from .chord_diagram import draw_chord_legend
from .layout import build_image_timeline, build_scene
from .models import ChordTrack, LyricLine, current_chord_at
from .render import (
    ACCENT_COLOR, DIM_TEXT_COLOR, FRAME_SIZE, apply_ken_burns, crossfade_backgrounds, draw_chord_bar, draw_countdown,
    draw_scene, draw_support_overlay, ken_burns_preset_for_key,
)

FPS = 24
DEFAULT_COUNTDOWN_BPM = 120.0  # used only if a song's own BPM wasn't detected (0 or missing)


def _first_available_image_key(image_dir: Path, preferred_key: str) -> str | None:
    """The countdown needs a guaranteed-real background, never a plain
    fallback color -- real owner complaint, 2026-09-10 ("dont have a blank
    screen"). `preferred_key` (whatever the real first moment of the song
    would show, for visual continuity into it) is used if its file actually
    exists; otherwise falls back to ANY real image already generated for
    this song rather than a flat color. Returns None only if the song has
    no images at all yet."""
    if (image_dir / f"{preferred_key}.png").exists():
        return preferred_key
    candidates = sorted(image_dir.glob("*.png"))
    return candidates[0].stem if candidates else None


def assemble_video(
    lines: list[LyricLine],
    chord_track: ChordTrack,
    image_dir: Path,
    audio_path: Path,
    out_path: Path,
    font_path: str,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
    *,
    frame_size: tuple[int, int] = FRAME_SIZE,
    fps: int = FPS,
    encoder: str = "libx264",
    crf: int = 20,
    lyric_size: int = 48,
    text_color: tuple[int, int, int] = (255, 255, 255),
    accent_color: tuple[int, int, int] = ACCENT_COLOR,
    dim_text_color: tuple[int, int, int] = DIM_TEXT_COLOR,
    panel_color: tuple[int, int, int] = (11, 18, 32),
    panel_alpha: int = 150,
    chord_now_size: int = 64,
    chord_next_size: int = 32,
    show_chord_timeline: bool = True,
    show_key_bpm: bool = True,
    timeline_window_sec: float = 12.0,
    chord_legend_labels: list[str] | None = None,
    show_chord_legend: bool = True,
    chord_legend_scale: float = 1.0,
    chord_diagram_panel_alpha: int = 235,
    countdown_beats: int = 4,
    min_hold_seconds: float = 2.0,
    image_transition_seconds: float = 0.25,
    support_overlay_text: str = "",
    support_overlay_scale: float = 1.0,
    support_overlay_lead_seconds: float = 20.0,
) -> None:
    image_cache: dict[str, Image.Image] = {}
    audio_clip = AudioFileClip(str(audio_path))
    duration = audio_clip.duration

    # Built once, up front, from the real chord/lyric data -- not recomputed
    # per frame -- so every image swap point (line change or hold-respecting
    # instrumental chord block) and the crossfade around it are consistent
    # across the whole render. See build_image_timeline's own docstring.
    image_timeline = build_image_timeline(lines, chord_track, duration, min_hold_seconds)

    # A real band's count-in is always N beats, not N seconds -- how long
    # that actually takes depends on the song's own tempo (owner request,
    # 2026-09-10: "should count down 4, and be in tempo with the song").
    bpm = chord_track.bpm if chord_track.bpm and chord_track.bpm > 0 else DEFAULT_COUNTDOWN_BPM
    beat_duration = 60.0 / bpm
    countdown_duration = countdown_beats * beat_duration

    def get_image(key: str) -> Image.Image:
        if key not in image_cache:
            path = image_dir / f"{key}.png"
            if path.exists():
                image_cache[key] = Image.open(path).convert("RGB")
            else:
                image_cache[key] = Image.new("RGB", frame_size, fallback_color)
        return image_cache[key]

    def make_frame(T: float):
        # T is the OUTER video's own timeline, which runs countdown_duration
        # longer than the song itself -- song_t < 0 means we're still in the
        # lead-in, frozen on the real first moment's own background
        # (progress=0.0, i.e. the Ken Burns pan's own starting position, so
        # there's no visual jump the instant the real content begins right
        # after), guaranteed to be a real generated image, never a flat
        # placeholder color.
        song_t = T - countdown_duration
        if song_t < 0:
            scene = build_scene(
                lines, 0.0, chord_track=chord_track, audio_duration=duration, image_timeline=image_timeline,
            )
            countdown_key = _first_available_image_key(image_dir, scene.image_key)
            if countdown_key is not None:
                start_x, start_y, end_x, end_y, zoom_start, zoom_end = ken_burns_preset_for_key(countdown_key)
                bg = apply_ken_burns(
                    get_image(countdown_key), 0.0,
                    start_x, start_y, end_x, end_y, zoom_start, zoom_end,
                    frame_size=frame_size,
                )
            else:
                bg = Image.new("RGB", frame_size, fallback_color)
            beat_index = min(countdown_beats - 1, int(T / beat_duration))
            beats_remaining = countdown_beats - beat_index
            frame = draw_countdown(bg, beats_remaining, font_path, frame_size=frame_size, accent_color=accent_color)
            return np.array(frame)

        scene = build_scene(
            lines, song_t, chord_track=chord_track, audio_duration=duration,
            image_timeline=image_timeline, image_transition_seconds=image_transition_seconds,
        )
        start_x, start_y, end_x, end_y, zoom_start, zoom_end = ken_burns_preset_for_key(scene.image_key)
        bg = apply_ken_burns(
            get_image(scene.image_key), scene.ken_burns_progress,
            start_x, start_y, end_x, end_y, zoom_start, zoom_end,
            frame_size=frame_size,
        )
        if scene.prev_image_key is not None and scene.image_blend < 1.0:
            # The outgoing image is frozen at the END of its own Ken Burns pan
            # (progress=1.0) for its last moments on screen, rather than
            # continuing to animate a pan nobody will see finish.
            prev_x, prev_y, prev_end_x, prev_end_y, prev_zoom_start, prev_zoom_end = ken_burns_preset_for_key(
                scene.prev_image_key
            )
            prev_bg = apply_ken_burns(
                get_image(scene.prev_image_key), 1.0,
                prev_x, prev_y, prev_end_x, prev_end_y, prev_zoom_start, prev_zoom_end,
                frame_size=frame_size,
            )
            bg = crossfade_backgrounds(prev_bg, bg, scene.image_blend)
        frame = draw_scene(
            scene, bg, font_path, font_size=lyric_size, text_color=text_color, frame_size=frame_size,
        )
        frame = draw_chord_bar(
            frame, chord_track, song_t, font_path,
            frame_size=frame_size, accent_color=accent_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=panel_alpha, chord_now_size=chord_now_size,
            chord_next_size=chord_next_size, show_chord_timeline=show_chord_timeline,
            show_key_bpm=show_key_bpm, timeline_window_sec=timeline_window_sec,
        )
        current = current_chord_at(chord_track, song_t)
        frame = draw_chord_legend(
            frame, chord_legend_labels or [], current.label if current is not None else None, font_path,
            frame_size=frame_size, show_chord_legend=show_chord_legend, size_scale=chord_legend_scale,
            accent_color=accent_color, text_color=text_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=chord_diagram_panel_alpha,
        )
        # Owner request, 2026-09-11: only the last support_overlay_lead_seconds
        # before the song ends -- not the whole video, and never the
        # countdown/intro (that branch returns above and never reaches here).
        if song_t >= duration - support_overlay_lead_seconds:
            frame = draw_support_overlay(
                frame, support_overlay_text, font_path,
                frame_size=frame_size, accent_color=accent_color, panel_color=panel_color,
                scale=support_overlay_scale,
            )
        return np.array(frame)

    total_duration = duration + countdown_duration
    final_audio = (
        CompositeAudioClip([audio_clip.set_start(countdown_duration)]) if countdown_beats > 0 else audio_clip
    )
    video_clip = VideoClip(make_frame, duration=total_duration).set_audio(final_audio)
    video_clip.write_videofile(
        str(out_path), fps=fps, codec=encoder, audio_codec="aac",
        ffmpeg_params=["-crf", str(crf)],
    )
