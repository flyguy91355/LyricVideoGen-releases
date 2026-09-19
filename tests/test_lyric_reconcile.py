"""lyric_reconcile asks Claude for small EDITS that make lyric text agree with what Whisper
heard, then applies them in code -- so Claude never writes lyrics from memory and can only
introduce words that were actually heard. Song text is invented."""

import json
from types import SimpleNamespace

from lyricvideo.lyric_audio_match import score_lyrics_against_transcript
from lyricvideo.lyric_reconcile import Edit, apply_edits, build_reconcile_prompt, parse_edits, reconcile_lyrics

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
HEARD = " ".join(SUNG)
SEGMENTS = [{"start": i * 4.0, "end": i * 4.0 + 3.5, "text": line} for i, line in enumerate(SUNG)]


def match_for(lines):
    return score_lyrics_against_transcript(lines, HEARD)


class _FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


# --- parse_edits -------------------------------------------------------------------------

def test_parse_edits_reads_replace_insert_and_delete():
    reply = json.dumps({"edits": [
        {"op": "replace", "lines": [5, 8], "text": ["a", "b"]},
        {"op": "insert", "after": 12, "text": ["c"]},
        {"op": "delete", "lines": [20, 21]},
    ]})

    assert parse_edits(reply) == [
        Edit("replace", 5, 8, ["a", "b"]),
        Edit("insert", 12, 12, ["c"]),
        Edit("delete", 20, 21, []),
    ]


def test_parse_edits_finds_json_inside_chatter_and_code_fences():
    reply = 'Here you go:\n```json\n{"edits": [{"op": "delete", "lines": [3, 3]}]}\n```\nHope that helps.'

    assert parse_edits(reply) == [Edit("delete", 3, 3, [])]


def test_parse_edits_ignores_malformed_edits_and_garbage():
    assert parse_edits("I could not do that.") == []
    assert parse_edits('{"edits": [{"op": "explode", "lines": [1, 2]}, {"op": "replace", "lines": [9, 4], "text": ["x"]}]}') == []
    assert parse_edits('{"edits": "nope"}') == []


# --- apply_edits -------------------------------------------------------------------------

def test_a_wrong_block_is_replaced_with_words_that_were_heard():
    lyric = V1 + WRONG + V2
    edits = [Edit("replace", 5, 8, CHORUS)]

    new_lines, changes = apply_edits(lyric, edits, HEARD, match_for(lyric))

    assert new_lines == SUNG
    assert changes == ["lines 5-8 replaced"]


def test_replacement_text_that_was_never_heard_is_rejected():
    """Claude must not introduce words from memory: new text has to be in the transcript."""
    lyric = V1 + WRONG + V2
    invented = ["completely invented words nobody ever sang", "another invented line about elephants and bicycles"]

    new_lines, changes = apply_edits(lyric, [Edit("replace", 5, 6, invented)], HEARD, match_for(lyric))

    assert new_lines == lyric
    assert changes == []


def test_a_line_that_already_matches_the_audio_is_never_replaced_or_deleted():
    lyric = list(SUNG)  # every line matches

    new_lines, changes = apply_edits(lyric, [Edit("replace", 2, 3, CHORUS[:2]), Edit("delete", 6, 6, [])], HEARD, match_for(lyric))

    assert new_lines == lyric
    assert changes == []


def test_a_line_that_was_never_sung_can_be_deleted():
    lyric = V1 + WRONG[:1] + CHORUS  # one extra, never-sung line at position 5

    new_lines, changes = apply_edits(lyric, [Edit("delete", 5, 5, [])], HEARD, match_for(lyric))

    assert new_lines == V1 + CHORUS
    assert changes == ["line 5 removed"]


def test_a_missing_verse_is_inserted_after_the_right_line():
    lyric = V1 + V2  # the chorus is sung but missing from the file

    new_lines, changes = apply_edits(lyric, [Edit("insert", 4, 4, CHORUS)], HEARD, match_for(lyric))

    assert new_lines == SUNG
    assert changes == ["4 lines added after line 4"]


def test_several_edits_apply_without_shifting_each_others_line_numbers():
    lyric = WRONG + V1 + WRONG[:1] + V2  # bad block 1-4, extra line 9
    edits = [Edit("replace", 1, 4, CHORUS), Edit("delete", 9, 9, [])]

    new_lines, changes = apply_edits(lyric, edits, HEARD, match_for(lyric))

    assert new_lines == CHORUS + V1 + V2
    assert len(changes) == 2


def test_overlapping_edits_keep_only_the_first():
    lyric = V1 + WRONG + V2

    new_lines, changes = apply_edits(
        lyric, [Edit("replace", 5, 8, CHORUS), Edit("replace", 6, 7, CHORUS[:2])], HEARD, match_for(lyric),
    )

    assert new_lines == SUNG
    assert changes == ["lines 5-8 replaced"]


def test_out_of_range_edits_are_ignored():
    lyric = V1 + WRONG

    new_lines, changes = apply_edits(lyric, [Edit("delete", 50, 60, [])], HEARD, match_for(lyric))

    assert new_lines == lyric and changes == []


# --- reconcile_lyrics --------------------------------------------------------------------

def test_reconcile_sends_the_numbered_lyrics_and_the_transcript_and_applies_the_reply():
    lyric = V1 + WRONG + V2
    reply = json.dumps({"edits": [{"op": "replace", "lines": [5, 8], "text": CHORUS}]})
    client = _FakeClient(reply)

    result = reconcile_lyrics(client, lyric, match_for(lyric), SEGMENTS)

    assert result == (SUNG, ["lines 5-8 replaced"])
    prompt = client.prompts[0]
    assert "5. neon signs are flashing down the avenue" in prompt      # numbered lyric lines
    assert "carry me home across the silver sea" in prompt            # what was heard
    assert "lines 5-8" in prompt                                      # where the check found trouble


def test_reconcile_returns_none_when_claude_proposes_nothing_usable():
    lyric = V1 + WRONG + V2

    assert reconcile_lyrics(_FakeClient('{"edits": []}'), lyric, match_for(lyric), SEGMENTS) is None
    assert reconcile_lyrics(_FakeClient("Sorry, I can't."), lyric, match_for(lyric), SEGMENTS) is None


def test_the_prompt_forbids_writing_lyrics_from_memory():
    prompt = build_reconcile_prompt(V1 + WRONG, match_for(V1 + WRONG), SEGMENTS)

    assert "only" in prompt.lower() and "transcript" in prompt.lower()
    assert "memory" in prompt.lower()


def test_the_repair_request_leaves_room_for_the_answer_after_the_models_reasoning():
    """Real finding, 2026-09-19: with max_tokens=2000 Claude Sonnet 5 (thinking on by default)
    spent the ENTIRE budget reasoning about the line matching and returned no text at all,
    so every repair silently came back empty."""
    seen = {}

    class _Recording(_FakeClient):
        def _create(self, **kwargs):
            seen.update(kwargs)
            return super()._create(**kwargs)

    client = _Recording('{"edits": []}')
    client.messages = SimpleNamespace(create=client._create)
    lyric = V1 + WRONG + V2

    reconcile_lyrics(client, lyric, match_for(lyric), SEGMENTS)

    assert seen["max_tokens"] >= 12000
    assert seen["output_config"] == {"effort": "medium"}
