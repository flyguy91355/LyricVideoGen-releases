"""Researches the real lyrics for a song diagnosed LYRICS_WRONG (diagnosis.py), using Claude's server-side
web search tool -- a real, cited internet search, run automatically by the API in the same request (no
client-side search loop to write) -- cross-referenced against what's actually heard in the recording.

Why this is different from lyric_reconcile.py's "never invent from memory" rule: that rule exists because
Claude's own unaided memory of a song produces fluent, plausible-sounding MISHEARINGS with no way to tell
them from the truth. A real, cited web source is not memory -- it is evidence, the same category of thing
the transcript already is. So a correction here can be grounded in either: real lyrics sites (preferring
ones that agree with each other and, ideally, that discuss the specific recording/edition), or the audio
itself. Where a well-cited source and the audio genuinely disagree, the audio wins (owner, 2026-09-22: the
video must match what's actually sung, not a generic printed version) -- but the real, final check on
whether any of this actually worked is not this module's own confidence self-report, it's runner.py sending
the result through a real redo and rechecking the timing gate. This module only proposes; it never decides
a song is fixed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

_CONFIDENCE_WORDS = ("high", "low")
# Sonnet 5 pricing (per-token, from the claude-api skill's rate table): $2/1M input, $10/1M output. This is
# the model research_lyrics() itself defaults to, and the only one deep_review currently uses.
_INPUT_PRICE_PER_TOKEN = 2.00 / 1_000_000
_OUTPUT_PRICE_PER_TOKEN = 10.00 / 1_000_000
# Web search is billed on top of the tokens its results add: $10 per 1,000 searches, i.e. $0.01 each -- up to
# $0.03 per attempt at max_uses=3, which alone can decide whether the 5-cent/song cap allows another attempt.
_WEB_SEARCH_PRICE_PER_REQUEST = 10.00 / 1000
# Prompt caching is not used here today; if it ever is, a cache write costs 1.25x input (5-minute TTL) and a
# cache read 0.1x.
_CACHE_WRITE_PRICE_PER_TOKEN = _INPUT_PRICE_PER_TOKEN * 1.25
_CACHE_READ_PRICE_PER_TOKEN = _INPUT_PRICE_PER_TOKEN * 0.10


@dataclass(frozen=True)
class Research:
    lines: list[str]
    citations: list[str] = field(default_factory=list)
    confidence: str = "low"      # "high" | "low" -- runner.py only auto-applies "high"
    notes: str = ""


@dataclass(frozen=True)
class ResearchAttempt:
    """What one research_lyrics() call actually did: its parsed result (None if the reply was unusable) AND
    its real, measured cost -- owner, 2026-09-22: "whats the estimated cost of this?" deserves a real answer
    once the call has actually happened, not a guess. The call costs money either way, so cost_usd is always
    populated, even when research is None."""
    research: Research | None
    cost_usd: float


def _usage_count(obj, name: str) -> float:
    """A usage counter, 0 when absent or not a number (older SDK objects and minimal test doubles lack some)."""
    value = getattr(obj, name, 0) if obj is not None else 0
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _cost_usd(usage) -> float:
    """The real dollar cost of one API response, from its own usage block: input/output tokens, any cached
    tokens, and the per-search web search fee (usage.server_tool_use.web_search_requests). 0.0 if usage is
    missing or malformed (e.g. a minimal test double) rather than raising -- a cost estimate must never break a
    real result over missing telemetry."""
    try:
        cost = usage.input_tokens * _INPUT_PRICE_PER_TOKEN + usage.output_tokens * _OUTPUT_PRICE_PER_TOKEN
    except (AttributeError, TypeError):
        return 0.0
    cost += _usage_count(usage, "cache_creation_input_tokens") * _CACHE_WRITE_PRICE_PER_TOKEN
    cost += _usage_count(usage, "cache_read_input_tokens") * _CACHE_READ_PRICE_PER_TOKEN
    searches = _usage_count(getattr(usage, "server_tool_use", None), "web_search_requests")
    return cost + searches * _WEB_SEARCH_PRICE_PER_REQUEST


def parse_research_reply(reply: str) -> Research | None:
    """None when the reply is unusable (no lines, or unparseable) -- tolerates search narration/chatter
    and a fenced code block around the JSON, same as lyric_arbiter.parse_arbitration."""
    first, last = reply.find("{"), reply.rfind("}")
    if first < 0 or last <= first:
        return None
    try:
        data = json.loads(reply[first:last + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    citations = data.get("citations")
    citations = [str(c) for c in citations] if isinstance(citations, list) else []
    confidence = str(data.get("confidence", "")).strip().lower()
    if confidence not in _CONFIDENCE_WORDS:
        confidence = "low"
    return Research([str(x) for x in lines], citations, confidence, str(data.get("notes", "")).strip())


def build_research_prompt(
    title: str, artist: str, current_lines: list[str], segments: list[dict], mismatched_lines: list[int] | None = None,
    previous_attempts: list[dict] | None = None,
) -> str:
    numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(current_lines, 1))
    heard = "\n".join(f"[{int(s['start'])}s] {s['text']}" for s in segments)
    trouble = (
        f"\nLines the automatic check found suspect: {', '.join(f'line {n}' for n in mismatched_lines)}.\n"
        if mismatched_lines else ""
    )
    retry_section = ""
    if previous_attempts:
        tried = "\n\n".join(
            f"Attempt {i}:\n" + "\n".join(a["lines"]) + f"\nOutcome: {a['outcome']}"
            for i, a in enumerate(previous_attempts, 1)
        )
        retry_section = (
            f"\nEARLIER ATTEMPTS ON THIS SAME SONG (none of these resolved it -- do NOT just repeat one of "
            f"them; find a genuinely different reading, different sources, or reconsider an assumption an "
            f"earlier attempt may have gotten wrong, such as the edition or verse order):\n{tried}\n"
        )
    return (
        f"This song's video was set aside because its lyric file does not match what is actually sung in the "
        f"recording. Search the real internet for the correct lyrics of this specific song and recording, then "
        f"produce a corrected lyric file for the video.\n\n"
        f"SONG: \"{title}\" by {artist}\n\n"
        f"CURRENT LYRIC FILE (numbered lines, may be wrong):\n{numbered}\n\n"
        f"WHAT THE RECOGNIZER ACTUALLY HEARD SUNG (transcript, seconds into the song):\n{heard}\n"
        f"{trouble}"
        f"{retry_section}\n"
        "Instructions:\n"
        "- Search the web for the real lyrics of this song. Recordings of the same song sometimes differ "
        "(radio edit vs. album version, a live recording, a censored version, a different verse order) -- "
        "prefer sources that discuss this specific recording, and cross-check multiple sources rather than "
        "trusting the first result.\n"
        "- Where well-cited real sources and what is actually heard in the recording clearly disagree, prefer "
        "what is actually sung -- the video has to match this recording, not a generic printed version.\n"
        "- Build the corrected lyric file as one sung line per entry, in the order they are sung.\n"
        "- Do not invent or guess lines from your own memory of the song with no source behind them; every "
        "line should be grounded in either a real cited source or the transcript above. If you cannot find "
        "reliable sources and the recording itself is unclear, say so with low confidence rather than guess.\n"
        "- List the real sources you actually used (site names or URLs) as citations.\n\n"
        "Reply with ONLY this JSON, nothing else:\n"
        "{\"lines\": [\"line one\", \"line two\", ...], \"citations\": [\"source\", ...], "
        "\"confidence\": \"high|low\", \"notes\": \"<one short sentence, e.g. which edition this matches>\"}"
    )


def research_lyrics(
    anthropic_client, title: str, artist: str, current_lines: list[str], segments: list[dict],
    mismatched_lines: list[int] | None = None, previous_attempts: list[dict] | None = None,
    model: str = "claude-sonnet-5", max_searches: int = 3,
) -> ResearchAttempt:
    """Always returns a ResearchAttempt, even when the reply is unusable (research=None then) -- the call
    itself cost real money either way, and the caller (runner.py) needs both the result and that real cost.
    Never raises on a bad reply; a network/API error still propagates, since the caller needs to know a
    song's research genuinely failed to try vs. was skipped. `previous_attempts` (owner, 2026-09-22: "not
    identical re attempts") tells a retry exactly what already failed and why, so it's pushed toward a
    genuinely different answer instead of just asking the same question again and hoping for different luck.
    `max_searches` default cut from 5 to 3 the same day (owner: "$0.26 ... thats a lot"; real measured cost
    on "dreams" showed web search results compounding across multiple rounds within one call is the actual
    cost driver, not the transcript/lyrics text, which is small) -- trades some cross-referencing breadth
    for materially lower cost per attempt."""
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"effort": "medium"},
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}],
        messages=[{"role": "user", "content": build_research_prompt(
            title, artist, current_lines, segments, mismatched_lines, previous_attempts,
        )}],
    )
    reply = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    return ResearchAttempt(parse_research_reply(reply), _cost_usd(getattr(response, "usage", None)))
