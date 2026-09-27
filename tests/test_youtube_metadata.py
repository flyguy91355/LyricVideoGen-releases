from lyricvideo.youtube_metadata import (
    build_easy_chord_title,
    build_play_along_title,
    classify_genre,
    draft_comment_reply,
    draft_engagement_comment,
    generate_video_metadata,
)


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

    def _prompt_text(self) -> str:
        return self.last_kwargs["messages"][0]["content"]


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_build_play_along_title_with_known_artist():
    assert (
        build_play_along_title("November Rain", "Guns N' Roses")
        == "November Rain - Guns N' Roses - (Play Along Lyrics & Chords)"
    )


def test_build_play_along_title_without_artist():
    assert build_play_along_title("Some Song", "") == "Some Song - (Play Along Lyrics & Chords)"


def test_build_easy_chord_title_with_known_artist():
    assert (
        build_easy_chord_title("Bridge Over Troubled Water", "Simon and Garfunkel", 1)
        == "Bridge Over Troubled Water - Simon and Garfunkel - (EASY CHORDS Play Along - Capo 1)"
    )


def test_build_easy_chord_title_without_artist():
    assert build_easy_chord_title("Some Song", "", 2) == "Some Song - (EASY CHORDS Play Along - Capo 2)"


def test_generate_video_metadata_parses_description_and_tags():
    client = _FakeAnthropicClient(
        "DESCRIPTION: A wistful song about absence and longing.\n"
        "TAGS: pink floyd, play along, guitar chords, lyrics video"
    )

    title, description, tags = generate_video_metadata(
        client, "Wish You Were Here", artist="Pink Floyd", full_lyrics="lyrics here",
    )

    assert title == "Wish You Were Here - Pink Floyd - (Play Along Lyrics & Chords)"
    assert description == "A wistful song about absence and longing."
    assert tags == ["pink floyd", "play along", "guitar chords", "lyrics video"]


def test_generate_video_metadata_title_is_deterministic_not_claude_authored():
    # Even if Claude's reply included a TITLE line, it's ignored -- the real
    # title is always the fixed build_play_along_title() pattern now.
    client = _FakeAnthropicClient("TITLE: Some Claude-Written Title\nDESCRIPTION: Some description\nTAGS: tag1")

    title, _description, _tags = generate_video_metadata(client, "Original Title", artist="", full_lyrics="lyrics")

    assert title == "Original Title - (Play Along Lyrics & Chords)"


def test_generate_video_metadata_tolerates_reordered_labels():
    client = _FakeAnthropicClient("TAGS: a, b\nDESCRIPTION: My description")

    title, description, tags = generate_video_metadata(client, "fallback", artist="", full_lyrics="lyrics")

    assert title == "fallback - (Play Along Lyrics & Chords)"
    assert "My description" in description
    assert tags == ["a", "b"]


def test_generate_video_metadata_states_the_real_artist_in_the_prompt_when_known():
    client = _FakeAnthropicClient("DESCRIPTION: d\nTAGS: a")

    generate_video_metadata(client, "Wish You Were Here", artist="Pink Floyd", full_lyrics="lyrics here")

    assert "Pink Floyd" in client.messages._prompt_text()


def test_generate_video_metadata_never_invents_an_artist_when_unknown():
    client = _FakeAnthropicClient("DESCRIPTION: d\nTAGS: a")

    generate_video_metadata(client, "Some Song", artist="", full_lyrics="lyrics here")

    assert "performed by" not in client.messages._prompt_text().lower()


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


def test_classify_genre_returns_the_parsed_genre():
    client = _FakeAnthropicClient("GENRE: Classic Rock")

    genre = classify_genre(client, "Free Bird", "Lynyrd Skynyrd", "lyrics here", known_genres=["Classic Rock"])

    assert genre == "Classic Rock"


def test_classify_genre_includes_every_known_genre_in_the_prompt():
    client = _FakeAnthropicClient("GENRE: Classic Rock")

    classify_genre(client, "Free Bird", "Lynyrd Skynyrd", "lyrics here", known_genres=["Classic Rock", "Country"])

    prompt = client.messages._prompt_text()
    assert "Classic Rock" in prompt
    assert "Country" in prompt


def test_classify_genre_can_propose_a_genre_not_in_the_known_list():
    """The list is a starting point, not a hard cap -- a genuinely new
    genre gets minted rather than forced into an ill-fitting bucket."""
    client = _FakeAnthropicClient("GENRE: Bluegrass")

    genre = classify_genre(client, "Some Song", "Some Artist", "lyrics", known_genres=["Classic Rock"])

    assert genre == "Bluegrass"


