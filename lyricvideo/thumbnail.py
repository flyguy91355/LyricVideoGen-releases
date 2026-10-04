"""A YouTube thumbnail made for each video (owner, 2026-10-04): one generated image of a GUITAR in a scene drawn from what
the song is about, with the song title and artist drawn big on top by code (an image model misspells words) and a red
PLAY ALONG VIDEOS tag (the channel's name). The whole title is always shown, in a bold condensed font so a long one stays large. Layout from the owner's research of what draws clicks: 1280x720, one subject, 3-5 big bold words with an
outline, high contrast (bright subject on a dark side), the bottom-right corner (YouTube's duration badge) kept clear.

Three candidate images are bought (about 0.3 cent each) and the one with the better brightness/contrast/colour score is kept;
a dark pick is lifted. The chosen picture is kept as thumbnail_bg.png, so an EASY CHORD version's thumbnail is made from
its song's picture at no cost. Never raises into the caller's job: pipeline.py and the upload wrap it."""

from __future__ import annotations

import base64
import colorsys
import io
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps, ImageStat

from .chord_diagram import draw_single_chord_diagram
from .chord_shapes import get_chord_shape
from .imagery import generate_line_image, is_fallback_image

THUMB_SIZE = (1280, 720)
THUMBNAIL_FILE = "thumbnail.jpg"
THUMBNAIL_BG_FILE = "thumbnail_bg.png"
THUMBNAIL_SET_FILE = "thumbnail_set.json"      # {"video_id": ...}: this video's thumbnail is on YouTube
MAX_BYTES = 1_900_000                          # YouTube's own limit is far higher; the owner's research said stay under 2 MB
# The prompt-writing step needs real interpretation of a song's meaning (owner, 2026-10-04: the Haiku version gave Bed of Roses two
# guitars in a room); Sonnet is $2/$10 per MTok, about half a cent per song. Thinking is off (a one-prompt answer).
PROMPT_MODEL = "claude-sonnet-5"
_HAIKU_IN, _HAIKU_OUT, _IMAGE_COST = 2.00e-6, 10.00e-6, 0.003      # Sonnet prices (name kept); flux-schnell $3/1000 (imagery.py)
# A bold CONDENSED face first (owner, 2026-10-04: never shorten a title -- a narrow font lets a long one stay big), then plain
# bold ones as fallbacks for a machine without it.
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/urw-base35/NimbusSansNarrow-Bold.otf", "C:/Windows/Fonts/ARIALNB.TTF",
    "/Library/Fonts/Arial Narrow Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf", "/Library/Fonts/Arial Bold.ttf",
)
_TAG_RED = (204, 0, 0)
_SUB_TAG_GREEN = (24, 140, 70)       # the EASY CHORDS badge under the red channel tag
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


GRID_MAX = 12                    # how many of a song's own pictures Claude is shown (the best-scoring ones)
_PICK_RE = re.compile(r"PICK:\s*(\d+)", re.IGNORECASE)


def _usable_song_images(images_dir: Path) -> list[Path]:
    """The song's own generated pictures that are real (decodable, not the solid-colour placeholder)."""
    found = []
    for path in sorted(Path(images_dir).glob("*")):
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            with Image.open(path) as img:
                img.load()
                if img.size[0] >= 640 and not is_fallback_image(path):
                    found.append(path)
        except Exception:
            continue
    return found


