import json

import pytest

from lyricvideo.vision_parse import (
    map_chords_with_vision,
    split_lyrics_text,
    VisionParseError,
)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeStream:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return _FakeResponse(self._text)


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def stream(self, **kwargs):
        return _FakeStream(self._text)


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


class _FakeMultiCallMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def stream(self, **kwargs):
        text = self._responses[self.call_count]
        self.call_count += 1
        return _FakeStream(text)


class _FlakyContentFilterMessages:
    """Raises a content-filter error N times, then succeeds -- simulates the
    confirmed-non-deterministic real API behavior (same request, different
    outcome on retry)."""

    def __init__(self, text, fail_times):
        self._text = text
        self._fail_times = fail_times
        self.call_count = 0

    def stream(self, **kwargs):
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise RuntimeError(
                "{'type': 'error', 'error': {'type': 'invalid_request_error', "
                "'message': 'Output blocked by content filtering policy'}}"
            )
        return _FakeStream(self._text)


class _FlakyContentFilterClient:
    def __init__(self, text, fail_times):
        self.messages = _FlakyContentFilterMessages(text, fail_times)


class _AlwaysErrorsMessages:
    def __init__(self, message):
        self._message = message
        self.call_count = 0

    def stream(self, **kwargs):
        self.call_count += 1
        raise RuntimeError(self._message)


class _AlwaysErrorsClient:
    def __init__(self, message):
        self.messages = _AlwaysErrorsMessages(message)


class _FakeMultiCallClient:
    """Returns a different canned response per call, in order -- for verifying
    batched calls each get their own request and results are aggregated."""

    def __init__(self, responses):
        self.messages = _FakeMultiCallMessages(responses)


def test_split_lyrics_text_drops_blank_lines_and_section_labels():
    text = "[Verse 1]\nSo, so you think you can tell\n\n(Chorus)\nHeaven from Hell\n"
    assert split_lyrics_text(text) == [
        "So, so you think you can tell",
        "Heaven from Hell",
    ]


def test_map_chords_with_vision_never_asked_to_reproduce_lyrics_in_response():
    payload = json.dumps({"line_chords": [[[0, "G"], [2, "D"]], []], "instrumental_blocks": []})
    client = _FakeAnthropicClient(text=payload)

    lines, instrumental_blocks = map_chords_with_vision(
        client,
        pdf_path=None,
        lyric_lines=["hello there my friend", "goodbye now"],
        page_images=[b"fake-png-bytes"],
    )

    assert len(lines) == 2
    assert [w.word for w in lines[0].words] == ["hello", "there", "my", "friend"]
    assert lines[0].words[0].chord == "G"
    assert lines[0].words[1].chord is None
    assert lines[0].words[2].chord == "D"
    assert lines[1].words[0].chord is None
    assert instrumental_blocks == []


def test_map_chords_with_vision_retries_transient_content_filter_error(monkeypatch):
    monkeypatch.setattr("lyricvideo.vision_parse.CONTENT_FILTER_RETRY_DELAY_SECONDS", 0.0)
    payload = json.dumps({"line_chords": [[[0, "G"]]], "instrumental_blocks": []})
    client = _FlakyContentFilterClient(payload, fail_times=2)

    lines, blocks = map_chords_with_vision(
        client, pdf_path=None, lyric_lines=["a line"], page_images=[b"x"],
    )

    assert client.messages.call_count == 3  # 2 failures + 1 success
    assert len(lines) == 1
    assert lines[0].words[0].chord == "G"


def test_map_chords_with_vision_gives_up_after_max_attempts(monkeypatch):
    from lyricvideo.vision_parse import CONTENT_FILTER_MAX_ATTEMPTS

    monkeypatch.setattr("lyricvideo.vision_parse.CONTENT_FILTER_RETRY_DELAY_SECONDS", 0.0)
    client = _FlakyContentFilterClient("irrelevant", fail_times=99)

    with pytest.raises(RuntimeError, match="content filtering policy"):
        map_chords_with_vision(client, pdf_path=None, lyric_lines=["a line"], page_images=[b"x"])

    assert client.messages.call_count == CONTENT_FILTER_MAX_ATTEMPTS


