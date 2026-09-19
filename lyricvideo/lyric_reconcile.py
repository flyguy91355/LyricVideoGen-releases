"""Repairs lyric text that doesn't match the audio, using Whisper's transcript and Claude.

Claude is never asked to write lyrics from memory or to return the whole file. It sees the
numbered lyric lines and what was heard, and returns a small list of EDITS (replace / insert /
delete a range of lines). Everything that could go wrong is enforced here in code:
  - new or replacement text must be built from words actually in the transcript, so Claude
    cannot introduce lyrics from memory;
  - a line the audio already supports can never be replaced or deleted;
  - edits are validated, de-overlapped and applied bottom-up so line numbers stay correct.
A repair is only ever a SUGGESTION. Measured on real songs (2026-09-19), applying it "improved"
the audio match while replacing CORRECT lyrics with the recognizer's fluent mishearings
("No reason to get excited..." became "I'm going to sing a song"): text built from Whisper's
words matches Whisper by construction, and Whisper's own confidence scores did not separate
its good segments from its bad ones. So the pipeline saves the proposal for the owner to read
and never feeds it back into the video.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .lyric_audio_match import AudioMatch, _content, _tokens, _words_match

SUGGESTION_FILENAME = "lyrics_suggested.txt"   # where the pipeline saves a proposed fix for review

_MIN_HEARD_FRACTION = 0.7   # share of an edit's content words that must appear in the transcript


@dataclass(frozen=True)
class Edit:
    kind: str          # "replace" | "insert" | "delete"
    start: int         # 1-based first line; for "insert", the line to insert AFTER (0 = at the very start)
    end: int           # 1-based last line (== start for "insert")
    text: list[str]    # new lines ("replace"/"insert"); empty for "delete"


def parse_edits(reply: str) -> list[Edit]:
    """Edits out of Claude's reply (tolerates chatter and code fences); anything malformed is dropped."""
    first, last = reply.find("{"), reply.rfind("}")
    if first < 0 or last <= first:
        return []
    try:
        data = json.loads(reply[first:last + 1])
    except ValueError:
        return []
    raw = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    edits: list[Edit] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            op = item.get("op")
            if op in ("replace", "delete"):
                a, b = (int(x) for x in item["lines"])
                text = [str(x) for x in item.get("text", [])] if op == "replace" else []
                if a < 1 or b < a or (op == "replace" and not text):
                    continue
                edits.append(Edit(op, a, b, text))
            elif op == "insert":
                after = int(item["after"])
                text = [str(x) for x in item.get("text", [])]
                if after < 0 or not text:
                    continue
                edits.append(Edit("insert", after, after, text))
        except (KeyError, TypeError, ValueError):
            continue
    return edits


def _mostly_heard(new_lines: list[str], heard_words: list[str]) -> bool:
    words = [w for line in new_lines for w in _content(_tokens(line))]
    if not words:
        return False
    found = sum(1 for w in words if any(_words_match(w, h) for h in heard_words))
    return found / len(words) >= _MIN_HEARD_FRACTION


def _describe(edit: Edit) -> str:
    span = f"line {edit.start}" if edit.start == edit.end else f"lines {edit.start}-{edit.end}"
    if edit.kind == "replace":
        return f"{span} replaced"
    if edit.kind == "delete":
        return f"{span} removed"
    n = len(edit.text)
    noun = "line" if n == 1 else "lines"
    return f"{n} {noun} added at the start" if edit.start == 0 else f"{n} {noun} added after line {edit.start}"