def pick_song_image(anthropic_client, title: str, artist: str, lyrics: str, images_dir: Path) -> tuple[Path | None, float]:
    """(the song's own picture that best shows what the song is about, the cost). The pictures were made from this song's lyrics
    and are already paid for (owner, 2026-10-04: "we have a bird in the images for the song already"). The brightest, most
    contrasty GRID_MAX are laid out in one numbered grid and Claude picks the one that shows the song's central image best, bright
    and clear. Falls back to the best-scoring one if Claude's answer cannot be read. A song with ONE usable picture just uses it (no
    call, no cost); (None, 0) only when it has none -- the caller then generates one."""
    paths = _usable_song_images(images_dir)
    if not paths:
        return None, 0.0
    if len(paths) == 1:
        return paths[0], 0.0
    scored = []
    for path in paths:
        with Image.open(path) as img:
            scored.append((background_score(img), path))
    scored.sort(key=lambda x: -x[0])
    shortlist = [p for _s, p in scored[:GRID_MAX]]
    cols, w, h = 4, 400, 225
    rows = -(-len(shortlist) // cols)
    sheet = Image.new("RGB", (cols * w, rows * h), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(shortlist):
        with Image.open(path) as img:
            sheet.paste(img.convert("RGB").resize((w, h)), ((i % cols) * w, (i // cols) * h))
        draw.rectangle([(i % cols) * w, (i // cols) * h, (i % cols) * w + 62, (i // cols) * h + 44], fill=(0, 0, 0))
        draw.text(((i % cols) * w + 8, (i // cols) * h + 4), str(i), font=_bold_font(36), fill=(255, 255, 0))
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=80)
    prompt = (
        f'These are pictures made for the song "{title}" by {artist}. Lyrics:\n{lyrics[:2500]}\n\n'
        "Pick the ONE picture that best shows what this song is about -- its central image or metaphor -- and would make someone who knows "
        "the song recognise it. It must be bright and clear, with ONE strong subject and a simpler or darker left side (the title goes there). "
        "Prefer a clearly visible subject over a dark or murky one. Reply with exactly: PICK: <number>"
    )
    try:
        response = anthropic_client.messages.create(
            model=PROMPT_MODEL, max_tokens=60, thinking={"type": "disabled"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode()}},
                {"type": "text", "text": prompt},
            ]}],
        )
    except Exception:
        return shortlist[0], 0.0
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
    usage = getattr(response, "usage", None)
    cost = getattr(usage, "input_tokens", 0) * _HAIKU_IN + getattr(usage, "output_tokens", 0) * _HAIKU_OUT
    match = _PICK_RE.search(text)
    index = int(match.group(1)) if match else 0
    return shortlist[index if 0 <= index < len(shortlist) else 0], cost


def lift_if_dark(image: Image.Image) -> Image.Image:
    """A pick whose right side averages dark is brightened (up to 1.8x) with a touch of contrast and colour."""
    right = image.convert("RGB").crop((image.width // 2, 0, image.width, image.height))
    mean = ImageStat.Stat(right.convert("L")).mean[0]
    if mean >= 100:
        return image.convert("RGB")
    out = ImageEnhance.Brightness(image.convert("RGB")).enhance(min(1.8, 105.0 / max(mean, 30.0)))
    return ImageEnhance.Color(ImageEnhance.Contrast(out).enhance(1.1)).enhance(1.15)


ONE_WORD_MAX_SIZE = 150         # a one-word title grows to fill the empty area (owner, 2026-10-04)


def layout_title(draw, text: str, max_width: int, max_height: int, font_path: str | None):
    """(font, lines, size). Long titles wrap and shrink to fit (_fit_title, <= TITLE_MAX_SIZE). A title that would sit on ONE
    short line instead fills the empty area: one word grows to the width (<= ONE_WORD_MAX_SIZE); several words are stacked on
    two lines at TITLE_MAX_SIZE, like "DEAD / FLOWERS"."""
    font, lines, size = _fit_title(draw, text, max_width, max_height, font_path)
    words = text.split()
    if len(lines) != 1 or not words:
        return font, lines, size
    if len(words) == 1:
        for big in range(ONE_WORD_MAX_SIZE, size - 1, -4):
            big_font = _bold_font(big, font_path)
            if draw.textlength(text, font=big_font) <= max_width and big * 1.02 <= max_height:
                return big_font, [text], big
        return font, lines, size
    best = min(range(1, len(words)), key=lambda k: max(len(" ".join(words[:k])), len(" ".join(words[k:]))))
    stacked = [" ".join(words[:best]), " ".join(words[best:])]
    stacked_font = _bold_font(TITLE_MAX_SIZE, font_path)
    if all(draw.textlength(l, font=stacked_font) <= max_width for l in stacked) and 2 * TITLE_MAX_SIZE * 1.02 <= max_height:
        return stacked_font, stacked, TITLE_MAX_SIZE
    return font, lines, size


def chord_panel_geometry(n: int) -> tuple[int, int, int, int, int, int]:
    """(cols, rows, cell width, cell height, gap, left edge) of the chord panel for n diagrams."""
    cols = n if n <= 4 else (4 if n <= 8 else 5)
    rows = -(-n // cols)
    gap, region_w, region_h = 10, 600, 330
    cw = min(160, (region_w - (cols - 1) * gap) // cols)
    ch = int(cw * 1.28)
    while rows * ch + (rows - 1) * gap > region_h and cw > 40:
        cw -= 4
        ch = int(cw * 1.28)
    return cols, rows, cw, ch, gap, THUMB_SIZE[0] - 40 - (cols * cw + (cols - 1) * gap)


def draw_chord_panel(canvas: Image.Image, labels: list[str], font_path: str | None) -> Image.Image:
    """Every chord of the song as a fingering diagram, in the top-right (owner, 2026-10-04: all of them), sized to fit: one
    row up to 4 chords, two up to 8, more beyond. Drawn BEFORE the title, so a long title is in front of them."""
    shapes = [(label, get_chord_shape(label)) for label in labels]
    shapes = [(label, shape) for label, shape in shapes if shape is not None]
    if not shapes:
        return canvas
    cols, rows, cw, ch, gap, x0 = chord_panel_geometry(len(shapes))
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


TITLE_MAX_SIZE = 100            # owner, 2026-10-04: 210 then 140 were too big; Sorry Seems (86 px) is the model


def _fit_title(draw, text: str, max_width: int, max_height: int, font_path: str | None, max_size: int = TITLE_MAX_SIZE):
    """The largest bold size (<= max_size px) at which the title fits in max_width x max_height on at most 3 lines."""
    for size in range(max_size, 59, -6):
        font = _bold_font(size, font_path)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= 3 and len(lines) * size * 1.02 <= max_height and all(draw.textlength(l, font=font) <= max_width for l in lines):
            return font, lines, size
    font = _bold_font(60, font_path)
    return font, _wrap(draw, text, font, max_width), 60


def compose_thumbnail(
    background: Image.Image, title: str, artist: str, out_path: Path, *, tag: str = "PLAY ALONG VIDEOS", font_path: str | None = None,
    chord_labels: list[str] | None = None, sub_tag: str | None = None,
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
    # with the chord panel up there the title keeps to the space LEFT of it, so no diagram is hidden
    title_top = 222 if sub_tag else 175                            # an EASY CHORDS badge sits under the tag: the text starts lower
    shown = sum(1 for label in (chord_labels or []) if get_chord_shape(label) is not None)
    title_width = min(600, chord_panel_geometry(shown)[5] - 50 - 24) if has_chords else 780       # stop short of the panel
    font, lines, size = layout_title(draw, text, title_width, 565 - title_top - 90 if sub_tag else 390, font_path)
    y = title_top
    for line in lines:
        draw.text((50, y), line, font=font, fill=(255, 255, 255), stroke_width=max(6, size // 18), stroke_fill=(0, 0, 0))
        y += int(size * 1.02)
    artist_text = (artist or "").upper().strip()
    if artist_text:
        afont = _bold_font(84, font_path)
        while draw.textlength(artist_text, font=afont) > 800 and afont.size > 40:
            afont = _bold_font(afont.size - 4, font_path)
        draw.text((54, y + 14), artist_text, font=afont, fill=_ARTIST_YELLOW, stroke_width=5, stroke_fill=(0, 0, 0))

    tag_font = _bold_font(54, font_path)
    width = draw.textlength(tag, font=tag_font)
    draw.rounded_rectangle([50, 40, 50 + width + 48, 124], radius=14, fill=_TAG_RED)
    draw.text((74, 82), tag, font=tag_font, fill=(255, 255, 255), anchor="lm")
    if sub_tag:
        sub_font = _bold_font(38, font_path)                     # smaller than the channel tag (54) -- owner, 2026-10-04
        sub_w = draw.textlength(sub_tag, font=sub_font)
        draw.rounded_rectangle([50, 134, 50 + sub_w + 40, 198], radius=12, fill=_SUB_TAG_GREEN)
        draw.text((70, 166), sub_tag, font=sub_font, fill=(255, 255, 255), anchor="lm")

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
        f'Song: "{title}" by {artist}. Lyrics:\n{lyrics[:3500]}\n\n'
        "You are designing the thumbnail picture for a guitar play-along video of this song. A viewer who knows the song must recognise "
        "from the picture alone what it is about.\n"
        "Step 1 (think silently): what is this song really about, and what is its CENTRAL IMAGE or metaphor? The title usually names it; "
        "otherwise take it from the most vivid lines. Pick 2 or 3 concrete visual motifs straight from the lyrics (objects, places, "
        "weather, time of day) -- never a generic 'cozy room' or 'sunset'.\n"
        "Step 2: write ONE image-generation prompt (max 70 words) that shows that central image LITERALLY and big, with ONE GUITAR "
        "(acoustic or electric, whichever suits the song) as part of the scene, interacting with it (resting on it, leaning against it, "
        "surrounded by it). Put the guitar and the central image on the RIGHT half, upper to middle of the frame, well away from the bottom "
        "edge and corners; the left side darker and simple. Bright, vivid, high contrast, cinematic lighting, a glowing well-lit subject on "
        "a darker background. No people or faces, no hands, no piano or other instruments, no text, letters or logos.\n"
        "Reply with ONLY the prompt from step 2."
    )


def write_background_prompt(anthropic_client, title: str, artist: str, lyrics: str) -> tuple[str, float]:
    """(the image prompt, its cost). Raises if Claude's reply has no text."""
    response = anthropic_client.messages.create(
        model=PROMPT_MODEL, max_tokens=600, thinking={"type": "disabled"}, messages=[{"role": "user", "content": build_prompt_request(title, artist, lyrics)}],
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
    work_dir: Path, source_dir: Path, *, title: str, artist: str, tag: str = "PLAY ALONG VIDEOS", font_path: str | None = None,
    chord_labels: list[str] | None = None, sub_tag: str | None = None,
) -> Path | None:
    """A thumbnail for `work_dir` from the picture `source_dir` already chose (an EASY CHORD version uses its song's, with
    its own tag); None when `source_dir` has none."""
    background = Path(source_dir) / THUMBNAIL_BG_FILE
    if not background.exists():
        return None
    with Image.open(background) as img:
        data = img.convert("RGB")
        data.load()
    return compose_thumbnail(data, title, artist, Path(work_dir) / THUMBNAIL_FILE, tag=tag, font_path=font_path, chord_labels=chord_labels, sub_tag=sub_tag)
