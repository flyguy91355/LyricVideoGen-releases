"""Sanity-checks already-fetched lyric text against a song's title/artist,
flagging a likely wrong-song match, garbled/corrupted text, wrong language,
or suspicious incompleteness. Reviews text that's already been fetched --
never asks Claude to reproduce full copyrighted lyrics as a ground truth,
just to judge what's already in front of it. See
docs/superpowers/specs/2026-09-18-lyric-accuracy-check-design.md."""

from __future__ import annotations

import logging

log = logging.getLogger("playalongvideoproduction")

# The concern recorded when Claude's reply says NO without a reason, or can't be read at all: never "" (which means
# "passed" to the pipeline and the upload lists).
UNCLEAR_REPLY_CONCERN = "The lyrics text check gave no clear answer, so these lyrics are not confirmed; please review them."


def _extract_text(response) -> str:
    parts = [block.text for block in getattr(response, "content", None) or []
             if getattr(block, "type", None) == "text"]
    return "".join(parts)


def _parse_labeled_fields(text: str, labels: list[str]) -> dict[str, str]:
    fields = {label: "" for label in labels}
    for line in text.splitlines():
        # Markdown decoration ("**LOOKS_ACCURATE:** NO", "- CONCERN: ...") is ignored.
        stripped = line.replace("**", "").replace("__", "").strip().lstrip("*_#->` ").strip()
        for label in labels:
            prefix = f"{label}:"
            if stripped.upper().startswith(prefix):
                fields[label] = stripped[len(prefix):].strip().strip("*_` ").strip()
    return fields


def _ask(anthropic_client, model: str, title: str, artist: str, lyrics_text: str):
    # Thinking OFF: Sonnet 5's default thinking can spend this small budget and return no text at all (the same
    # failure key_opinion.py and youtube_metadata.py guard against); this is a two-line formatted answer.
    return anthropic_client.messages.create(
        model=model,
        max_tokens=200,
        thinking={"type": "disabled"},
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


def check_lyric_accuracy(
    anthropic_client, title: str, artist: str, lyric_lines: list[str], model: str = "claude-sonnet-5",
) -> tuple[bool, str]:
    """Returns (looks_accurate, concern) -- concern is "" exactly when looks_accurate is True. Never raises: an
    API error, a reply with no text (thinking used the budget), an unreadable LOOKS_ACCURATE or a NO without a reason
    all come back as not accurate WITH a concern, so they flag for review rather than silently passing (issue #7
    review: each of those used to return concern "", which the pipeline stored as "lyrics passed")."""
    lyrics_text = "\n".join(lyric_lines)
    try:
        response = _ask(anthropic_client, model, title, artist, lyrics_text)
    except Exception as e:  # overloaded, rate limit, network: the song is held for review, never failed outright
        log.warning("Lyrics text check could not run: %s: %s", type(e).__name__, e)
        return False, (f"The lyrics text check could not run ({type(e).__name__}), so these lyrics are not "
                       "confirmed; please review them.")
    fields = _parse_labeled_fields(_extract_text(response), ["LOOKS_ACCURATE", "CONCERN"])
    looks_accurate = fields["LOOKS_ACCURATE"].strip().upper().startswith("YES")
    concern = "" if looks_accurate else (fields["CONCERN"].strip() or UNCLEAR_REPLY_CONCERN)
    return looks_accurate, concern
