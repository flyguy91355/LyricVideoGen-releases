"""deep_review.research: finds the real lyrics for a song diagnosed LYRICS_WRONG, using a real web search
(cited sources, not Claude's own memory of the song -- lyric_reconcile.py's docstring explains why memory
alone is unsafe) cross-referenced against what's actually heard in the recording. Song text is invented."""

import json
from types import SimpleNamespace

import pytest

from deep_review.research import Research, build_research_prompt, parse_research_reply, research_lyrics

CURRENT_LINES = [
    "the river runs beside the mill",
    "and morning fog lies on the hill",
]
SEGMENTS = [
    {"start": 10.0, "end": 13.0, "text": "the river runs beside the mill"},
    {"start": 18.0, "end": 21.5, "text": "neon signs flash downtown"},
]


class _FakeClient:
    def __init__(self, reply, input_tokens=1000, output_tokens=200):
        self.reply, self.calls = reply, []
        self.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)], usage=self.usage)


def research_json(lines, citations=("genius.com/some-song-lyrics",), confidence="high", notes=""):
    return json.dumps({"lines": lines, "citations": list(citations), "confidence": confidence, "notes": notes})


# --- parse_research_reply ----------------------------------------------------------------

def test_a_well_formed_reply_parses_into_a_research_result():
    reply = research_json(["corrected line one", "corrected line two"], notes="matches the album version")

    result = parse_research_reply(reply)

    assert result.lines == ["corrected line one", "corrected line two"]
    assert result.citations == ["genius.com/some-song-lyrics"]
    assert result.confidence == "high"
    assert result.notes == "matches the album version"


def test_json_inside_chatter_and_search_narration_is_found():
    reply = "Let me search for this.\n\nI found it on Genius.\n```json\n" + research_json(["a line"]) + "\n```"

    assert parse_research_reply(reply).lines == ["a line"]


def test_garbage_or_missing_lines_gives_none():
    assert parse_research_reply("I couldn't find anything reliable.") is None
    assert parse_research_reply(json.dumps({"citations": [], "confidence": "low"})) is None
    assert parse_research_reply(json.dumps({"lines": "not a list", "confidence": "low"})) is None


def test_an_unrecognized_confidence_word_defaults_to_low():
    reply = json.dumps({"lines": ["a line"], "citations": [], "confidence": "pretty sure!"})

    assert parse_research_reply(reply).confidence == "low"


def test_empty_lines_list_is_rejected_as_unusable():
    assert parse_research_reply(research_json([])) is None


# --- build_research_prompt ----------------------------------------------------------------

