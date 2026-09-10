"""Claude-authored YouTube upload metadata and comment-reply drafts. See
docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations


class MetadataGenError(Exception):
    pass


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise MetadataGenError("Claude response contained no text content")
    return "".join(parts)


def _parse_labeled_fields(text: str, labels: list[str]) -> dict[str, str]:
    fields = {label: "" for label in labels}
    for line in text.splitlines():
        stripped = line.strip()
        for label in labels:
            prefix = f"{label}:"
            if stripped.upper().startswith(prefix):
                fields[label] = stripped[len(prefix):].strip()
    return fields


def generate_video_metadata(
    anthropic_client, song_title: str, full_lyrics: str, model: str = "claude-sonnet-5",
) -> tuple[str, str, list[str]]:
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f'A song titled "{song_title}" has these lyrics:\n\n{full_lyrics}\n\n'
                    "Write YouTube upload metadata for a 'play along' lyric+chord video of "
                    "this song. Reply with EXACTLY three lines, each prefixed with its label "
                    "and nothing else before or after:\n"
                    "TITLE: <a natural YouTube title>\n"
                    "DESCRIPTION: <a 2-4 sentence description of the song>\n"
                    "TAGS: <5-8 relevant search tags, comma-separated>"
                ),
            }
        ],
    )
    fields = _parse_labeled_fields(_extract_text(response), ["TITLE", "DESCRIPTION", "TAGS"])
    title = fields["TITLE"] or song_title
    description = fields["DESCRIPTION"].strip()
    tags = [t.strip() for t in fields["TAGS"].split(",") if t.strip()]
    return title, description, tags


def draft_comment_reply(
    anthropic_client, comment_text: str, song_title: str, model: str = "claude-sonnet-5",
) -> tuple[str, bool]:
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f'Someone left this comment on a "{song_title}" play-along video:\n\n'
                    f'"{comment_text}"\n\n'
                    "Reply with EXACTLY two lines, each prefixed with its label:\n"
                    "IS_ERROR_REPORT: <YES or NO -- is this reporting a mistake in the video, "
                    "like wrong chords, sync issues, or wrong lyrics?>\n"
                    "REPLY: <a short, friendly, genuine-sounding reply, written as the channel owner>"
                ),
            }
        ],
    )
    fields = _parse_labeled_fields(_extract_text(response), ["IS_ERROR_REPORT", "REPLY"])
    is_error_report = fields["IS_ERROR_REPORT"].strip().upper().startswith("YES")
    return fields["REPLY"], is_error_report