def apply_edits(
    lines: list[str], edits: list[Edit], heard_text: str, match: AudioMatch,
) -> tuple[list[str], list[str]]:
    """(new_lines, human-readable changes). Rejects any edit that is out of range, touches only
    lines the audio already supports, introduces words that were never heard, or overlaps an
    earlier accepted edit."""
    heard_words = list(dict.fromkeys(_content(_tokens(heard_text))))
    accepted: list[Edit] = []
    spans: list[tuple[float, float]] = []
    for edit in edits:
        if edit.kind == "insert":
            if not 0 <= edit.start <= len(lines) or not _mostly_heard(edit.text, heard_words):
                continue
            span = (edit.start + 0.5, edit.start + 0.5)
        else:
            if not 1 <= edit.start <= edit.end <= len(lines):
                continue
            touched = range(edit.start, edit.end + 1)
            if not any(i <= len(match.line_supported) and not match.line_supported[i - 1] for i in touched):
                continue  # every line here already matches the audio -- leave it alone
            if edit.kind == "replace" and not _mostly_heard(edit.text, heard_words):
                continue
            span = (float(edit.start), float(edit.end))
        if any(not (span[1] < s[0] or s[1] < span[0]) for s in spans):
            continue
        accepted.append(edit)
        spans.append(span)

    result = list(lines)
    for edit in sorted(accepted, key=lambda e: e.start, reverse=True):
        if edit.kind == "replace":
            result[edit.start - 1:edit.end] = edit.text
        elif edit.kind == "delete":
            del result[edit.start - 1:edit.end]
        else:
            result[edit.start:edit.start] = edit.text
    return result, [_describe(e) for e in sorted(accepted, key=lambda e: e.start)]


def build_reconcile_prompt(lines: list[str], match: AudioMatch, segments: list[dict]) -> str:
    numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    heard = "\n".join(f"[{int(s['start'])}s] {s['text']}" for s in segments)
    trouble = ", ".join(f"lines {a}-{b}" if a != b else f"line {a}" for a, b in match.unsupported_ranges) or "none"
    return (
        "A speech recognizer transcribed the isolated vocal track of a song, and an automatic check "
        "found that the lyric file below does not agree with what is sung. Propose the SMALLEST set of "
        "edits that makes the lyric file match what is sung.\n\n"
        f"LYRIC FILE (numbered lines):\n{numbered}\n\n"
        f"WHAT THE RECOGNIZER HEARD (transcript, with seconds into the song):\n{heard}\n\n"
        f"The check could not match these lyric lines to the audio: {trouble}. "
        f"Overall about {match.coverage:.0%} of the lyric words were heard.\n\n"
        "Rules:\n"
        "- Reply with ONLY a JSON object: {\"edits\": [...]}. No other text.\n"
        "- Each edit is one of: {\"op\": \"replace\", \"lines\": [first, last], \"text\": [new lines]}, "
        "{\"op\": \"insert\", \"after\": line_number, \"text\": [new lines]} (0 = at the very start), or "
        "{\"op\": \"delete\", \"lines\": [first, last]} (for lines that are not sung at all).\n"
        "- Only change lines the transcript actually contradicts, or add sung lines the file is missing. "
        "Leave every line that roughly agrees with the transcript exactly as it is.\n"
        "- Build any new or replacement text ONLY from words that appear in the transcript above. Never "
        "write lyrics from memory or from what you know of the song. The recognizer can mishear or "
        "misspell a word; fix an obvious mishearing, and tidy capitalization and punctuation, but do not "
        "invent words.\n"
        "- Keep lyric-line style (one sung line per entry). If you are not sure an edit is right, leave it "
        "out; {\"edits\": []} is a fine answer."
    )


def reconcile_lyrics(
    anthropic_client, lines: list[str], match: AudioMatch, segments: list[dict], model: str = "claude-sonnet-5",
) -> tuple[list[str], list[str]] | None:
    """(repaired lines, changes) or None when nothing usable came back. Never raises on a bad reply."""
    # Sonnet 5 thinks by default; the first version asked for max_tokens=2000 and the model spent
    # all of it reasoning about the line matching, returning no text (2026-09-19). Leave room for
    # the answer and cap the reasoning depth.
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": build_reconcile_prompt(lines, match, segments)}],
    )
    reply = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    edits = parse_edits(reply)
    if not edits:
        return None
    heard_text = " ".join(s["text"] for s in segments)
    new_lines, changes = apply_edits(lines, edits, heard_text, match)
    return (new_lines, changes) if changes else None
