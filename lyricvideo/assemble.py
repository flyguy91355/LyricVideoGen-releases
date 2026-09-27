from __future__ import annotations

import bisect
import logging
import os
import re
import subprocess
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

from .chord_diagram import clear_overlay_caches as _clear_diagram_overlay_caches
from .chord_diagram import draw_capo_badge, draw_chord_legend
from .layout import ImageSegment, build_image_timeline, build_scene
from .models import ChordTrack, LyricLine, current_chord_at
from .render import (
    ACCENT_COLOR, DIM_TEXT_COLOR, FRAME_SIZE, apply_ken_burns, crossfade_backgrounds, draw_chord_bar, draw_countdown,
    draw_scene, draw_support_overlay, ken_burns_preset_for_key,
)
from .render import clear_overlay_caches as _clear_render_overlay_caches

log = logging.getLogger(__name__)

# moviepy is imported on first use (_load_moviepy), not at module import: pipeline.py imports this module and
# gui.py imports pipeline.py, so a top-level `import moviepy.editor` (~0.6 s on a fast desktop, several times that
# on the owner's render box) was paid on every GUI launch before a single window drew (issue #7). Tests replace
# these three names with fakes; _load_moviepy only fills in a name that is still None.
AudioFileClip = None
CompositeAudioClip = None
VideoClip = None

FPS = 24
DEFAULT_COUNTDOWN_BPM = 120.0  # used only if a song's own BPM wasn't detected (0 or missing)

# Decoded, frame-sized backgrounds kept at once. A frame needs at most the current image, the outgoing one during a
# crossfade and the countdown's; access follows the timeline, so a small LRU never re-decodes in practice, while
# the old unbounded dict held every image of the song (~8 MB each at 1080p) until the render finished.
_IMAGE_CACHE_SIZE = 4

# A render goes to a partial name first and only becomes `out_path` once it is complete and checked (see
# _partial_render_paths / _check_rendered_video). Writing straight to `out_path` left a PLAYABLE mp4 with a short
# picture and the full audio whenever a render died -- the cut-short EASY CHORD videos of issue #7.
_PARTIAL_SUFFIX = ".rendering"
_TEMP_AUDIO_SUFFIX = ".rendering-audio.m4a"
_MUXER_FOR_SUFFIX = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska"}
# How much shorter than countdown + song the finished picture may be before it is rejected. A real render writes
# every frame (the check only catches a writer that stopped early without raising), so a second is generous.
_MAX_PICTURE_SHORTFALL_SECONDS = 1.0


def _load_moviepy() -> None:
    global AudioFileClip, CompositeAudioClip, VideoClip
    if AudioFileClip is not None and CompositeAudioClip is not None and VideoClip is not None:
        return
    try:
        from moviepy.editor import AudioFileClip as audio_cls, CompositeAudioClip as mix_cls, VideoClip as video_cls
    except ImportError:  # moviepy >= 2.0 dropped .editor (requirements.txt pins < 2.0)
        from moviepy import AudioFileClip as audio_cls, CompositeAudioClip as mix_cls, VideoClip as video_cls
    if AudioFileClip is None:
        AudioFileClip = audio_cls
    if CompositeAudioClip is None:
        CompositeAudioClip = mix_cls
    if VideoClip is None:
        VideoClip = video_cls


def _partial_render_paths(out_path: Path) -> tuple[Path, Path, list[str]]:
    """(partial video path, temp audio path, extra ffmpeg output args) for a render of `out_path`. Both temp files
    sit next to `out_path` -- never in the process's CWD, where moviepy put its `<stem>TEMP_MPY_wvf_snd.mp4` (the
    repo root when launched normally) -- and are named after the whole output file name, so two different outputs
    never share one. The partial video's name deliberately does NOT end in .mp4 (a `glob("*.mp4")` elsewhere must
    never mistake an unfinished render for a video), so ffmpeg is told the container explicitly."""
    out_path = Path(out_path)
    temp_audio = out_path.with_name(out_path.name + _TEMP_AUDIO_SUFFIX)
    muxer = _MUXER_FOR_SUFFIX.get(out_path.suffix.lower())
    if muxer is not None:
        return out_path.with_name(out_path.name + _PARTIAL_SUFFIX), temp_audio, ["-f", muxer]
    return out_path.with_name(f"{out_path.stem}{_PARTIAL_SUFFIX}{out_path.suffix}"), temp_audio, []


