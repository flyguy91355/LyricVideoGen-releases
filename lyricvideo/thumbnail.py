"""A YouTube thumbnail made for each video (owner, 2026-10-04): one generated image of a GUITAR in a scene drawn from what
the song is about, with the song title and artist drawn big on top by code (an image model misspells words) and a red
PLAY ALONG VIDEOS tag (the channel's name). The whole title is always shown, in a bold condensed font so a long one stays large. Layout from the owner's research of what draws clicks: 1280x720, one subject, 3-5 big bold words with an
outline, high contrast (bright subject on a dark side), the bottom-right corner (YouTube's duration badge) kept clear.

Three candidate images are bought (about 0.3 cent each) and the one with the better brightness/contrast/colour score is kept;
a dark pick is lifted. The chosen picture is kept as thumbnail_bg.png, so an EASY CHORD version's thumbnail is made from
its song's picture at no cost. Never raises into the caller's job: pipeline.py and the upload wrap it."""

from __future__ import annotations

import colorsys
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps, ImageStat

from .chord_diagram import draw_single_chord_diagram
from .chord_shapes import get_chord_shape
from .imagery import generate_line_image

THUMB_SIZE = (1280, 720)
THUMBNAIL_FILE = "thumbnail.jpg"
THUMBNAIL_BG_FILE = "thumbnail_bg.png"
THUMBNAIL_SET_FILE = "thumbnail_set.json"      # {"video_id": ...}: this video's thumbnail is on YouTube
MAX_BYTES = 1_900_000                          # YouTube's own limit is far higher; the owner's research said stay under 2 MB
PROMPT_MODEL = "claude-haiku-4-5"
_HAIKU_IN, _HAIKU_OUT, _IMAGE_COST = 1.00e-6, 5.00e-6, 0.003      # flux-schnell $3/1000 (imagery.py)
# A bold CONDENSED face first (owner, 2026-10-04: never shorten a title -- a narrow font lets a long one stay big), then plain
# bold ones as fallbacks for a machine without it.
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/urw-base35/NimbusSansNarrow-Bold.otf", "C:/Windows/Fonts/ARIALNB.TTF",
    "/Library/Fonts/Arial Narrow Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf", "/Library/Fonts/Arial Bold.ttf",
)
_TAG_RED = (204, 0, 0)
_ARTIST_YELLOW = (255, 208, 0)


@dataclass(frozen=True)
class ThumbnailResult:
    path: Path
    cost_usd: float
    score: float = 0.0


