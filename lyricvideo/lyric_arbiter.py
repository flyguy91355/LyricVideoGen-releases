"""Claude as a JUDGE of the stretches the audio check could not match -- never as a copyist.

Why: copying Whisper's transcript into the lyrics (lyric_reconcile.py) measurably replaced CORRECT
lyrics with Whisper's fluent mishearings ("No reason to get excited..." became "I'm going to sing a
song"). But the same evidence lets Claude JUDGE well: shown the lyric file next to the transcript,
it can tell a phonetically similar mishearing, a loop of nonsense, or a missing quiet vocal (the
RECOGNIZER failed) from a stretch where the transcript clearly holds different coherent lyrics (the
FILE is wrong). It writes no lyrics; it only returns a verdict per unmatched range.

`confirmed` is True only when EVERY unmatched range is judged a recognizer error and no sung section
is reported missing; anything else (wrong, unsure, forgotten, unparseable) leaves the song held.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .lyric_audio_match import MAX_UNEXPLAINED_SUNG_WORDS, AudioMatch, audio_match_passes

_VERDICTS = ("recognizer_error", "lyrics_wrong", "unsure")


@dataclass(frozen=True)
class RangeVerdict:
    start: int
    end: int
    verdict: str        # recognizer_error | lyrics_wrong | unsure
    reason: str


@dataclass(frozen=True)
class Arbitration:
    confirmed: bool
    verdicts: list[RangeVerdict] = field(default_factory=list)
    missing_section: bool = False
    missing_note: str = ""


def _span(a: int, b: int) -> str:
    return f"line {a}" if a == b else f"lines {a}-{b}"


def parse_arbitration(reply: str, ranges: list[tuple[int, int]], gap_flagged: bool = False) -> Arbitration | None:
    first, last = reply.find("{"), reply.rfind("}")
    if first < 0 or last <= first:
        return None
    try:
        data = json.loads(reply[first:last + 1])
    except ValueError:
        return None
    raw = data.get("ranges") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None
    verdicts: list[RangeVerdict] = []
    for item in raw:
        try:
            a, b = (int(x) for x in item["lines"])
            word = str(item.get("verdict", "")).strip().lower()
            verdicts.append(RangeVerdict(a, b, word if word in _VERDICTS else "unsure", str(item.get("reason", "")).strip()))
        except (KeyError, TypeError, ValueError):
            continue
    missing = bool(data.get("missing_section", False))
    note = str(data.get("missing_note", "")).strip()

    def judged(rng: tuple[int, int]) -> str:
        overlapping = [v.verdict for v in verdicts if v.start <= rng[1] and rng[0] <= v.end]
        if not overlapping:
            return "unsure"                      # forgotten
        return "recognizer_error" if all(v == "recognizer_error" for v in overlapping) else "not-ok"

    all_fine = all(judged(r) == "recognizer_error" for r in ranges)
    gap_ok = not gap_flagged or "missing_section" in data   # a flagged gap must be explicitly answered
    return Arbitration(all_fine and not missing and gap_ok, verdicts, missing, note)


def describe_arbitration(result: Arbitration) -> str:
    parts = [f"{_span(v.start, v.end)}: {v.reason or 'judged wrong for this recording'}"
             for v in result.verdicts if v.verdict == "lyrics_wrong"]
    text = "AI review found the lyrics likely wrong at " + "; ".join(parts) if parts else ""
    if result.missing_section:
        note = result.missing_note or "a sung section seems to be missing"
        text += (". " if text else "AI review: ") + f"A sung section may be missing from the lyrics ({note})"
    unsure = [v for v in result.verdicts if v.verdict == "unsure"]
    if unsure and not text:
        text = "AI review could not tell whether " + ", ".join(_span(v.start, v.end) for v in unsure) + " are right"
    return (text + ".") if text else ""


def build_arbiter_prompt(lines: list[str], match: AudioMatch, segments: list[dict]) -> str:
    numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    heard = "\n".join(f"[{int(s['start'])}s] {s['text']}" for s in segments)
    ranges = ", ".join(_span(a, b) for a, b in match.unsupported_ranges) or "none"
    gap = ""
    if match.worst_heard_gap > MAX_UNEXPLAINED_SUNG_WORDS:
        gap = (
            f"\nAlso, about {match.worst_heard_gap} sung words in a row are not in the lyric file "
            f"(roughly: \"{match.gap_text}\").\n"
        )
    return (
        "A speech recognizer transcribed the isolated vocal track of a song, and an automatic check found "
        "stretches of the lyric file it could not match to that transcript. Speech recognizers are unreliable "
        "on singing: they mishear words in fluent, plausible-looking ways, loop on repeated phrases, and miss "
        "quiet or backing vocals. So a mismatch means EITHER the lyric file is wrong for this recording "
        "OR the recognizer failed. Decide which, for each stretch.\n\n"
        f"LYRIC FILE (numbered lines):\n{numbered}\n\n"
        f"WHAT THE RECOGNIZER HEARD (transcript, seconds into the song):\n{heard}\n\n"
        f"Unmatched stretches: {ranges}.{gap}\n"
        "For EACH unmatched stretch give one verdict:\n"
        "- \"recognizer_error\": the lyric lines are coherent, plausible lyrics for this song, and the transcript "
        "around it is garbled, looping, nonsense, absent, or a phonetically similar mishearing of those lines.\n"
        "- \"lyrics_wrong\": the transcript there clearly holds DIFFERENT coherent lyrics that contradict the "
        "file's lines, so those lines are not what is sung (a different verse, edition or song, wrong words, or a "
        "section out of order).\n"
        "A line that is simply missing from the transcript, or merged into a neighbouring line, is NOT evidence "
        "the lyric is wrong: recognizers routinely skip and merge lines. Use \"lyrics_wrong\" only when you can "
        "point to transcript words that contradict the file's, otherwise \"recognizer_error\" or \"unsure\".\n"
        "- \"unsure\": you cannot tell.\n"
        "Use your own knowledge of the song as well as how the transcript reads, and prefer \"unsure\" to a guess. "
        "Do not write or rewrite any lyrics; only judge.\n"
        + ("If sung words are listed above as missing from the file, also say whether they look like a real sung "
           "section the lyric file lacks (\"missing_section\": true) or like a loop, ad-lib or recognizer "
           "hallucination (false).\n" if gap else "")
        + "\nReply with ONLY this JSON, nothing else:\n"
        "{\"ranges\": [{\"lines\": [first, last], \"verdict\": \"recognizer_error|lyrics_wrong|unsure\", "
        "\"reason\": \"<one short sentence>\"}], \"missing_section\": false, \"missing_note\": \"\"}"
    )


def arbitrate(
    anthropic_client, lines: list[str], match: AudioMatch, segments: list[dict], model: str = "claude-sonnet-5",
) -> Arbitration | None:
    """None when there is nothing to judge or the reply is unusable (=> the song stays held)."""
    gap_flagged = match.worst_heard_gap > MAX_UNEXPLAINED_SUNG_WORDS
    if audio_match_passes(match) or (not match.unsupported_ranges and not gap_flagged):
        return None
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=16000,                        # Sonnet 5 reasons first; leave room for the JSON
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": build_arbiter_prompt(lines, match, segments)}],
    )
    reply = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    return parse_arbitration(reply, match.unsupported_ranges, gap_flagged)
