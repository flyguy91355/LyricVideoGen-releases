from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

try:
    from moviepy.editor import AudioFileClip, VideoClip  # moviepy < 2.0
except ImportError:
    from moviepy import AudioFileClip, VideoClip  # moviepy >= 2.0 dropped .editor

from .layout import build_scene
from .models import InstrumentalChord, LyricLine
from .render import FRAME_SIZE, apply_ken_burns, draw_scene, ken_burns_preset_for_key

FPS = 24


def assemble_video(
    lines: list[LyricLine],
    image_dir: Path,
    audio_path: Path,
    out_path: Path,
    font_path: str,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
    instrumental_chords: list[InstrumentalChord] | None = None,
) -> None:
    image_cache: dict[str, Image.Image] = {}
    audio_clip = AudioFileClip(str(audio_path))
    duration = audio_clip.duration

    def get_image(key: str) -> Image.Image:
        if key not in image_cache:
            path = image_dir / f"{key}.png"
            if path.exists():
                image_cache[key] = Image.open(path).convert("RGB")
            else:
                image_cache[key] = Image.new("RGB", FRAME_SIZE, fallback_color)
        return image_cache[key]

    def make_frame(t: float):
        scene = build_scene(lines, t, instrumental_chords=instrumental_chords, audio_duration=duration)
        start_x, start_y, end_x, end_y, zoom_start, zoom_end = ken_burns_preset_for_key(scene.image_key)
        bg = apply_ken_burns(
            get_image(scene.image_key), scene.ken_burns_progress,
            start_x, start_y, end_x, end_y, zoom_start, zoom_end,
        )
        frame = draw_scene(scene, bg, font_path)
        return np.array(frame)

    video_clip = VideoClip(make_frame, duration=duration).set_audio(audio_clip)
    video_clip.write_videofile(str(out_path), fps=FPS, codec="libx264", audio_codec="aac")
