"""lyric_arbiter asks Claude to JUDGE each stretch the audio check couldn't match -- are the lyric
lines wrong for this recording, or did the speech recognizer just mishear/loop/miss? -- instead of
copying the transcript (which measurably replaced correct lyrics with Whisper's mishearings).
Song text is invented."""

import json
from types import SimpleNamespace

from lyricvideo.lyric_arbiter import arbitrate, build_arbiter_prompt, describe_arbitration, parse_arbitration
from lyricvideo.lyric_audio_match import score_lyrics_against_transcript

V1 = [
    "the river runs beside the old stone mill",
    "and morning fog lies heavy on the hill",
    "a lantern swings above the wooden door",
    "i wait for you like i have waited before",
]
CHORUS = [
    "carry me home across the silver sea",
    "carry me home where i long to be",
    "the stars will guide us through the night",
    "carry me home to the morning light",
]
V2 = [
    "the winter came and covered every road",
    "we traded all our dreams for heavy loads",
    "a whisper crossed the empty market square",
    "and told me you were waiting for me there",
]
WRONG = [
    "neon signs are flashing down the avenue",
    "every stranger's face looks like a stranger's face to you",
    "dance until the sunrise turns the pavement gold",
    "nobody will ever tell the story we were told",
]
SUNG = V1 + CHORUS + V2
LYRICS = V1 + WRONG + V2                    # lines 5-8 don't match
SEGMENTS = [{"start": i * 4.0, "end": i * 4.0 + 3.5, "text": t} for i, t in enumerate(SUNG)]
MATCH = score_lyrics_against_transcript(LYRICS, " ".join(SUNG))
RANGES = [(5, 8)]


class _FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def verdict_json(*ranges, missing=False, note=""):
    return json.dumps({
        "ranges": [{"lines": [a, b], "verdict": v, "reason": r} for a, b, v, r in ranges],
        "missing_section": missing, "missing_note": note,
    })


# --- parse_arbitration -------------------------------------------------------------------

def test_every_range_judged_a_recognizer_error_confirms_the_lyrics():
    reply = verdict_json((5, 8, "recognizer_error", "transcript there is a loop of nonsense"))

    result = parse_arbitration(reply, RANGES)

    assert result.confirmed is True
    assert result.verdicts[0].verdict == "recognizer_error"


def test_one_range_judged_wrong_means_the_lyrics_are_not_confirmed():
    reply = verdict_json((5, 8, "lyrics_wrong", "the audio has a different, coherent verse here"))

    result = parse_arbitration(reply, RANGES)

    assert result.confirmed is False
    assert "different, coherent verse" in describe_arbitration(result)
    assert "lines 5-8" in describe_arbitration(result)


def test_unsure_is_not_confirmation():
    assert parse_arbitration(verdict_json((5, 8, "unsure", "can't tell")), RANGES).confirmed is False


def test_an_unknown_verdict_word_counts_as_unsure():
    assert parse_arbitration(verdict_json((5, 8, "probably fine", "")), RANGES).confirmed is False


def test_a_range_the_reply_forgot_to_judge_is_not_confirmed():
    reply = verdict_json((5, 8, "recognizer_error", "loop"))

    result = parse_arbitration(reply, [(5, 8), (12, 12)])

    assert result.confirmed is False


def test_a_missing_sung_section_blocks_confirmation_even_if_every_range_is_a_recognizer_error():
    reply = verdict_json((5, 8, "recognizer_error", "loop"), missing=True, note="a whole verse about a winter road")

    result = parse_arbitration(reply, RANGES)

    assert result.confirmed is False
    assert "winter road" in describe_arbitration(result)


def test_json_inside_chatter_is_found_and_garbage_gives_none():
    reply = "Sure!\n```json\n" + verdict_json((5, 8, "recognizer_error", "loop")) + "\n```"

    assert parse_arbitration(reply, RANGES).confirmed is True
    assert parse_arbitration("I don't know.", RANGES) is None
    assert parse_arbitration('{"ranges": "nope"}', RANGES) is None


def test_a_verdict_range_that_only_overlaps_the_asked_range_still_counts():
    reply = verdict_json((4, 9, "recognizer_error", "loop"))

    assert parse_arbitration(reply, RANGES).confirmed is True


# --- prompt + arbitrate ------------------------------------------------------------------

def test_the_prompt_shows_the_lyrics_the_transcript_and_the_trouble_spots_and_forbids_rewriting():
    prompt = build_arbiter_prompt(LYRICS, MATCH, SEGMENTS)

    assert "5. neon signs are flashing down the avenue" in prompt
    assert "carry me home across the silver sea" in prompt          # what was heard
    assert "lines 5-8" in prompt
    assert "recognizer_error" in prompt and "lyrics_wrong" in prompt
    assert "do not write or rewrite" in prompt.lower()


def test_arbitrate_asks_claude_with_room_to_answer_and_returns_the_judgement():
    client = _FakeClient(verdict_json((5, 8, "recognizer_error", "loop")))

    result = arbitrate(client, LYRICS, MATCH, SEGMENTS)

    assert result.confirmed is True
    call = client.calls[0]
    assert call["max_tokens"] >= 12000                # Sonnet 5 reasons first; leave room for the JSON
    assert call["output_config"] == {"effort": "medium"}


def test_arbitrate_returns_none_for_an_unusable_reply():
    assert arbitrate(_FakeClient("no idea"), LYRICS, MATCH, SEGMENTS) is None


def test_arbitrate_has_nothing_to_judge_for_a_fully_matching_song():
    matching = score_lyrics_against_transcript(SUNG, " ".join(SUNG))
    client = _FakeClient("{}")

    assert arbitrate(client, SUNG, matching, SEGMENTS) is None
    assert client.calls == []
