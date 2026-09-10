from lyricvideo.youtube_metadata import draft_comment_reply, generate_video_metadata


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_generate_video_metadata_parses_all_three_labeled_fields():
    client = _FakeAnthropicClient(
        "TITLE: Wish You Were Here - Play Along\n"
        "DESCRIPTION: A wistful song about absence and longing.\n"
        "TAGS: pink floyd, play along, guitar chords, lyrics video"
    )

    title, description, tags = generate_video_metadata(client, "Wish You Were Here", "lyrics here")

    assert title == "Wish You Were Here - Play Along"
    assert description == "A wistful song about absence and longing."
    assert tags == ["pink floyd", "play along", "guitar chords", "lyrics video"]


def test_generate_video_metadata_falls_back_to_song_title_if_title_missing():
    client = _FakeAnthropicClient("DESCRIPTION: Some description\nTAGS: tag1")

    title, _description, _tags = generate_video_metadata(client, "Original Title", "lyrics")

    assert title == "Original Title"


def test_generate_video_metadata_tolerates_reordered_labels():
    client = _FakeAnthropicClient("TAGS: a, b\nTITLE: My Title\nDESCRIPTION: My description")

    title, description, tags = generate_video_metadata(client, "fallback", "lyrics")

    assert title == "My Title"
    assert "My description" in description
    assert tags == ["a", "b"]


def test_draft_comment_reply_detects_error_report():
    client = _FakeAnthropicClient(
        "IS_ERROR_REPORT: YES\nREPLY: Thanks for catching that, I'll take a look!"
    )

    reply, is_error_report = draft_comment_reply(client, "the chord at 1:30 looks wrong", "My Song")

    assert is_error_report is True
    assert reply == "Thanks for catching that, I'll take a look!"


def test_draft_comment_reply_detects_non_error_comment():
    client = _FakeAnthropicClient("IS_ERROR_REPORT: NO\nREPLY: Glad you liked it!")

    reply, is_error_report = draft_comment_reply(client, "great video!", "My Song")

    assert is_error_report is False
    assert reply == "Glad you liked it!"
