from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
from PIL import Image

from .models import line_hash

FRAME_SIZE = (1920, 1080)
REPLICATE_API_BASE = "https://api.replicate.com/v1"
REPLICATE_POLL_INTERVAL_SECONDS = 1.0
REPLICATE_POLL_TIMEOUT_SECONDS = 120.0
_TERMINAL_STATUSES = ("succeeded", "failed", "canceled")


class ImageGenError(Exception):
    pass


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


def _create_prediction(http_client, token: str, model: str, prompt: str) -> dict:
    owner, name = model.split("/", 1)
    response = http_client.post(
        f"{REPLICATE_API_BASE}/models/{owner}/{name}/predictions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        # aspect_ratio matches the video's own 1920x1080 (16:9) frame -- flux-schnell
        # defaults to a square 1:1 image otherwise, which would need stretching/
        # distortion to fill a widescreen frame.
        json={"input": {"prompt": prompt, "aspect_ratio": "16:9"}},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


def _poll_prediction(http_client, token: str, get_url: str, status: str, output) -> tuple[str, object]:
    deadline = time.monotonic() + REPLICATE_POLL_TIMEOUT_SECONDS
    while status not in _TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            raise ImageGenError(
                f"Replicate prediction timed out after {REPLICATE_POLL_TIMEOUT_SECONDS}s"
            )
        time.sleep(REPLICATE_POLL_INTERVAL_SECONDS)
        try:
            response = http_client.get(
                get_url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
            )
            response.raise_for_status()
        except httpx.HTTPStatusError:
            continue  # transient server error (e.g. 503) -- just retry on the next tick
        data = response.json()
        status = data["status"]
        output = data.get("output")
    return status, output


def generate_line_image(
    replicate_token: str,
    prompt: str,
    out_path: Path,
    model: str = "black-forest-labs/flux-schnell",
    http_client=httpx,
) -> Path:
    data = _create_prediction(http_client, replicate_token, model, prompt)
    status, output = _poll_prediction(
        http_client, replicate_token, data["urls"]["get"], data["status"], data.get("output")
    )
    if status != "succeeded":
        raise ImageGenError(f"Replicate prediction failed: status={status!r}")

    url = output[0] if isinstance(output, list) else output
    img_response = http_client.get(str(url), timeout=60.0)
    img_response.raise_for_status()
    out_path.write_bytes(img_response.content)
    return out_path


def get_or_generate_image(
    anthropic_client,
    replicate_token: str,
    song_gist: str,
    line_text: str,
    cache_dir: Path,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
    extra_cache_dirs: list[Path] | None = None,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = line_hash(line_text)
    cached_path = cache_dir / f"{key}.png"
    if cached_path.exists():
        return cached_path

    # Check any backup/archive directories (e.g. images_backup_<timestamp>/ from a
    # prior regeneration) for an already-paid-for image before generating a new
    # one -- a line's own text never changes what its hash is, so an old backup
    # is exactly as valid as a fresh generation.
    for extra_dir in extra_cache_dirs or []:
        candidate = extra_dir / f"{key}.png"
        if candidate.exists():
            cached_path.write_bytes(candidate.read_bytes())
            return cached_path

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            prompt = build_image_prompt(anthropic_client, song_gist, line_text)
            return generate_line_image(replicate_token, prompt, cached_path)
        except Exception as e:
            last_error = e
            print(
                f"WARNING: image generation attempt {attempt + 1}/2 failed for "
                f"line {key}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )

    print(
        f"WARNING: falling back to a plain-color background for line {key} "
        f"after 2 failed attempts (last error: {last_error})",
        file=sys.stderr,
    )
    Image.new("RGB", FRAME_SIZE, fallback_color).save(cached_path)
    return cached_path
