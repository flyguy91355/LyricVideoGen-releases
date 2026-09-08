from __future__ import annotations

import base64
import io
import json
import re
import time
from pathlib import Path

import pdfplumber

from .models import ChordWord, InstrumentalBlock, LyricLine

SECTION_LABEL_RE = re.compile(r"^[\[(].*[\])]$")

VISION_PROMPT_TEMPLATE = """This is a page (or pages) from a guitar chord sheet PDF, \
rendered as an image because it has no extractable text layer.

Below is the song's real lyric text, given to you verbatim and numbered by line. \
Do NOT reproduce, repeat, paraphrase, or include any of this lyric text in your \
answer - it is provided only so you can match it against what you see in the image.

You have two jobs:

1. For each numbered line, look at the image and determine which chord symbol (if \
any) is positioned above which word in that line, using the word's position in the \
line (0-based index, counting from the first word as 0). A chord written above a \
gap between words applies to whichever word it's closest to/starts above. If a line \
has no chords shown above it in the image, or the line doesn't appear in the image \
at all, give it an empty list.

2. Find any instrumental-only chord progressions in the image (sections like an \
Intro, an instrumental break, or an outro, shown as chord symbols with NO lyric \
words under them - just the chords themselves, e.g. "Em7  G  Em7  G"). For each \
one, list its chords in the order they appear, and say which numbered line it comes \
right before (using the count of numbered lines below if it comes after all of \
them, e.g. an outro). Chord symbols are not copyrighted, so include them directly.

Numbered lines:
{numbered_lines}

Reply with ONLY a JSON object (no other text), in this exact shape:
{{
  "line_chords": [[[0, "G"], [2, "D"]], [], [[1, "C"]]],
  "instrumental_blocks": [{{"before_line_index": 0, "chords": ["Em7", "G", "Em7", "G"]}}]
}}
"line_chords" must have exactly one entry per numbered line, in order. \
"instrumental_blocks" may be an empty list if there are none.
"""


class VisionParseError(Exception):
    pass


def split_lyrics_text(text: str) -> list[str]:
    lines = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or SECTION_LABEL_RE.match(stripped):
            continue
        lines.append(stripped)
    return lines


def render_pdf_page_images(pdf_path: Path, resolution: int = 200) -> list[bytes]:
    images: list[bytes] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_image = page.to_image(resolution=resolution)
            buf = io.BytesIO()
            page_image.original.save(buf, format="PNG")
            images.append(buf.getvalue())
    return images


def _extract_json_object(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise VisionParseError(f"Claude response did not contain a JSON object: {text[:200]!r}")
    return json.loads(text[start : end + 1])


# Empirically found against real songs: a 17-line batch (Wish You Were Here)
# succeeded, a 15-line batch (Turn the Page) was content-filtered, and 5/8/10-line
# batches of Turn the Page all succeeded -- 10 is a safety margin below the
# smallest observed failure, not a documented Anthropic limit.
DEFAULT_MAX_LINES_PER_CALL = 10


def map_chords_with_vision(
    anthropic_client,
    pdf_path: Path,
    lyric_lines: list[str],
    model: str = "claude-sonnet-5",
    page_images: list[bytes] | None = None,
    max_lines_per_call: int = DEFAULT_MAX_LINES_PER_CALL,
) -> tuple[list[LyricLine], list[InstrumentalBlock]]:
    if not lyric_lines:
        raise VisionParseError("no lyric lines supplied to map chords onto")

    if page_images is None:
        page_images = render_pdf_page_images(pdf_path)
    if not page_images:
        raise VisionParseError(f"{pdf_path} has no pages to read")

    # A single request holding a whole song's verbatim lyrics as input context
    # can trip Anthropic's content-filtering policy once the song is long
    # enough (confirmed: 17 lines was fine, 50 lines was blocked) -- batching
    # keeps each request's verbatim-lyric volume well under whatever that
    # threshold is, at the cost of one API call per batch instead of one call
    # per song.
    all_lines: list[LyricLine] = []
    all_blocks: list[InstrumentalBlock] = []
    for batch_start in range(0, len(lyric_lines), max_lines_per_call):
        batch = lyric_lines[batch_start : batch_start + max_lines_per_call]
        batch_lines, batch_blocks = _map_chords_batch(anthropic_client, page_images, batch, model)
        all_lines.extend(batch_lines)
        for block in batch_blocks:
            all_blocks.append(
                InstrumentalBlock(
                    chords=block.chords,
                    before_line_index=block.before_line_index + batch_start,
                )
            )
    return all_lines, all_blocks


CONTENT_FILTER_MAX_ATTEMPTS = 6
CONTENT_FILTER_RETRY_DELAY_SECONDS = 2.0


def _is_content_filter_error(exc: Exception) -> bool:
    # Empirically confirmed non-deterministic: the exact same request that got
    # "Output blocked by content filtering policy" failed on one call and
    # succeeded cleanly on an identical retry seconds later -- treat it as
    # transient, not a hard block, the same way image generation already
    # retries transient failures.
    return "content filtering policy" in str(exc).lower()


def _map_chords_batch(
    anthropic_client,
    page_images: list[bytes],
    lyric_lines: list[str],
    model: str,
) -> tuple[list[LyricLine], list[InstrumentalBlock]]:
    numbered_lines = "\n".join(f"{i}: {line}" for i, line in enumerate(lyric_lines))
    prompt = VISION_PROMPT_TEMPLATE.format(numbered_lines=numbered_lines)

    content: list[dict] = [{"type": "text", "text": prompt}]
    for img_bytes in page_images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(img_bytes).decode("ascii"),
                },
            }
        )

    # Streaming (+ get_final_message()) rather than a plain create() call: a
    # large song's chord map can need well beyond the ~16000-token ceiling
    # that's safe for non-streaming requests before risking an SDK HTTP
    # timeout, and Sonnet 5 supports up to 128K output tokens when streamed.
    last_error: Exception | None = None
    response = None
    for attempt in range(CONTENT_FILTER_MAX_ATTEMPTS):
        try:
            with anthropic_client.messages.stream(
                model=model,
                max_tokens=64000,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                response = stream.get_final_message()
            break
        except Exception as e:
            if _is_content_filter_error(e) and attempt < CONTENT_FILTER_MAX_ATTEMPTS - 1:
                last_error = e
                time.sleep(CONTENT_FILTER_RETRY_DELAY_SECONDS)
                continue
            raise
    if response is None:
        raise VisionParseError(f"vision call never succeeded: {last_error}")

    text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not text_parts:
        raise VisionParseError(
            f"Claude response contained no text content (stop_reason={response.stop_reason!r}, "
            f"block types={[getattr(b, 'type', None) for b in response.content]})"
        )
    raw = _extract_json_object("".join(text_parts))

    line_chords = raw.get("line_chords")
    if line_chords is None or len(line_chords) != len(lyric_lines):
        got = 0 if line_chords is None else len(line_chords)
        raise VisionParseError(
            f"expected {len(lyric_lines)} line_chords entries, got {got}"
        )

    result: list[LyricLine] = []
    for line_text, chord_positions in zip(lyric_lines, line_chords):
        words_text = line_text.split()
        words = [ChordWord(word=w) for w in words_text]
        for entry in chord_positions:
            idx, chord = entry[0], entry[1]
            if 0 <= idx < len(words):
                words[idx].chord = chord
        result.append(LyricLine(words=words))

    instrumental_blocks = [
        InstrumentalBlock(chords=list(b["chords"]), before_line_index=int(b["before_line_index"]))
        for b in raw.get("instrumental_blocks", [])
    ]

    return result, instrumental_blocks
