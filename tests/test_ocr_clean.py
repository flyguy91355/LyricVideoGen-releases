from lyricvideo.ocr_clean import filter_dictionary_lines

DICTIONARY = {"so", "you", "think", "can", "tell", "heaven", "from", "hell", "hit"}


def test_filter_dictionary_lines_keeps_real_sentences():
    lines = ["So you think you can tell", "Heaven from Hell"]
    assert filter_dictionary_lines(lines, DICTIONARY) == lines


def test_filter_dictionary_lines_drops_gibberish():
    lines = ["fil Hit thi bil ie fi til", "So you think you can tell"]
    assert filter_dictionary_lines(lines, DICTIONARY) == ["So you think you can tell"]


def test_filter_dictionary_lines_drops_numeric_tab_remnants():
    lines = ["2S25-5555 x2", "Soest x2", "So you think you can tell"]
    assert filter_dictionary_lines(lines, DICTIONARY) == ["So you think you can tell"]


def test_filter_dictionary_lines_digit_tokens_never_count_as_real_words():
    # "2S25-5555" strips down to just "s", and "x2" strips to "x" -- both would
    # accidentally match if the dictionary contains single-letter entries; a
    # token containing any digit must never count as a real-word match.
    dictionary_with_single_letters = DICTIONARY | {"s", "x"}
    lines = ["2S25-5555 x2"]
    assert filter_dictionary_lines(lines, dictionary_with_single_letters) == []


def test_filter_dictionary_lines_drops_blank_lines():
    assert filter_dictionary_lines(["", "  ", "So you think you can tell"], DICTIONARY) == [
        "So you think you can tell"
    ]


def test_filter_dictionary_lines_respects_custom_ratio():
    lines = ["So you think you xyzzy"]  # 4/5 real words = 0.8
    assert filter_dictionary_lines(lines, DICTIONARY, min_ratio=0.9) == []
    assert filter_dictionary_lines(lines, DICTIONARY, min_ratio=0.7) == lines
