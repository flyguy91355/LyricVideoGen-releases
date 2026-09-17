"""Sanity-checks already-fetched lyric text against a song's title/artist,
flagging a likely wrong-song match, garbled/corrupted text, wrong language,
or suspicious incompleteness. Reviews text that's already been fetched --
never asks Claude to reproduce full copyrighted lyrics as a ground truth,
just to judge what's already in front of it. See
docs/superpowers/specs/2026-09-18-lyric-accuracy-check-design.md."""

from __future__ import annotations


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
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


def check_lyric_accuracy(
    anthropic_client, title: str, artist: str, lyric_lines: list[str], model: str = "claude-sonnet-5",
) -> tuple[bool, str]:
    """Returns (looks_accurate, concern) -- concern is always "" when
    looks_accurate is True. Never raises on a malformed reply; an
    unparseable LOOKS_ACCURATE simply reads as "" which fails the YES
    check, so a bad reply flags for review rather than silently passing."""
    lyrics_text = "\n".join(lyric_lines)
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    f'Here is lyric text that was fetched for a song titled "{title}" '
                    f'by "{artist}":\n\n{lyrics_text}\n\n'
                    "Judge whether this looks like real, correctly-matched, complete "
                    "lyrics for that specific song -- flag it if it looks like the wrong "
                    "song or a different edition, garbled/corrupted text, the wrong "
                    "language, or suspiciously incomplete. Reply with EXACTLY two lines:\n"
                    "LOOKS_ACCURATE: <YES or NO>\n"
                    "CONCERN: <blank if accurate, otherwise a short reason>"
                ),
            }
        ],
    )
    fields = _parse_labeled_fields(_extract_text(response), ["LOOKS_ACCURATE", "CONCERN"])
    looks_accurate = fields["LOOKS_ACCURATE"].strip().upper().startswith("YES")
    concern = "" if looks_accurate else fields["CONCERN"].strip()
    return looks_accurate, concern
