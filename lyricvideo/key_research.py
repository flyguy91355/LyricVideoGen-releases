"""Researches a song's real key on the web (owner, 2026-10-04: "i dont want any songs in review if they dont have to be").

key_decision.py settles a key when the chord estimate and Claude's from-memory second opinion AGREE. When they do not,
this module looks the key up with Claude's server-side web search (real, cited sources: the original key on Musicnotes,
Hooktheory, tab sites listing the song without a capo) and CHOOSES among the keys the detected chords allow. Memory is not
trusted here -- only a high-confidence answer backed by at least two cited sources settles a key; anything less leaves the
song for the owner's Set Key, now with what the sources said shown beside it. Never raises: a failed or unusable search is
None ("no research")."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .chord_theory import key_name
from .key_estimate import parse_key

log = logging.getLogger("playalongvideoproduction")

MIN_SOURCES = 2
# Haiku 4.5 (owner, 2026-10-04: keep this cheap): $1/1M in, $5/1M out, plus $0.01 per web search. It takes only the basic
# web search tool (web_search_20250305; the dynamic-filtering _20260209 variant is for Sonnet/Opus) and no `effort` setting.
MODEL = "claude-haiku-4-5"
MAX_SEARCHES = 2
_INPUT_PRICE = 1.00 / 1_000_000
_OUTPUT_PRICE = 5.00 / 1_000_000
_SEARCH_PRICE = 0.01


@dataclass(frozen=True)
class KeyResearch:
    key: str = ""                       # one of the candidates, in the app's spelling; "" = the sources gave no usable answer
    sources: list[str] = field(default_factory=list)
    confident: bool = False             # high confidence AND >= MIN_SOURCES cited sources
    notes: str = ""
    cost_usd: float = 0.0


def _number(obj, name: str) -> float:
    value = getattr(obj, name, 0) if obj is not None else 0
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _cost_usd(usage) -> float:
    try:
        cost = usage.input_tokens * _INPUT_PRICE + usage.output_tokens * _OUTPUT_PRICE
    except (AttributeError, TypeError):
        return 0.0
    return cost + _number(getattr(usage, "server_tool_use", None), "web_search_requests") * _SEARCH_PRICE


def build_key_research_prompt(title: str, artist: str, candidates: list[str], chords: str) -> str:
    by = f" by {artist.strip()}" if artist and artist.strip() else ""
    options = "\n".join(f"- {c}" for c in candidates)
    return (
        f'Find the real key of the original studio recording of "{title}"{by}.\n\n'
        f"An analysis of the recording's audio found these chords (share of the song): {chords}. These keys fit them:\n"
        f"{options}\n\n"
        "Search the web: the key printed as the ORIGINAL key on Musicnotes or similar sheet-music stores, Hooktheory, "
        "Songsterr, and tab/chord sites that list the song in its original key (no capo or transposition). Cross-check at "
        "least two independent sources; prefer ones about this specific recording over a cover, a live version or a "
        "transposed arrangement.\n\n"
        "Choose the ONE key from the list above that the sources support. If the sources name a key that is not on the "
        "list, or disagree with each other, or you found nothing reliable, say so with low confidence rather than guess; "
        "never answer from memory alone.\n\n"
        'Reply with ONLY this JSON, nothing else: {"key": "<copied from the list, or NONE>", "citations": '
        '["site or URL actually used", ...], "confidence": "high|low", "notes": "<one short sentence: what the sources said>"}'
    )


def parse_key_research(reply: str, candidates: list[str]) -> KeyResearch | None:
    """None for an unusable reply; a KeyResearch with key "" when the sources gave no answer on the list."""
    first, last = reply.find("{"), reply.rfind("}")
    if first < 0 or last <= first:
        return None
    try:
        data = json.loads(reply[first:last + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    allowed = {}
    for c in candidates:
        parsed = parse_key(c)
        if parsed is not None:
            allowed[parsed] = c
    answer = str(data.get("key", "")).strip().rstrip(".")
    parsed = None if not answer or answer.upper().startswith("NONE") else parse_key(answer)
    key = key_name(parsed[0], parsed[1], True) if parsed in allowed else ""
    sources = data.get("citations")
    sources = [str(s).strip() for s in sources if str(s).strip()] if isinstance(sources, list) else []
    confidence = str(data.get("confidence", "")).strip().lower()
    notes = re.sub(r"\s+", " ", str(data.get("notes", ""))).strip()
    return KeyResearch(key, sources, bool(key) and confidence == "high" and len(sources) >= MIN_SOURCES, notes)


def research_song_key(
    anthropic_client, title: str, artist: str, candidates: list[str], chords: str,
    model: str = MODEL, max_searches: int = MAX_SEARCHES,
) -> KeyResearch | None:
    """One web-searching Claude call (MAX_SEARCHES searches max; the search fee is the floor, $0.01 each). None on any
    failure or an unreadable reply."""
    try:
        response = anthropic_client.messages.create(
            model=model, max_tokens=2000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}],
            messages=[{"role": "user", "content": build_key_research_prompt(title, artist, candidates, chords)}],
        )
    except Exception as e:  # network, quota, content filter...: no research, never a crash
        log.warning("Key research for %r failed: %s: %s", title, type(e).__name__, e)
        return None
    reply = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    result = parse_key_research(reply, candidates)
    if result is None:
        return None
    return KeyResearch(result.key, result.sources, result.confident, result.notes, _cost_usd(getattr(response, "usage", None)))
