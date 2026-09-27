from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from PIL import Image

from .models import line_hash

if TYPE_CHECKING:
    from .library_session import LibrarySession

FRAME_SIZE = (1920, 1080)
REPLICATE_API_BASE = "https://api.replicate.com/v1"
REPLICATE_POLL_INTERVAL_SECONDS = 1.0
REPLICATE_POLL_TIMEOUT_SECONDS = 120.0
_TERMINAL_STATUSES = ("succeeded", "failed", "canceled")
_MAX_GENERATION_ATTEMPTS = 3
_DOWNLOAD_ATTEMPTS = 3                  # the picture is already paid for once a prediction succeeds
_RATE_LIMIT_RETRIES = 2                 # a 429 on create: wait (Retry-After, capped) and try again
_MAX_RETRY_AFTER_SECONDS = 30.0
# Replicate's pricing page, checked 2026-09-25: black-forest-labs/flux-schnell is "$3.00 / thousand output images".
REPLICATE_PRICE_PER_IMAGE_USD = 0.003


class ImageGenError(Exception):
    pass


class PredictionFailed(ImageGenError):
    """Replicate ran the prediction and it failed or was cancelled (e.g. a content-filter rejection) -- the one
    failure where a REWRITTEN prompt can help, so get_or_generate_image asks Claude for a new one."""


class ReplicateUnreachable(ImageGenError):
    """A network-level failure (poll timeout, output download) -- the prompt was fine, so a retry reuses it."""


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write to a private temp sibling, then rename into place: an interrupted or disk-full write never leaves a
    half-written picture under a cache name (issue #7 review -- one did, and every later render crashed on it)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_copy(source: Path, dest: Path) -> None:
    _atomic_write_bytes(dest, Path(source).read_bytes())


def _is_solid_fallback(img: Image.Image, fallback_color: tuple[int, int, int]) -> bool:
    if img.size != FRAME_SIZE:
        return False
    rgb = img.convert("RGB")
    return all(lo == hi for lo, hi in rgb.getextrema()) and rgb.getpixel((0, 0)) == fallback_color


def _usable_cached_image(path: Path, fallback_color: tuple[int, int, int]) -> bool:
    """True when `path` is a complete, decodable picture that is NOT the plain-colour placeholder. A placeholder
    (every generation failed on an earlier run) or a truncated/corrupt file is a cache MISS: it is generated again
    instead of being trusted forever (issue #7 review)."""
    try:
        with Image.open(path) as img:
            img.load()          # the same full decode the render does -- a truncated file fails here, not there
            return not _is_solid_fallback(img, fallback_color)
    except Exception:
        return False


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise ImageGenError("Claude response contained no text content")
    return "".join(parts)


def summarize_song_gist(
    anthropic_client,
    full_lyrics: str,
    model: str = "claude-sonnet-5",
) -> str:
    """One Claude call, made once per song, so every per-line image prompt shares
    the same anchor -- without this, 17 separate per-line calls can each read the
    same lyrics and still land on visually inconsistent themes/styles."""
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    "Here are a song's full lyrics:\n\n"
                    f"{full_lyrics}\n\n"
                    "In 2-3 sentences, describe what this song is about overall and its "
                    "emotional mood, focused on concrete VISUAL themes/motifs/setting that "
                    "could anchor a consistent set of background images for a lyric video. "
                    "Reply with ONLY that description, nothing else."
                ),
            }
        ],
    )
    return _extract_text(response).strip()


def build_image_prompt(
    anthropic_client,
    song_gist: str,
    line_text: str,
    model: str = "claude-sonnet-5",
) -> str:
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    "You are writing a single image-generation prompt for a background "
                    "image in a lyric video. Here is what the song is about overall, so "
                    "every image in the video stays visually consistent:\n\n"
                    f"{song_gist}\n\n"
                    f'The current line is: "{line_text}"\n\n'
                    "Write ONE concise, vivid, purely visual image-generation prompt (no "
                    "camera jargon, no text-in-image requests) that captures the meaning/"
                    "imagery of this specific line, staying visually consistent with the "
                    "song's overall theme above. Reply with ONLY the prompt text, nothing "
                    "else."
                ),
            }
        ],
    )
    return _extract_text(response).strip()


def _retry_after_seconds(response) -> float:
    headers = getattr(response, "headers", None) or {}
    try:
        seconds = float(headers.get("retry-after") or headers.get("Retry-After") or 5.0)
    except (TypeError, ValueError):
        seconds = 5.0
    return min(max(seconds, 1.0), _MAX_RETRY_AFTER_SECONDS)


