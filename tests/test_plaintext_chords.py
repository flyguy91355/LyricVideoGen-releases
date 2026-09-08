import pytest

from lyricvideo.plaintext_chords import parse_plaintext_chords


def test_parse_chord_over_lyric_text():
    text = "G          D\nhello      there\n"
    lines, blocks = parse_plaintext_chords(text)

    assert len(lines) == 1
    words = lines[0].words
    assert [w.word for w in words] == ["hello", "there"]
    assert words[0].chord == "G"
    assert words[1].chord == "D"
    assert blocks == []


def test_parse_plain_lyric_line_with_no_chords():
    text = "just some ordinary\nsentence with no chords\n"
    lines, blocks = parse_plaintext_chords(text)

    assert all(w.chord is None for line in lines for w in line.words)
    assert blocks == []


def test_parse_leading_instrumental_block():
    text = "Em7  G\nG      D\nhello  there\n"
    lines, blocks = parse_plaintext_chords(text)

    assert len(lines) == 1
    assert len(blocks) == 1
    assert blocks[0].before_line_index == 0
    assert blocks[0].chords == ["Em7", "G"]


def test_parse_trailing_instrumental_block():
    text = "G      D\nhello  there\nEm7  G\n"
    lines, blocks = parse_plaintext_chords(text)

    assert len(lines) == 1
    assert len(blocks) == 1
    assert blocks[0].before_line_index == 1
    assert blocks[0].chords == ["Em7", "G"]


def test_parse_ignores_blank_lines():
    text = "G      D\nhello  there\n\n\nEm    C\ngoodbye now\n"
    lines, blocks = parse_plaintext_chords(text)

    assert len(lines) == 2
    assert [w.word for w in lines[0].words] == ["hello", "there"]
    assert [w.word for w in lines[1].words] == ["goodbye", "now"]


def test_parse_raises_on_empty_result():
    with pytest.raises(ValueError):
        parse_plaintext_chords("\n\n")


def test_chord_matched_to_nearest_word_by_column():
    # "D" sits directly above "there" (column 11), not "hello" (column 0)
    text = "           D\nhello      there\n"
    lines, _ = parse_plaintext_chords(text)

    words = lines[0].words
    assert words[0].word == "hello"
    assert words[0].chord is None
    assert words[1].word == "there"
    assert words[1].chord == "D"
