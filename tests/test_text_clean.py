from lyricvideo.text_clean import artist_key, clean_title, normalize, smart_title_case


def test_clean_title_strips_official_video_noise():
    assert clean_title("Eye in the Sky (Official Video)") == "Eye in the Sky"


def test_clean_title_strips_track_number_prefix():
    assert clean_title("01. Eye in the Sky") == "Eye in the Sky"
    assert clean_title("01 - Eye in the Sky") == "Eye in the Sky"


def test_clean_title_leaves_numbers_that_are_part_of_the_title():
    assert clean_title("99 Luftballons") == "99 Luftballons"
    assert clean_title("21 Guns") == "21 Guns"


def test_normalize_strips_accents_and_punctuation():
    assert normalize("Café del Mar!") == "cafe del mar"


def test_normalize_collapses_whitespace():
    assert normalize("  Hello   World  ") == "hello world"


def test_artist_key_collapses_the_and_ampersand_variants():
    assert artist_key("The Alan Parsons Project") == artist_key("Alan Parsons Project")
    assert artist_key("Simon & Garfunkel") == artist_key("Simon and Garfunkel")


def test_smart_title_case_capitalizes_lowercase_input():
    assert smart_title_case("eye in the sky") == "Eye in the Sky"


def test_smart_title_case_leaves_mixed_case_untouched():
    assert smart_title_case("Eye IN the Sky") == "Eye IN the Sky"