def _encoder_params(encoder: str, crf: int) -> list[str]:
    params = ["-crf", str(crf)]
    if encoder != "libx264":
        # moviepy adds `-pix_fmt yuv420p` for libx264 only; any other encoder was handed the raw rgb24 frames
        # as-is, so libx265 wrote 4:4:4 RGB HEVC (gbrp, Range Extensions) that most players can't decode.
        params += ["-pix_fmt", "yuv420p"]
    if encoder == "libx265":
        params += ["-tag:v", "hvc1"]  # ffmpeg's default hev1 tag is refused by QuickTime/Safari and many devices
    return params


def _ffmpeg_binary() -> str:
    """The ffmpeg moviepy itself renders with (imageio-ffmpeg's bundled binary unless overridden)."""
    try:
        from moviepy.config import get_setting
    except ImportError:  # moviepy >= 2.0
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    return get_setting("FFMPEG_BINARY")


def _run_ffmpeg_null(path: Path, stream: str) -> tuple[int, str]:
    """Stream-copies one stream of `path` to ffmpeg's null muxer (no decoding, about a second for a whole song);
    returns (exit code, ffmpeg's report)."""
    cmd = [
        _ffmpeg_binary(), "-hide_banner", "-nostdin", "-i", str(path),
        "-map", stream, "-c", "copy", "-f", "null", "-",
    ]
    popen_kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW, as moviepy does
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600, **popen_kwargs)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def _rendered_video_frame_count(path: Path) -> int:
    """Number of video frames actually stored in `path`. A container's own Duration can't be trusted here: a
    truncated render reports the full AUDIO length there."""
    return _frame_count_from_report(path, *_run_ffmpeg_null(path, "0:v:0"))