def _create_prediction(http_client, token: str, model: str, prompt: str) -> dict:
    owner, name = model.split("/", 1)
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        response = http_client.post(
            f"{REPLICATE_API_BASE}/models/{owner}/{name}/predictions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            # aspect_ratio matches the video's own 1920x1080 (16:9) frame -- flux-schnell
            # defaults to a square 1:1 image otherwise, which would need stretching/
            # distortion to fill a widescreen frame.
            json={"input": {"prompt": prompt, "aspect_ratio": "16:9"}},
            timeout=30.0,
        )
        if getattr(response, "status_code", 200) != 429 or attempt == _RATE_LIMIT_RETRIES:
            break
        time.sleep(_retry_after_seconds(response))      # rate limited: nothing was created (or billed) yet
    response.raise_for_status()
    return response.json()


def _cancel_prediction(http_client, token: str, cancel_url: str | None) -> None:
    """Best effort: stop a prediction this run is abandoning, so it isn't left running (and billed) unseen."""
    if not cancel_url:
        return
    try:
        http_client.post(cancel_url, headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
    except Exception:
        pass


def _poll_prediction(
    http_client, token: str, get_url: str, status: str, output, cancel_url: str | None = None,
) -> tuple[str, object]:
    deadline = time.monotonic() + REPLICATE_POLL_TIMEOUT_SECONDS
    while status not in _TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            _cancel_prediction(http_client, token, cancel_url)
            raise ReplicateUnreachable(
                f"Replicate prediction timed out after {REPLICATE_POLL_TIMEOUT_SECONDS}s (cancelled)"
            )
        time.sleep(REPLICATE_POLL_INTERVAL_SECONDS)
        try:
            response = http_client.get(
                get_url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
            )
            response.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError):
            # a transient server error (e.g. 503) or network blip (read timeout, dropped connection): the
            # prediction is still running on Replicate -- keep polling THIS one until the deadline, never
            # abandon it and buy another (issue #7 review)
            continue
        data = response.json()
        status = data["status"]
        output = data.get("output")
    return status, output


