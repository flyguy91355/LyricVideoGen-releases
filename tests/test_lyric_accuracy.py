from lyricvideo.lyric_accuracy import check_lyric_accuracy


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
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(self._text)

    def prompt_text(self) -> str:
        return self.last_kwargs["messages"][0]["content"]


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_check_lyric_accuracy_true_when_claude_says_yes():
    client = _FakeAnthropicClient("LOOKS_ACCURATE: YES\nCONCERN:")

    looks_accurate, concern = check_lyric_accuracy(client, "Imagine", "John Lennon", ["Imagine there's no heaven"])

    assert looks_accurate is True
    assert concern == ""


def test_check_lyric_accuracy_false_with_a_concern_when_claude_says_no():
    client = _FakeAnthropicClient(
        "LOOKS_ACCURATE: NO\nCONCERN: This text is in French, not English, and doesn't match the song."
    )

    looks_accurate, concern = check_lyric_accuracy(client, "Imagine", "John Lennon", ["Ceci n'est pas une chanson"])

    assert looks_accurate is False
    assert concern == "This text is in French, not English, and doesn't match the song."


def test_check_lyric_accuracy_includes_title_artist_and_lines_in_the_prompt():
    client = _FakeAnthropicClient("LOOKS_ACCURATE: YES\nCONCERN:")

    check_lyric_accuracy(client, "Imagine", "John Lennon", ["Imagine there's no heaven", "It's easy if you try"])

    prompt = client.messages.prompt_text()
    assert "Imagine" in prompt
    assert "John Lennon" in prompt
    assert "Imagine there's no heaven" in prompt


def test_check_lyric_accuracy_tolerates_reordered_labels():
    client = _FakeAnthropicClient("CONCERN: wrong song\nLOOKS_ACCURATE: NO")

    looks_accurate, concern = check_lyric_accuracy(client, "Imagine", "John Lennon", ["some text"])

    assert looks_accurate is False
    assert concern == "wrong song"
