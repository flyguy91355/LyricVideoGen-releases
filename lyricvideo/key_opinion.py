"""A second opinion on a song's key (owner, 2026-09-26): one small Claude call that CHOOSES between the few keys the
detected chords allow (key_estimate.candidate_keys) -- the key the song's published sheet music lists for the original
recording. Asked to name the key from memory, Claude was right for only 40 of 80 checked songs (it is often a semitone
or a fifth off); choosing among the candidates the chords support is what makes it useful. It is only ever compared with
the chord-based estimate (key_decision.py), never trusted alone. Never raises: a failed, off-list or unsure answer is
None ("no second opinion")."""

from __future__ import annotations

import logging
import re

from .chord_theory import key_name
from .key_estimate import parse_key

log = logging.getLogger("playalongvideoproduction")

_ATTEMPTS = 3
_KEY_LINE = re.compile(r"KEY:\s*([^\n]+)", re.IGNORECASE)


def _reply_text(response) -> str:
    return "".join(getattr(b, "text", "") for b in getattr(response, "content", []) if getattr(b, "type", "") == "text")


def ask_published_key(
    anthropic_client, title: str, artist: str, candidates: list[str], chords: str, model: str = "claude-sonnet-5",
) -> str | None:
    """One of `candidates` (as written there, in the app's own spelling), or None when Claude says none fit / is unsure /
    answers off the list or off the format, or the call fails. Sonnet 5 thinks by default, which can use a small token
    budget up and return no text (see youtube_metadata.py), so thinking is off -- this is a one-line formatted answer."""
    allowed = {}
    for c in candidates:
        parsed = parse_key(c)
        if parsed is not None:
            allowed[parsed] = c
    if not allowed:
        return None
    by = f" by {artist.strip()}" if artist and artist.strip() else ""
    options = "\n".join(f"- {c}" for c in candidates)
    prompt = (
        f'The song "{title}"{by} has these chords in its recording (share of the song): {chords}.\n\n'
        "Which ONE of these keys is the key its published sheet music lists for the original recording (for example the "
        f"original key on Musicnotes)?\n{options}\n\n"
        "Reply with EXACTLY one line and nothing else: KEY: <the key copied from the list>, or KEY: NONE if none of "
        "them is right or you are not sure."
    )
    for _attempt in range(_ATTEMPTS):
        try:
            response = anthropic_client.messages.create(
                model=model, max_tokens=100, thinking={"type": "disabled"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # network, quota, content filter...: no opinion, never a crash
            log.warning("Published-key lookup for %r failed: %s: %s", title, type(e).__name__, e)
            continue
        match = _KEY_LINE.search(_reply_text(response))
        if match is None:
            continue
        answer = match.group(1).strip().rstrip(".")
        if answer.upper().startswith("NONE"):
            return None
        parsed = parse_key(answer)
        if parsed in allowed:
            return key_name(parsed[0], parsed[1], True)
    return None