def _download_output(http_client, url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        if attempt:
            time.sleep(REPLICATE_POLL_INTERVAL_SECONDS)
        try:
            response = http_client.get(url, timeout=60.0)
            response.raise_for_status()
            return response.content
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            last_error = e
    raise ReplicateUnreachable(
        f"the generated image could not be downloaded after {_DOWNLOAD_ATTEMPTS} tries: "
        f"{type(last_error).__name__}: {last_error}"
    )


def generate_line_image(
    replicate_token: str,
    prompt: str,
    out_path: Path,
    model: str = "black-forest-labs/flux-schnell",
    http_client=httpx,
) -> Path:
    data = _create_prediction(http_client, replicate_token, model, prompt)
    urls = data.get("urls") or {}
    status, output = _poll_prediction(
        http_client, replicate_token, urls["get"], data["status"], data.get("output"), urls.get("cancel"),
    )
    if status != "succeeded":
        raise PredictionFailed(f"Replicate prediction failed: status={status!r}")

    url = output[0] if isinstance(output, list) else output
    _atomic_write_bytes(out_path, _download_output(http_client, str(url)))
    return out_path


def _prompt_is_reusable(error: Exception) -> bool:
    """A failure that says nothing about the prompt (network, timeout, an HTTP error from Replicate other than a
    rejected input): the retry reuses the prompt Claude already wrote instead of paying for a new one (issue #7
    review). Anything else -- a failed or
    content-rejected prediction, or an unknown error -- gets a freshly written prompt, as before."""
    if isinstance(error, (httpx.TransportError, ReplicateUnreachable)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        # auth/credit (401/402/403), rate limit (429) and server (5xx) errors are never the prompt's fault;
        # only a rejected INPUT (400/422) might be
        code = getattr(error.response, "status_code", 0) or 0
        return code not in (400, 422)
    return False


def get_or_generate_image(
    anthropic_client,
    replicate_token: str,
    song_gist: str,
    line_text: str,
    cache_dir: Path,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
    extra_cache_dirs: list[Path] | None = None,
    previous_image: Path | None = None,
    library: "LibrarySession | None" = None,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = line_hash(line_text)
    cached_path = cache_dir / f"{key}.png"
    if cached_path.exists():
        if _usable_cached_image(cached_path, fallback_color):
            return cached_path
        # A plain-colour placeholder from a run where every generation failed (Replicate down, out of credit),
        # or a file cut short by a kill/full disk: never a permanent cache hit -- generate it again.
        print(
            f"WARNING: the cached image for line {key} is a plain-colour placeholder or unreadable; "
            "generating it again",
            file=sys.stderr,
        )

    # Check any backup/archive directories (e.g. images_backup_<timestamp>/ from a
    # prior regeneration) for an already-paid-for image before generating a new
    # one -- a line's own text never changes what its hash is, so an old backup
    # is exactly as valid as a fresh generation (unless it is itself a placeholder or broken).
    for extra_dir in extra_cache_dirs or []:
        candidate = extra_dir / f"{key}.png"
        if candidate.exists() and _usable_cached_image(candidate, fallback_color):
            _atomic_copy(candidate, cached_path)
            return cached_path

    last_error: Exception | None = None
    library_consulted = False
    prompt: str | None = None
    for attempt in range(_MAX_GENERATION_ATTEMPTS):
        try:
            if prompt is None:
                prompt = build_image_prompt(anthropic_client, song_gist, line_text)
            if library is not None and not library_consulted:
                # Once, with the first prompt that builds: a later retry's rewritten prompt is not re-matched.
                # The library never raises (it disables itself), so a problem there can't trigger a retry.
                library_consulted = True
                hit = library.find_match(prompt, cached_path)
                if hit is not None:
                    return hit
            generate_line_image(replicate_token, prompt, cached_path)
            if library is not None:
                library.record_purchase(cached_path, prompt, line_text)
            return cached_path
        except Exception as e:
            last_error = e
            if not _prompt_is_reusable(e):
                prompt = None           # a rejected/failed prediction (or unknown error): ask Claude for a new prompt
            print(
                f"WARNING: image generation attempt {attempt + 1}/{_MAX_GENERATION_ATTEMPTS} "
                f"failed for line {key}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )

    if previous_image is not None and previous_image.exists():
        # Reuse the last successfully-generated real image immediately rather
        # than ever writing a flat color to disk -- a generation failure is
        # never rare enough (content-filter rejections in particular repeat
        # identically on every retry) to risk a plain-color frame reaching a
        # finished video if this call happens to be the one substitute_fallback_
        # images() never gets to run for. substitute_fallback_images() can
        # still improve on this later by picking a chronologically closer
        # neighbor once the whole song's images are known.
        print(
            f"WARNING: reusing the previous image for line {key} after "
            f"{_MAX_GENERATION_ATTEMPTS} failed attempts (last error: {last_error})",
            file=sys.stderr,
        )
        _atomic_copy(previous_image, cached_path)
        return cached_path

    print(
        f"WARNING: falling back to a plain-color background for line {key} "
        f"after {_MAX_GENERATION_ATTEMPTS} failed attempts (last error: {last_error}) -- "
        "no earlier real image exists yet in this song to reuse instead. "
        "substitute_fallback_images() will replace this with a real neighboring "
        "image once the whole song's images have been generated, unless every "
        "single one of them failed",
        file=sys.stderr,
    )
    placeholder = io.BytesIO()
    Image.new("RGB", FRAME_SIZE, fallback_color).save(placeholder, format="PNG")
    _atomic_write_bytes(cached_path, placeholder.getvalue())
    return cached_path


def is_fallback_image(path: Path, fallback_color: tuple[int, int, int] = (30, 30, 40)) -> bool:
    """True if the PNG at `path` is get_or_generate_image's own last-resort
    plain-color placeholder -- a real AI-generated image is never a single
    solid color, so this is an unambiguous signature, not a heuristic.
    The size is checked from the header first, so a real (differently sized)
    picture is never decoded just to answer this."""
    try:
        with Image.open(path) as img:
            return _is_solid_fallback(img, fallback_color)
    except Exception:
        return False


def substitute_fallback_images(
    image_paths: list[Path], fallback_color: tuple[int, int, int] = (30, 30, 40)
) -> None:
    """Called once after a full images-stage pass (every line's + every
    instrumental caption's image already generated or fallen back). A flat
    placeholder color visibly breaks a finished video even though the
    pipeline itself never crashes on a generation failure -- so any fallback
    found here is replaced with a copy of the *nearest real, successfully
    generated* image in the song's own sequence (previous line preferred,
    falling back to the next one for a fallback that leads the whole list).
    Only when every single image in the song is a fallback (the API was down
    for the whole run) is anything left as the plain-color placeholder --
    there is no real image anywhere left to substitute in that case."""
    existing = [p for p in image_paths if p.exists()]
    if not existing or all(is_fallback_image(p, fallback_color) for p in existing):
        return

    last_real: Path | None = None
    for path in image_paths:
        if not path.exists():
            continue
        if is_fallback_image(path, fallback_color):
            if last_real is not None:
                _atomic_copy(last_real, path)
        else:
            last_real = path

    next_real: Path | None = None
    for path in reversed(image_paths):
        if not path.exists():
            continue
        if is_fallback_image(path, fallback_color):
            if next_real is not None:
                _atomic_copy(next_real, path)
        else:
            next_real = path
