"""scripts/update_support_description.py's pure decision logic: reorder an already-long description exactly as
written, but regenerate a too-short one from the song's own real lyrics (owner, 2026-09-29)."""

from pathlib import Path

from lyricvideo.chord_theory import save_easy_chord_capo_marker
from lyricvideo.models import LyricLine, Song, Word, save_song

_TEMPLATE = "☕ Tips: https://ko-fi.com/x\n{description}\nThanks for playing along!"
_LONG_BODY = "A" * 250   # well over READY_CHARS, whatever its wording
_SHORT_BODY = "A two sentence description. Not much more to it."   # well under READY_CHARS


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]
        self.stop_reason = "end_turn"


class _FakeMessages:
    def __init__(self, reply="DESCRIPTION: A freshly written, much longer description of the song.\nTAGS: a, b"):
        self.reply = reply
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _FakeAnthropicResponse(self.reply)


class _FakeAnthropicClient:
    def __init__(self, reply=None):
        self.messages = _FakeMessages(reply) if reply else _FakeMessages()


def _make_song_dir(tmp_path: Path, title: str = "My Song") -> Path:
    work_dir = tmp_path / "my-song"
    work_dir.mkdir()
    song = Song(title=title, audio_path="song.mp3", lines=[LyricLine(words=[Word(word="hello")])])
    save_song(song, work_dir / "lyrics_timed.json")
    (work_dir / "song_info.json").write_text('{"artist": "An Artist"}', encoding="utf-8")
    return work_dir


def test_a_long_body_is_kept_exactly_as_written_and_claude_is_never_called(tmp_path):
    from scripts.update_support_description import new_description

    work_dir = _make_song_dir(tmp_path)
    client = _FakeAnthropicClient()

    updated, regenerated = new_description(_LONG_BODY, _TEMPLATE, [], anthropic_client=client, work_dir=work_dir)

    assert not regenerated
    assert _LONG_BODY in updated
    assert client.messages.calls == 0


def test_a_short_body_is_regenerated_from_the_songs_real_lyrics(tmp_path):
    from scripts.update_support_description import new_description

    work_dir = _make_song_dir(tmp_path)
    client = _FakeAnthropicClient()

    updated, regenerated = new_description(_SHORT_BODY, _TEMPLATE, [], anthropic_client=client, work_dir=work_dir)

    assert regenerated
    assert "A freshly written, much longer description of the song." in updated
    assert _SHORT_BODY not in updated
    assert client.messages.calls == 1


def test_without_an_anthropic_client_a_short_body_is_just_reordered(tmp_path):
    from scripts.update_support_description import new_description

    work_dir = _make_song_dir(tmp_path)

    updated, regenerated = new_description(_SHORT_BODY, _TEMPLATE, [], anthropic_client=None, work_dir=work_dir)

    assert not regenerated
    assert _SHORT_BODY in updated


def test_a_short_body_is_kept_when_the_songs_local_files_are_gone(tmp_path):
    """Never blocks or fabricates a description -- a song whose work folder was moved or deleted just keeps
    whatever short text YouTube already has, reordered."""
    from scripts.update_support_description import new_description

    work_dir = tmp_path / "gone-song"     # never created -- no lyrics_timed.json to read
    client = _FakeAnthropicClient()

    updated, regenerated = new_description(_SHORT_BODY, _TEMPLATE, [], anthropic_client=client, work_dir=work_dir)

    assert not regenerated
    assert _SHORT_BODY in updated
    assert client.messages.calls == 0


def test_a_key_note_survives_a_regeneration_right_after_the_new_body(tmp_path):
    from scripts.update_support_description import new_description

    work_dir = _make_song_dir(tmp_path)
    note = "📌 Song key: C minor (not D minor as shown in the video)"
    current = f"{note}\n\n{_SHORT_BODY}\n\nThanks for playing along!"
    client = _FakeAnthropicClient()

    updated, regenerated = new_description(current, _TEMPLATE, [], anthropic_client=client, work_dir=work_dir)

    assert regenerated
    body_pos = updated.index("A freshly written")
    note_pos = updated.index(note)
    assert body_pos < note_pos   # the new description reads first, the key note right after it


def test_song_facts_uses_the_clean_title_for_an_easy_chord_folder(tmp_path):
    from scripts.update_support_description import _song_facts

    work_dir = tmp_path / "bridge-easychords"
    work_dir.mkdir()
    song = Song(title="Bridge Over Troubled Water EasyChords", audio_path="song.mp3",
                lines=[LyricLine(words=[Word(word="hello")])])
    save_song(song, work_dir / "lyrics_timed.json")
    (work_dir / "song_info.json").write_text('{"artist": "Simon and Garfunkel"}', encoding="utf-8")
    save_easy_chord_capo_marker(
        work_dir, capo_fret=1, shape_key="D", original_key="Eb major",
        original_title="Bridge Over Troubled Water",
    )

    facts = _song_facts(work_dir)

    assert facts is not None
    title, artist, _lyrics = facts
    assert title == "Bridge Over Troubled Water"
    assert artist == "Simon and Garfunkel"


def test_song_facts_is_none_when_the_song_has_no_local_files(tmp_path):
    from scripts.update_support_description import _song_facts

    assert _song_facts(tmp_path / "nowhere") is None


# --- an older, unrecognized description format is skipped, not written half-duplicated ------------------------------

def test_looks_clean_rejects_a_duplicated_tip_link():
    from scripts.update_support_description import looks_clean

    duplicated = (
        "Body.\n\nTips: https://ko-fi.com/playalongvideos\n\n"
        "☕ Tips are never expected: https://ko-fi.com/playalongvideos"
    )
    assert not looks_clean(duplicated)


def test_looks_clean_rejects_a_duplicated_key_mention():
    from scripts.update_support_description import looks_clean

    duplicated = "🎸 Song key: D major\n\nBody.\n\n📌 Song key: C major (not D major as shown in the video)"
    assert not looks_clean(duplicated)


def test_looks_clean_accepts_an_ordinary_description():
    from scripts.update_support_description import looks_clean

    assert looks_clean("Body.\n\n☕ Tips: https://ko-fi.com/playalongvideos\n\nThanks!")
    assert looks_clean("📌 Song key: C major (not D major as shown in the video)\n\nBody.")