def test_map_chords_with_vision_does_not_retry_unrelated_errors():
    client = _AlwaysErrorsClient("some unrelated network error")

    with pytest.raises(RuntimeError, match="unrelated network error"):
        map_chords_with_vision(client, pdf_path=None, lyric_lines=["a line"], page_images=[b"x"])

    assert client.messages.call_count == 1  # no wasted retries on a non-transient error


def test_map_chords_with_vision_batches_long_songs_into_multiple_calls():
    # 5 lines with max_lines_per_call=2 -> batches of [0,1], [2,3], [4] = 3 calls
    batch1 = json.dumps(
        {"line_chords": [[[0, "G"]], []], "instrumental_blocks": [{"before_line_index": 0, "chords": ["Em"]}]}
    )
    batch2 = json.dumps({"line_chords": [[[0, "D"]], []], "instrumental_blocks": []})
    batch3 = json.dumps(
        {"line_chords": [[]], "instrumental_blocks": [{"before_line_index": 1, "chords": ["C"]}]}
    )
    client = _FakeMultiCallClient([batch1, batch2, batch3])

    lines, instrumental_blocks = map_chords_with_vision(
        client,
        pdf_path=None,
        lyric_lines=["line a", "line b", "line c", "line d", "line e"],
        page_images=[b"fake-png-bytes"],
        max_lines_per_call=2,
    )

    assert client.messages.call_count == 3
    assert len(lines) == 5
    assert lines[0].words[0].chord == "G"
    assert lines[2].words[0].chord == "D"

    # instrumental block indices must be offset by their batch's start position:
    # batch1's index 0 -> global 0; batch3's index 1 -> global 4 (batch_start=4) + 1
    assert instrumental_blocks[0].before_line_index == 0
    assert instrumental_blocks[0].chords == ["Em"]
    assert instrumental_blocks[1].before_line_index == 5
    assert instrumental_blocks[1].chords == ["C"]


def test_map_chords_with_vision_extracts_instrumental_blocks():
    payload = json.dumps(
        {
            "line_chords": [[], []],
            "instrumental_blocks": [
                {"before_line_index": 0, "chords": ["Em7", "G", "Em7", "G"]},
                {"before_line_index": 2, "chords": ["G"]},
            ],
        }
    )
    client = _FakeAnthropicClient(text=payload)

    lines, instrumental_blocks = map_chords_with_vision(
        client,
        pdf_path=None,
        lyric_lines=["hello there", "goodbye now"],
        page_images=[b"fake-png-bytes"],
    )

    assert len(instrumental_blocks) == 2
    assert instrumental_blocks[0].before_line_index == 0
    assert instrumental_blocks[0].chords == ["Em7", "G", "Em7", "G"]
    assert instrumental_blocks[1].before_line_index == 2
    assert instrumental_blocks[1].chords == ["G"]


def test_map_chords_with_vision_raises_on_non_json_response():
    client = _FakeAnthropicClient(text="Sorry, I can't read this image.")

    with pytest.raises(VisionParseError):
        map_chords_with_vision(client, pdf_path=None, lyric_lines=["a b"], page_images=[b"x"])


def test_map_chords_with_vision_raises_on_line_count_mismatch():
    payload = json.dumps({"line_chords": [[]], "instrumental_blocks": []})  # 1 entry but 2 lines given
    client = _FakeAnthropicClient(text=payload)

    with pytest.raises(VisionParseError):
        map_chords_with_vision(
            client, pdf_path=None, lyric_lines=["a b", "c d"], page_images=[b"x"]
        )


def test_map_chords_with_vision_raises_on_no_lyric_lines():
    client = _FakeAnthropicClient(text=json.dumps({"line_chords": [], "instrumental_blocks": []}))

    with pytest.raises(VisionParseError):
        map_chords_with_vision(client, pdf_path=None, lyric_lines=[], page_images=[b"x"])


def test_map_chords_with_vision_raises_on_no_page_images():
    client = _FakeAnthropicClient(text=json.dumps({"line_chords": [], "instrumental_blocks": []}))

    with pytest.raises(VisionParseError):
        map_chords_with_vision(client, pdf_path=None, lyric_lines=["a b"], page_images=[])