def test_draft_engagement_comment_returns_the_parsed_text():
    client = _FakeAnthropicClient("COMMENT: Which instrument are you playing along with?")

    comment = draft_engagement_comment(client, "Free Bird")

    assert comment == "Which instrument are you playing along with?"


def test_draft_engagement_comment_mentions_the_song_title_in_the_prompt():
    client = _FakeAnthropicClient("COMMENT: Nice!")

    draft_engagement_comment(client, "Free Bird")

    assert "Free Bird" in client.messages._prompt_text()


# --- a reply that isn't the two labelled lines must never become a blank YouTube description ----------------
# Real incident, 2026-09-25: for "Blackbird" Claude sometimes answered with a paragraph ("I should clarify
# something important: the lyrics you've provided don't match...") instead of DESCRIPTION:/TAGS:, which parsed
# to "" and "" and was uploaded as a video with no description and no tags. Other times default adaptive thinking
# used the whole 300-token budget and returned no text block at all.

class _ScriptedMessages:
    """One scripted reply per call: a str is a plain text reply, a list is used as the response's content blocks."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, str):
            return _FakeResponse(reply)
        return type("Response", (), {"content": reply})()


class _ScriptedClient:
    def __init__(self, replies):
        self.messages = _ScriptedMessages(replies)


_PROSE = "I should clarify something important: the lyrics you've provided don't match the actual song..."
_GOOD = "DESCRIPTION: A gentle acoustic song about hope.\nTAGS: acoustic, beatles, fingerstyle"


def test_generate_video_metadata_retries_a_reply_with_no_labelled_fields():
    client = _ScriptedClient([_PROSE, _GOOD])

    title, description, tags = generate_video_metadata(client, "Blackbird", "The Beatles", "lyrics")

    assert description == "A gentle acoustic song about hope."
    assert tags == ["acoustic", "beatles", "fingerstyle"]
    assert len(client.messages.calls) == 2


def test_generate_video_metadata_retries_a_reply_with_a_description_but_no_tags():
    client = _ScriptedClient(["DESCRIPTION: Only half of it.", _GOOD])

    _title, description, tags = generate_video_metadata(client, "Blackbird", "The Beatles", "lyrics")

    assert description == "A gentle acoustic song about hope." and tags


def test_generate_video_metadata_retries_a_reply_with_no_text_block_at_all():
    thinking_only = [type("Block", (), {"type": "thinking", "thinking": ""})()]
    client = _ScriptedClient([thinking_only, _GOOD])

    _title, description, _tags = generate_video_metadata(client, "Blackbird", "The Beatles", "lyrics")

    assert description == "A gentle acoustic song about hope."
    assert len(client.messages.calls) == 2


def test_generate_video_metadata_raises_instead_of_returning_a_blank_description():
    import pytest

    from lyricvideo.youtube_metadata import MetadataGenError

    client = _ScriptedClient([_PROSE, _PROSE, _PROSE])

    with pytest.raises(MetadataGenError):
        generate_video_metadata(client, "Blackbird", "The Beatles", "lyrics")

    assert len(client.messages.calls) == 3      # tried three times, then gave up rather than guess


_THINKING_ONLY = [type("Block", (), {"type": "thinking", "thinking": ""})()]


class _CutOffResponse:
    stop_reason = "max_tokens"

    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


# --- comment replies, genres and engagement comments: thinking off, retried, never a blank or cut-off answer ----------
# Issue #7 review, F084/F085: with Sonnet 5's default adaptive thinking these small calls could return no text at all
# (a thinking-only reply), which aborted the whole comment scan / playlist organization, and classify_genre's 50-token
# budget could cut a genre in half ("Classic Ro") that then became a permanent public playlist.

def test_draft_comment_reply_turns_thinking_off():
    client = _ScriptedClient(["IS_ERROR_REPORT: NO\nREPLY: Thanks!"])

    draft_comment_reply(client, "nice video", "My Song")

    assert client.messages.calls[0]["thinking"] == {"type": "disabled"}


def test_draft_comment_reply_retries_a_reply_with_no_text():
    client = _ScriptedClient([_THINKING_ONLY, "IS_ERROR_REPORT: NO\nREPLY: Thanks for playing along!"])

    reply, is_error = draft_comment_reply(client, "nice video", "My Song")

    assert reply == "Thanks for playing along!" and is_error is False
    assert len(client.messages.calls) == 2


def test_draft_comment_reply_raises_rather_than_returning_a_blank_draft():
    import pytest

    from lyricvideo.youtube_metadata import MetadataGenError

    client = _ScriptedClient([_THINKING_ONLY, "IS_ERROR_REPORT: NO\nREPLY:", _THINKING_ONLY])

    with pytest.raises(MetadataGenError):
        draft_comment_reply(client, "nice video", "My Song")
    assert len(client.messages.calls) == 3


def test_classify_genre_turns_thinking_off_with_room_to_answer():
    client = _ScriptedClient(["GENRE: Classic Rock"])

    classify_genre(client, "Some Song", "Some Band", "la la", known_genres=["Classic Rock"])

    call = client.messages.calls[0]
    assert call["thinking"] == {"type": "disabled"} and call["max_tokens"] >= 100


def test_classify_genre_returns_blank_instead_of_raising_when_no_text_ever_comes_back():
    client = _ScriptedClient([_THINKING_ONLY] * 3)

    assert classify_genre(client, "Some Song", "Some Band", "la la", known_genres=["Classic Rock"]) == ""


def test_classify_genre_never_returns_a_cut_off_genre():
    class _Messages:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            return _CutOffResponse("GENRE: Classic Ro")

    client = type("Client", (), {"messages": _Messages()})()

    assert classify_genre(client, "Some Song", "Some Band", "la la", known_genres=[]) == ""
    assert client.messages.calls == 3


def test_classify_genre_returns_blank_for_a_reply_without_a_genre_line():
    client = _ScriptedClient(["I think it is rock.", "Hmm.", "Rock, probably."])

    assert classify_genre(client, "Some Song", "Some Band", "la la", known_genres=[]) == ""


def test_classify_genre_leaves_blank_genres_out_of_the_prompt():
    client = _ScriptedClient(["GENRE: Country"])

    classify_genre(client, "Some Song", "Some Band", "la la", known_genres=["Country", "", "  "])

    assert "\n- \n" not in client.messages.calls[0]["messages"][0]["content"]


def test_draft_engagement_comment_turns_thinking_off_and_raises_rather_than_returning_blank():
    import pytest

    from lyricvideo.youtube_metadata import MetadataGenError

    client = _ScriptedClient([_THINKING_ONLY, "COMMENT:", "no label here"])

    with pytest.raises(MetadataGenError):
        draft_engagement_comment(client, "Some Song")
    assert all(call["thinking"] == {"type": "disabled"} for call in client.messages.calls)


# --- titles always fit YouTube's rules (issue #7 review, F142) ----------------------------------------------------------

_LONG_TITLE = "Everything Is Fine Except the Weather and the Neighbors and the Old Car in the Yard"


def test_a_long_play_along_title_is_fitted_to_100_characters_keeping_its_suffix():
    title = build_play_along_title(_LONG_TITLE, "The Made Up Band")

    assert len(title) <= 100
    assert title.endswith(" - (Play Along Lyrics & Chords)")


def test_a_long_easy_chord_title_is_fitted_and_keeps_its_capo_suffix():
    title = build_easy_chord_title(_LONG_TITLE, "The Made Up Band", 3)

    assert len(title) <= 100
    assert title.endswith("(EASY CHORDS Play Along - Capo 3)")


def test_the_artist_is_dropped_before_the_song_title_is_shortened():
    title = build_play_along_title("A" * 60, "B" * 30)

    assert title == "A" * 60 + " - (Play Along Lyrics & Chords)"


def test_a_150_character_title_is_shortened_with_an_ellipsis():
    title = build_play_along_title("x" * 150, "")

    assert len(title) == 100
    assert title.endswith("… - (Play Along Lyrics & Chords)")


def test_angle_brackets_are_taken_out_of_titles():
    title = build_play_along_title("<Song> Title", "Band >")

    assert "<" not in title and ">" not in title
    assert title == "Song Title - Band - (Play Along Lyrics & Chords)"


def test_short_titles_are_unchanged():
    assert build_play_along_title("November Rain", "Guns N' Roses") == (
        "November Rain - Guns N' Roses - (Play Along Lyrics & Chords)"
    )


def test_generate_video_metadata_turns_thinking_off_and_tells_claude_not_to_comment_on_the_lyrics():
    client = _ScriptedClient([_GOOD])

    generate_video_metadata(client, "Blackbird", "The Beatles", "lyrics")

    call = client.messages.calls[0]
    assert call["thinking"] == {"type": "disabled"}      # default adaptive thinking can spend the whole token budget
    assert "do not comment" in call["messages"][0]["content"].lower()
    assert call["max_tokens"] >= 300