def test_the_prompt_includes_the_song_the_current_lyrics_and_what_was_heard_and_asks_for_citations():
    prompt = build_research_prompt("Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert "Test Song" in prompt and "Test Artist" in prompt
    assert "the river runs beside the mill" in prompt
    assert "neon signs flash downtown" in prompt
    assert "citations" in prompt.lower()
    assert "edition" in prompt.lower() or "version" in prompt.lower()


def test_the_prompt_never_asks_claude_to_invent_from_memory():
    prompt = build_research_prompt("Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert "search" in prompt.lower()
    assert "do not invent" in prompt.lower() or "never invent" in prompt.lower()


def test_the_prompt_can_point_at_specific_mismatched_lines():
    prompt = build_research_prompt("Test Song", "Test Artist", CURRENT_LINES, SEGMENTS, mismatched_lines=[2])

    assert "line 2" in prompt.lower() or "lines: 2" in prompt.lower() or "line 2" in prompt


def test_a_retry_shows_what_earlier_attempts_tried_and_says_not_to_repeat_them():
    """Owner, 2026-09-22: retries must not be identical -- a retry has to know what already failed."""
    previous = [{"lines": ["a first guess line"], "outcome": "still only 75% in sync, lines 3-4 off"}]

    prompt = build_research_prompt("Test Song", "Test Artist", CURRENT_LINES, SEGMENTS, previous_attempts=previous)

    assert "a first guess line" in prompt
    assert "75%" in prompt
    assert "not" in prompt.lower() and "repeat" in prompt.lower()


def test_no_previous_attempts_means_no_retry_section_in_the_prompt():
    prompt = build_research_prompt("Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert "earlier attempt" not in prompt.lower()


def test_multiple_previous_attempts_are_all_shown():
    previous = [
        {"lines": ["first guess"], "outcome": "still 60%"},
        {"lines": ["second guess"], "outcome": "still 80%, closer"},
    ]

    prompt = build_research_prompt("Test Song", "Test Artist", CURRENT_LINES, SEGMENTS, previous_attempts=previous)

    assert "first guess" in prompt and "second guess" in prompt
    assert "still 60%" in prompt and "still 80%, closer" in prompt


# --- research_lyrics ----------------------------------------------------------------------

def test_research_lyrics_enables_web_search_and_returns_the_parsed_result():
    client = _FakeClient(research_json(["a corrected line"], citations=["azlyrics.com/x"]))

    attempt = research_lyrics(client, "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert attempt.research.lines == ["a corrected line"]
    call = client.calls[0]
    tool_types = [t["type"] for t in call["tools"]]
    assert any(t.startswith("web_search") for t in tool_types)


def test_research_lyrics_returns_none_research_for_an_unusable_reply():
    attempt = research_lyrics(_FakeClient("no idea"), "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert attempt.research is None


def test_research_lyrics_passes_through_the_model_and_a_search_use_limit():
    client = _FakeClient(research_json(["a line"]))

    research_lyrics(client, "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS, model="claude-sonnet-5", max_searches=2)

    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["tools"][0]["max_uses"] == 2


def test_the_default_search_limit_is_three_not_five():
    """Owner, 2026-09-22: real measured cost on "dreams" was $0.26/attempt, driven by web search results
    compounding across multiple search rounds within one call -- cut the default max_uses to reduce that
    compounding, trading some cross-referencing breadth for lower cost per attempt."""
    client = _FakeClient(research_json(["a line"]))

    research_lyrics(client, "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert client.calls[0]["tools"][0]["max_uses"] == 3


def test_research_lyrics_includes_previous_attempts_in_the_prompt_it_sends():
    client = _FakeClient(research_json(["a line"]))
    previous = [{"lines": ["a first guess"], "outcome": "still only 75% in sync"}]

    research_lyrics(client, "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS, previous_attempts=previous)

    sent_prompt = client.calls[0]["messages"][0]["content"]
    assert "a first guess" in sent_prompt and "75%" in sent_prompt


# --- real, measured cost -- owner, 2026-09-22: "whats the estimated cost of this?" deserves a real answer,
# not a guess, once the call has actually been made -------------------------------------------------------

def test_every_call_reports_its_real_dollar_cost_from_the_responses_own_usage():
    """Sonnet 5 pricing: $2/1M input, $10/1M output (claude-api skill rate table)."""
    client = _FakeClient(research_json(["a line"]), input_tokens=10_000, output_tokens=1_000)

    attempt = research_lyrics(client, "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    expected = 10_000 * (2.00 / 1_000_000) + 1_000 * (10.00 / 1_000_000)
    assert attempt.cost_usd == pytest.approx(expected)


def test_an_unusable_reply_still_reports_the_real_cost_the_call_made():
    """The API call happened and cost money even though the reply itself couldn't be used."""
    client = _FakeClient("no idea", input_tokens=5_000, output_tokens=500)

    attempt = research_lyrics(client, "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert attempt.research is None
    assert attempt.cost_usd == pytest.approx(5_000 * (2.00 / 1_000_000) + 500 * (10.00 / 1_000_000))


def test_missing_or_malformed_usage_reports_zero_cost_rather_than_crashing():
    client = _FakeClient(research_json(["a line"]))
    client.usage = None

    attempt = research_lyrics(client, "Test Song", "Test Artist", CURRENT_LINES, SEGMENTS)

    assert attempt.research is not None
    assert attempt.cost_usd == 0.0