def _frame_count_from_report(path: Path, returncode: int, output: str) -> int:
    counts = re.findall(r"frame=\s*(\d+)", output)
    if returncode == 0 and counts:
        return int(counts[-1])
    # Some ffmpeg builds (confirmed live, 2026-09-27, on 6.1.1-3ubuntu5: a stream-copy run to the null muxer)
    # never print frame= at all, only size=/time=/bitrate=/speed= -- the exact-count path above then finds
    # nothing and used to treat a real, complete render (a whole 2:32 1080p24 video) as unreadable. Recover the
    # count from the reported duration times the video's own frame rate, both already printed in the same
    # report text -- never a silent loss of precision when frame= IS present, since that path returns above.
    if returncode == 0:
        fps_match = re.search(r"Video:.*?(\d+(?:\.\d+)?) fps", output)
        times = re.findall(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        if fps_match and float(fps_match.group(1)) > 0 and times:
            hours, minutes, seconds = times[-1]
            seconds_total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            return round(seconds_total * float(fps_match.group(1)))
    raise RuntimeError(
        f"Could not read back the rendered video {Path(path).name} to check its length "
        f"(ffmpeg exit code {returncode}): {output.strip()[-400:]}"
    )


def rendered_stream_seconds(path: Path) -> tuple[float, float | None]:
    """(seconds of picture, seconds of audio -- None if there is no audio stream) actually stored in a video file,
    read by stream copy (no decoding). For spotting an already-damaged video, e.g. before an upload: a cut-short
    render has its picture far shorter than its audio, while the container's own Duration shows the full length.
    Raises RuntimeError if the file can't be read."""
    path = Path(path)
    returncode, video_report = _run_ffmpeg_null(path, "0:v:0")
    frames = _frame_count_from_report(path, returncode, video_report)
    fps_match = re.search(r"Video:.*?(\d+(?:\.\d+)?) fps", video_report)
    if fps_match is None or float(fps_match.group(1)) <= 0:
        raise RuntimeError(f"Could not read the frame rate of {path.name}")
    audio_returncode, audio_output = _run_ffmpeg_null(path, "0:a:0")
    times = re.findall(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)", audio_output)
    audio_seconds = None
    if audio_returncode == 0 and times:
        hours, minutes, seconds = times[-1]
        audio_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return frames / float(fps_match.group(1)), audio_seconds


def _check_rendered_video(path: Path, expected_seconds: float, fps: float) -> None:
    """Raises unless `path` holds (nearly) the whole countdown + song worth of picture."""
    picture_seconds = _rendered_video_frame_count(path) / fps
    if picture_seconds < expected_seconds - _MAX_PICTURE_SHORTFALL_SECONDS:
        raise RuntimeError(
            f"The rendered video is cut short: {picture_seconds:.1f} s of picture for {expected_seconds:.1f} s of "
            f"countdown + song. It was not kept."
        )


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Could not remove the leftover render file %s: %s", path, e)


def _segment_index_at(timeline: list[ImageSegment], starts: list[float], t: float) -> int | None:
    """Same lookup as layout's own (start <= t < end), by bisection over the sorted segment starts."""
    i = bisect.bisect_right(starts, t) - 1
    if 0 <= i < len(timeline) and timeline[i].start <= t < timeline[i].end:
        return i
    return None


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


class _BackgroundCache:
    """Frame-sized backgrounds by image key. Each key is resolved once to the image FILE it really shows (not a
    directory probe per frame), and decoded images are kept in a small LRU keyed by that file -- so every missing
    key shares one copy of its fallback instead of each holding its own frame-sized image, and a render holds a
    handful of backgrounds instead of all of them (~8 MB each at 1080p) until it ends."""

    def __init__(
        self, image_dir: Path, frame_size: tuple[int, int], fallback_color: tuple[int, int, int],
        max_images: int = _IMAGE_CACHE_SIZE, order: list[str] | None = None,
    ) -> None:
        self._image_dir = Path(image_dir)
        self._frame_size = tuple(frame_size)
        self._fallback_color = fallback_color
        self._max_images = max_images
        self._order = list(dict.fromkeys(order or []))      # the song's image keys in timeline order (for substitutes)
        self._resolved: dict[str, str | None] = {}
        self._images: OrderedDict[str | None, Image.Image] = OrderedDict()
        self._unreadable: set[str] = set()

    def __len__(self) -> int:
        return len(self._images)

    def _decode(self, real_key: str) -> Image.Image | None:
        """The frame-sized picture for real_key's file, or None (after one warning) when the file cannot be decoded -- a
        picture cut short or damaged on disk (issue #7 review: a `--stage render` over such a file died mid-render)."""
        path = self._image_dir / f"{real_key}.png"
        try:
            # Cached already scaled to the output frame: apply_ken_burns
            # starts from a frame-sized image, and re-scaling the raw
            # (differently-sized) generation on EVERY frame was a full
            # extra resample per frame for the same pixels each time.
            with Image.open(path) as source:
                return source.convert("RGB").resize(self._frame_size)
        except Exception as e:
            self._unreadable.add(real_key)
            log.warning("Background image %s could not be read (%s: %s); using another of the song's images instead.",
                        path.name, type(e).__name__, e)
            return None

    def _readable_substitute(self, bad_key: str) -> tuple[str | None, Image.Image | None]:
        """The nearest picture to `bad_key` in the song's own image order (the previous one first) that decodes, else any
        picture of this song that does (the any-real-image rule a missing key follows); (None, None) when none does."""
        candidates: list[str] = []
        if bad_key in self._order:
            i = self._order.index(bad_key)
            for step in range(1, len(self._order)):
                candidates += [self._order[j] for j in (i - step, i + step) if 0 <= j < len(self._order)]
        candidates += [path.stem for path in sorted(self._image_dir.glob("*.png"))]
        for candidate in dict.fromkeys(candidates):
            if candidate in self._unreadable or not (self._image_dir / f"{candidate}.png").exists():
                continue
            image = self._images.get(candidate)
            if image is None:
                image = self._decode(candidate)
            if image is not None:
                return candidate, image
        return None, None

    def _nearest_existing_key(self, key: str) -> str | None:
        """`key` when its file exists; else the chronologically nearest key in the song's image order whose file exists
        (the previous one first) -- the "nearest real image" rule -- else any picture of the song
        (_first_available_image_key); None when the song has none."""
        if (self._image_dir / f"{key}.png").exists() or key not in self._order:
            return _first_available_image_key(self._image_dir, key)
        i = self._order.index(key)
        for step in range(1, len(self._order)):
            for j in (i - step, i + step):
                if 0 <= j < len(self._order) and (self._image_dir / f"{self._order[j]}.png").exists():
                    return self._order[j]
        return _first_available_image_key(self._image_dir, key)

    def get(self, key: str) -> Image.Image:
        if key not in self._resolved:
            # A key with no file behind it (an instrumental caption the images
            # stage didn't generate, or a stale cache dir) must never render as
            # a flat placeholder color while ANY real image exists for this
            # song -- the same rule the countdown already follows, applied to
            # every frame (real owner complaint class, "blank screen").
            self._resolved[key] = self._nearest_existing_key(key)
        real_key = self._resolved[key]
        image = self._images.get(real_key)
        if image is not None:
            self._images.move_to_end(real_key)
            return image
        path = self._image_dir / f"{real_key}.png" if real_key is not None else None
        if path is not None and path.exists():
            image = None if real_key in self._unreadable else self._decode(real_key)
            if image is None:
                # Every key that showed this file shows a readable picture from now on (decoded and warned about once).
                bad_key = real_key
                real_key, image = self._readable_substitute(bad_key)
                for other, shown in self._resolved.items():
                    if shown == bad_key:
                        self._resolved[other] = real_key
        if image is None:
            image = Image.new("RGB", self._frame_size, self._fallback_color)
        self._images[real_key] = image
        self._images.move_to_end(real_key)
        while len(self._images) > self._max_images:
            self._images.popitem(last=False)
        return image


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
    lyric_preview_lead_seconds: float = 3.0,
    support_overlay_text: str = "",
    support_overlay_scale: float = 1.0,
    support_overlay_lead_seconds: float = 20.0,
    capo: int | None = None,
    key_label: str | None = None,
) -> None:
    out_path = Path(out_path)
    _load_moviepy()
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

    get_image = _BackgroundCache(
        image_dir, frame_size, fallback_color, order=[segment.image_key for segment in image_timeline],
    ).get

    # Where the outgoing image's Ken Burns pan really was at the moment it stopped being the main image, per
    # timeline segment index (see outgoing_ken_burns_progress).
    segment_starts = [segment.start for segment in image_timeline]
    outgoing_progress_cache: dict[int, float] = {}

    def outgoing_ken_burns_progress(song_t: float) -> float:
        """The progress to freeze the OUTGOING image at during a crossfade: wherever its own pan had reached on its
        last moment as the main image -- so the first crossfade frame (100% outgoing) continues the previous frame
        instead of jumping. A sung line's pan is paced to the NEXT line's start, but its segment ends at the line's
        plausible end whenever an instrumental break follows, so it is typically only 10-40% through; the old
        fixed 1.0 snapped zoom/pan by up to 15% / ~160 px in one frame at every verse-to-break handoff."""
        seg_idx = _segment_index_at(image_timeline, segment_starts, song_t)
        if seg_idx is None or seg_idx == 0:
            return 1.0
        if seg_idx not in outgoing_progress_cache:
            prev_segment = image_timeline[seg_idx - 1]
            last_moment = max(prev_segment.start, prev_segment.end - 1e-6)
            before = build_scene(
                lines, last_moment, chord_track=chord_track, audio_duration=duration,
                image_timeline=image_timeline, image_transition_seconds=image_transition_seconds,
                lyric_preview_lead_seconds=lyric_preview_lead_seconds,
            )
            # Rounded so an image whose pan really did finish (an instrumental block, paced to its own span) is
            # frozen at exactly 1.0 as before, not at 0.9999995 -- a sub-pixel difference that still re-rounds
            # the zoomed size by a pixel.
            outgoing_progress_cache[seg_idx] = (
                round(before.ken_burns_progress, 4) if before.image_key == prev_segment.image_key else 1.0
            )
        return outgoing_progress_cache[seg_idx]

    # The countdown's background is the same for every lead-in frame; resolve
    # it once (a scene build plus a directory scan) instead of per frame.
    countdown_key_cache: dict[str, str | None] = {}

    def countdown_image_key() -> str | None:
        if "key" not in countdown_key_cache:
            scene = build_scene(
                lines, 0.0, chord_track=chord_track, audio_duration=duration, image_timeline=image_timeline,
            )
            countdown_key_cache["key"] = _first_available_image_key(image_dir, scene.image_key)
        return countdown_key_cache["key"]

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
            countdown_key = countdown_image_key()
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
            lyric_preview_lead_seconds=lyric_preview_lead_seconds,
        )
        start_x, start_y, end_x, end_y, zoom_start, zoom_end = ken_burns_preset_for_key(scene.image_key)
        bg = apply_ken_burns(
            get_image(scene.image_key), scene.ken_burns_progress,
            start_x, start_y, end_x, end_y, zoom_start, zoom_end,
            frame_size=frame_size,
        )
        if scene.prev_image_key is not None and scene.image_blend < 1.0:
            # The outgoing image is frozen where its own Ken Burns pan stood on
            # its last moment as the main image (outgoing_ken_burns_progress),
            # rather than continuing to animate a pan nobody will see finish.
            prev_x, prev_y, prev_end_x, prev_end_y, prev_zoom_start, prev_zoom_end = ken_burns_preset_for_key(
                scene.prev_image_key
            )
            prev_bg = apply_ken_burns(
                get_image(scene.prev_image_key), outgoing_ken_burns_progress(song_t),
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
            show_key_bpm=show_key_bpm, timeline_window_sec=timeline_window_sec, key_label=key_label,
        )
        current = current_chord_at(chord_track, song_t)
        frame = draw_chord_legend(
            frame, chord_legend_labels or [], current.label if current is not None else None, font_path,
            frame_size=frame_size, show_chord_legend=show_chord_legend, size_scale=chord_legend_scale,
            accent_color=accent_color, text_color=text_color, dim_text_color=dim_text_color,
            panel_color=panel_color, panel_alpha=chord_diagram_panel_alpha,
        )
        frame = draw_capo_badge(
            frame, capo, font_path, frame_size=frame_size, accent_color=accent_color, text_color=text_color,
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
    partial_path, temp_audio_path, container_params = _partial_render_paths(out_path)
    try:
        final_audio = (
            CompositeAudioClip([audio_clip.set_start(countdown_duration)]) if countdown_beats > 0 else audio_clip
        )
        video_clip = VideoClip(make_frame, duration=total_duration).set_audio(final_audio)
        # moviepy writes the full-length audio first and then pipes frames into an ffmpeg that muxes that audio
        # in: a render that raises or is killed partway still leaves a playable file with a short picture and the
        # whole song's sound. So the render goes to a partial name, is read back and length-checked, and only
        # then replaces `out_path` in one step -- `out_path` either holds a complete video or is untouched (a
        # failed Redo/EASY re-render keeps the previous good video instead of truncating it up front).
        video_clip.write_videofile(
            str(partial_path), fps=fps, codec=encoder, audio_codec="aac",
            ffmpeg_params=_encoder_params(encoder, crf) + container_params,
            temp_audiofile=str(temp_audio_path),
        )
        _check_rendered_video(partial_path, total_duration, fps)
        os.replace(partial_path, out_path)
    finally:
        # AudioFileClip holds an ffmpeg reader subprocess with the source file
        # open; moviepy never closes it on its own (its Clip.close docstring
        # says so explicitly), so every render leaked one until interpreter
        # exit -- a whole batch run's worth of zombie ffmpeg processes, and on
        # Windows a lock on each audio file for the rest of the session.
        audio_clip.close()
        # Nothing of a failed render survives: no partial video, and no temp audio (moviepy only deletes its temp
        # audio after a SUCCESSFUL write).
        _remove_quietly(partial_path)
        _remove_quietly(temp_audio_path)
        # The per-state legend/badge/panel patches are only worth keeping for the frames of one render.
        _clear_diagram_overlay_caches()
        _clear_render_overlay_caches()
