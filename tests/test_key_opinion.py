from types import SimpleNamespace

from lyricvideo.key_opinion import ask_published_key

CANDS = ["D major", "B minor", "A major", "G major"]
CHORDS = "D 48%, G 26%, A 11%, Em 10%"


class FakeClient:
    """messages.create returns the queued replies in order; an Exception in the queue is raised instead."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        blocks = [] if reply is None else [SimpleNamespace(type="text", text=reply)]
        return SimpleNamespace(content=blocks)


def ask(client, cands=CANDS, title="All for Love", artist="Bryan Adams"):
    return ask_published_key(client, title, artist, cands, CHORDS)


def test_returns_the_chosen_candidate():
    assert ask(FakeClient("KEY: D major")) == "D major"


def test_spelling_differences_still_match_a_candidate():
    assert ask(FakeClient("KEY: Bm"), CANDS) == "B minor"
    assert ask(FakeClient("KEY: A# major"), ["Bb major", "G minor"]) == "Bb major"


def test_a_key_that_is_not_one_of_the_candidates_gives_none():
    assert ask(FakeClient("KEY: F major", "KEY: F major", "KEY: F major")) is None


def test_none_gives_none_without_retrying():
    client = FakeClient("KEY: NONE")
    assert ask(client) is None
    assert len(client.calls) == 1


def test_a_reply_without_a_key_line_is_retried_then_gives_none():
    client = FakeClient("I think it is D.", None, "no idea")
    assert ask(client) is None
    assert len(client.calls) == 3


def test_a_good_reply_after_a_bad_one_is_used():
    assert ask(FakeClient("hmm", "KEY: G major")) == "G major"


def test_a_failing_call_gives_none_and_never_raises():
    client = FakeClient(RuntimeError("down"), RuntimeError("down"), RuntimeError("down"))
    assert ask(client) is None


def test_no_candidates_means_no_call():
    client = FakeClient("KEY: D major")
    assert ask(client, []) is None
    assert client.calls == []


def test_the_prompt_lists_the_song_the_chords_and_every_candidate_and_thinking_is_off():
    client = FakeClient("KEY: D major")
    ask(client)
    call = client.calls[0]
    prompt = call["messages"][0]["content"]
    assert "All for Love" in prompt and "Bryan Adams" in prompt and CHORDS in prompt
    assert all(c in prompt for c in CANDS)
    assert call["thinking"] == {"type": "disabled"}
    assert call["max_tokens"] >= 100


def test_a_missing_artist_is_still_asked():
    client = FakeClient("KEY: D major")
    assert ask(client, artist="") == "D major"