def _bold_font(size: int, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    for candidate in ([font_path] if font_path else []) + list(_FONT_CANDIDATES):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def background_score(image: Image.Image) -> float:
    """Higher = a better thumbnail picture: bright on the right (where the subject sits), contrast, colour. 0..~1."""
    small = image.convert("RGB").resize((160, 90))
    right = small.crop((60, 0, 160, 90))
    luma = ImageStat.Stat(right.convert("L"))
    brightness = min(luma.mean[0] / 140.0, 1.0)                  # a mean of 140/255 is plenty bright
    contrast = min(luma.stddev[0] / 70.0, 1.0)
    pixels = list(right.getdata())[::7]
    saturation = sum(colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[1] for r, g, b in pixels) / max(1, len(pixels))
    return 0.45 * brightness + 0.35 * contrast + 0.20 * min(saturation / 0.6, 1.0)


def subject_centre(image: Image.Image) -> tuple[float, float]:
    """(x, y) as shares of the picture of where its subject is: the centroid of its brightest, most colourful pixels (a
    lit guitar in a dark scene). The middle when nothing stands out."""
    small = image.convert("RGB").resize((96, 54))
    scores = []
    for y in range(54):
        for x in range(96):
            r, g, b = small.getpixel((x, y))
            _h, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            scores.append((val * (0.4 + sat), x, y))
    scores.sort(reverse=True)
    top = scores[: max(1, len(scores) // 12)]
    total = sum(w for w, _x, _y in top) or 1.0
    return (sum(w * x for w, x, _y in top) / total / 96, sum(w * y for w, _x, y in top) / total / 54)


def reframe_subject_right(image: Image.Image, zoom: float = 1.3, target_x: float = 0.72, target_y: float = 0.45) -> Image.Image:
    """Zooms in and shifts the crop so the picture's subject lands about `target_x` across (the right of the thumbnail, clear
    of the title) and a little above the middle; a subject on the left is mirrored over first. One already there is only zoomed."""
    img = image.convert("RGB")
    w, h = img.size
    cx, cy = subject_centre(img)
    if cx < 0.4:                       # on the left: a crop cannot move it right, so mirror the picture (it holds no text)
        img, cx = ImageOps.mirror(img), 1.0 - cx
    cw, ch = w / zoom, h / zoom
    left = min(max(cx * w - target_x * cw, 0), w - cw)
    top = min(max(cy * h - target_y * ch, 0), h - ch)
    return img.crop((int(left), int(top), int(left + cw), int(top + ch)))


def lift_if_dark(image: Image.Image) -> Image.Image:
    """A pick whose right side averages dark is brightened (up to 1.8x) with a touch of contrast and colour."""
    right = image.convert("RGB").crop((image.width // 2, 0, image.width, image.height))
    mean = ImageStat.Stat(right.convert("L")).mean[0]
    if mean >= 100:
        return image.convert("RGB")
    out = ImageEnhance.Brightness(image.convert("RGB")).enhance(min(1.8, 105.0 / max(mean, 30.0)))
    return ImageEnhance.Color(ImageEnhance.Contrast(out).enhance(1.1)).enhance(1.15)


def draw_chord_panel(canvas: Image.Image, labels: list[str], font_path: str | None) -> Image.Image:
    """Every chord of the song as a fingering diagram, in the top-right (owner, 2026-10-04: all of them), sized to fit: one
    row up to 4 chords, two up to 8, more beyond. Drawn BEFORE the title, so a long title is in front of them."""
    shapes = [(label, get_chord_shape(label)) for label in labels]
    shapes = [(label, shape) for label, shape in shapes if shape is not None]
    if not shapes:
        return canvas
    n = len(shapes)
    cols = n if n <= 4 else (4 if n <= 8 else 5)
    rows = -(-n // cols)
    gap, region_w, region_h = 10, 600, 330
    cw = min(160, (region_w - (cols - 1) * gap) // cols)
    ch = int(cw * 1.28)
    while rows * ch + (rows - 1) * gap > region_h and cw > 40:
        cw -= 4
        ch = int(cw * 1.28)
    x0 = THUMB_SIZE[0] - 40 - (cols * cw + (cols - 1) * gap)
    out = canvas.convert("RGBA")
    font = font_path or next((f for f in _FONT_CANDIDATES if Path(f).exists()), "")
    for i, (label, shape) in enumerate(shapes):
        r, c = divmod(i, cols)
        out.alpha_composite(draw_single_chord_diagram(shape, label, (cw, ch), font, panel_alpha=235), (x0 + c * (cw + gap), 36 + r * (ch + gap)))
    return out.convert("RGB")


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + [cur] if cur else lines


def _fit_title(draw, text: str, max_width: int, max_height: int, font_path: str | None):
    """The largest bold size (<= 210 px) at which the title fits in max_width x max_height on at most 3 lines."""
    for size in range(210, 59, -6):
        font = _bold_font(size, font_path)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= 3 and len(lines) * size * 1.02 <= max_height and all(draw.textlength(l, font=font) <= max_width for l in lines):
            return font, lines, size
    font = _bold_font(60, font_path)
    return font, _wrap(draw, text, font, max_width), 60


def compose_thumbnail(
    background: Image.Image, title: str, artist: str, out_path: Path, *, tag: str = "PLAY ALONG VIDEOS", font_path: str | None = None,
    chord_labels: list[str] | None = None,
) -> Path:
    """1280x720 JPEG under MAX_BYTES: the picture, a dark gradient on the left for the text, the title big in white with a
    thick outline, the artist in yellow, the red tag top-left. Nothing is drawn in the bottom-right corner."""
    has_chords = bool(chord_labels) and any(get_chord_shape(label) is not None for label in chord_labels)
    # with the chord panel up there the subject goes lower-right, under it
    bg = lift_if_dark(reframe_subject_right(background, target_y=0.66 if has_chords else 0.45)).resize(THUMB_SIZE)
    shade = Image.new("L", THUMB_SIZE)
    sd = ImageDraw.Draw(shade)
    for x in range(THUMB_SIZE[0]):
        sd.line([(x, 0), (x, THUMB_SIZE[1])], fill=int(190 * max(0.0, 1 - x / 840)))
    bg = Image.composite(Image.new("RGB", THUMB_SIZE, (5, 8, 16)), bg, shade)
    if has_chords:
        bg = draw_chord_panel(bg, list(chord_labels), font_path)        # before the title: the title is in front
    draw = ImageDraw.Draw(bg)

    text = re.sub(r"\s+", " ", title or "").strip().upper()      # the whole title, never shortened
    font, lines, size = _fit_title(draw, text, 780, 390, font_path)
    y = 175
    for line in lines:
        draw.text((50, y), line, font=font, fill=(255, 255, 255), stroke_width=max(6, size // 18), stroke_fill=(0, 0, 0))
        y += int(size * 1.02)
    artist_text = (artist or "").upper().strip()
    if artist_text:
        afont = _bold_font(78, font_path)
        while draw.textlength(artist_text, font=afont) > 800 and afont.size > 40:
            afont = _bold_font(afont.size - 4, font_path)
        draw.text((54, y + 14), artist_text, font=afont, fill=_ARTIST_YELLOW, stroke_width=5, stroke_fill=(0, 0, 0))

    tag_font = _bold_font(54, font_path)
    width = draw.textlength(tag, font=tag_font)
    draw.rounded_rectangle([50, 40, 50 + width + 48, 124], radius=14, fill=_TAG_RED)
    draw.text((74, 82), tag, font=tag_font, fill=(255, 255, 255), anchor="lm")

    out_path = Path(out_path)
    for quality in (92, 86, 80, 74, 68):
        with tempfile.NamedTemporaryFile(suffix=".jpg", dir=out_path.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        bg.save(tmp_path, "JPEG", quality=quality, optimize=True)
        if tmp_path.stat().st_size <= MAX_BYTES or quality == 68:
            tmp_path.replace(out_path)
            return out_path
        tmp_path.unlink(missing_ok=True)
    return out_path


def build_prompt_request(title: str, artist: str, lyrics: str) -> str:
    return (
        f'Song: "{title}" by {artist}. Lyrics:\n{lyrics[:3000]}\n\n'
        "Write ONE image-generation prompt (max 60 words) for a YouTube thumbnail background for a guitar play-along video of this song. "
        "The main subject is ONE GUITAR (acoustic or electric, whichever suits the song), large, dramatic and sharply lit, shown in a "
        "scene drawn from what the song is about (its setting, imagery, mood). Place the guitar on the RIGHT half, upper to middle of the "
        "frame, well away from the bottom edge and bottom corners; the left side darker and simple. "
        "Bright, vivid, high contrast, a glowing well-lit subject against a darker background, cinematic lighting. "
        "No people or faces, no hands, no piano or other instruments, no text, letters or logos. Reply with ONLY the prompt."
    )


def write_background_prompt(anthropic_client, title: str, artist: str, lyrics: str) -> tuple[str, float]:
    """(the image prompt, its cost). Raises if Claude's reply has no text."""
    response = anthropic_client.messages.create(
        model=PROMPT_MODEL, max_tokens=300, messages=[{"role": "user", "content": build_prompt_request(title, artist, lyrics)}],
    )
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()
    if not text:
        raise RuntimeError("the thumbnail prompt came back empty")
    usage = getattr(response, "usage", None)
    cost = getattr(usage, "input_tokens", 0) * _HAIKU_IN + getattr(usage, "output_tokens", 0) * _HAIKU_OUT
    return text, cost


def generate_thumbnail(
    work_dir: Path, anthropic_client, replicate_token: str, *, title: str, artist: str, lyrics: str,
    candidates: int = 3, http_client=httpx, font_path: str | None = None, chord_labels: list[str] | None = None,
) -> ThumbnailResult:
    """Buys `candidates` pictures from one prompt, keeps the best-scoring as thumbnail_bg.png, composes thumbnail.jpg."""
    work_dir = Path(work_dir)
    prompt, cost = write_background_prompt(anthropic_client, title, artist, lyrics)
    best: tuple[float, Path] | None = None
    for n in range(max(1, candidates)):
        candidate = work_dir / f"thumbnail_candidate_{n}.png"
        try:
            generate_line_image(replicate_token, prompt, candidate, http_client=http_client)
        except Exception:
            if best is None and n == max(1, candidates) - 1:
                raise
            continue
        cost += _IMAGE_COST
        with Image.open(candidate) as img:
            score = background_score(img)
        if best is None or score > best[0]:
            best = (score, candidate)
    if best is None:
        raise RuntimeError("no thumbnail picture could be generated")
    with Image.open(best[1]) as img:
        data = img.convert("RGB")
        data.load()
    png = work_dir / THUMBNAIL_BG_FILE
    tmp = work_dir / (THUMBNAIL_BG_FILE + ".tmp.png")
    data.save(tmp, "PNG")
    tmp.replace(png)
    for n in range(max(1, candidates)):
        (work_dir / f"thumbnail_candidate_{n}.png").unlink(missing_ok=True)
    path = compose_thumbnail(data, title, artist, work_dir / THUMBNAIL_FILE, font_path=font_path, chord_labels=chord_labels)
    return ThumbnailResult(path, cost, best[0])


def compose_from_saved_background(
    work_dir: Path, source_dir: Path, *, title: str, artist: str, tag: str, font_path: str | None = None,
    chord_labels: list[str] | None = None,
) -> Path | None:
    """A thumbnail for `work_dir` from the picture `source_dir` already chose (an EASY CHORD version uses its song's, with
    its own tag); None when `source_dir` has none."""
    background = Path(source_dir) / THUMBNAIL_BG_FILE
    if not background.exists():
        return None
    with Image.open(background) as img:
        data = img.convert("RGB")
        data.load()
    return compose_thumbnail(data, title, artist, Path(work_dir) / THUMBNAIL_FILE, tag=tag, font_path=font_path, chord_labels=chord_labels)
