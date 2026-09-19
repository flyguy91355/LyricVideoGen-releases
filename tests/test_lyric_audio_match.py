"""lyric_audio_match scores candidate lyric text against what Whisper actually heard.
Song text below is invented (same shape as a real song: verses, a repeated chorus,
a bridge) so no real lyrics live in the tests."""

from lyricvideo.lyric_audio_match import audio_match_passes, describe_mismatch, score_lyrics_against_transcript

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
BRIDGE = [
    "hold on hold on the storm will soon be past",
    "hold on hold on we finally found peace at last",
]
OTHER_SONG = [
    "neon signs are flashing down the avenue",
    "every stranger's face looks like a stranger's face to you",
    "dance until the sunrise turns the pavement gold",
    "nobody will ever tell the story we were told",
]
SUNG_ORDER = V1 + CHORUS + V2 + CHORUS + BRIDGE + CHORUS


def whisper_like(lines: list[str]) -> str:
    """What a speech recognizer plausibly hears: every 9th word dropped and every
    7th word misheard (last letter lost), like real, imperfect Whisper output."""
    heard = []
    for i, word in enumerate(" ".join(lines).split(), 1):
        if i % 9 == 0:
            continue
        heard.append(word[:-1] if i % 7 == 0 and len(word) > 3 else word)
    return " ".join(heard)


def test_correct_lyrics_pass_even_when_the_transcript_is_imperfect():
    match = score_lyrics_against_transcript(SUNG_ORDER, whisper_like(SUNG_ORDER))

    assert audio_match_passes(match)
    assert match.coverage > 0.75
    assert all(match.line_supported)


def test_a_line_sung_twice_in_a_row_but_transcribed_once_is_still_supported():
    """Whisper collapses immediate repeats (real: 'ah, look at all the lonely
    people' sung twice, heard once). The lyric file lists both."""
    lyric = ["ah look at all the lonely people"] * 2 + V1
    heard = whisper_like(["ah look at all the lonely people"] + V1)

    match = score_lyrics_against_transcript(lyric, heard)

    assert match.line_supported[:2] == [True, True]
    assert audio_match_passes(match)


def test_a_repeated_chorus_block_transcribed_once_is_still_supported():
    lyric = V1 + CHORUS + CHORUS + V2
    heard = whisper_like(V1 + CHORUS + V2)

    match = score_lyrics_against_transcript(lyric, heard)

    assert audio_match_passes(match)
    assert all(match.line_supported)


def test_swapping_the_first_verse_and_the_first_chorus_is_caught():
    """Owner's test (2026-09-19): mix up the verse and the chorus."""
    wrong_order = CHORUS + V1 + V2 + CHORUS + BRIDGE + CHORUS

    match = score_lyrics_against_transcript(wrong_order, whisper_like(SUNG_ORDER))

    assert not audio_match_passes(match)
    assert match.worst_run >= 4


def test_a_verse_from_a_different_song_is_caught_and_located():
    wrong_verse = V1 + CHORUS + OTHER_SONG + CHORUS + BRIDGE + CHORUS

    match = score_lyrics_against_transcript(wrong_verse, whisper_like(SUNG_ORDER))

    assert not audio_match_passes(match)
    assert match.unsupported_ranges == [(9, 12)]  # 1-based line numbers of OTHER_SONG
    assert "lines 9-12" in describe_mismatch(match)


def test_lyrics_for_an_entirely_different_song_fail():
    wrong_song = OTHER_SONG * 3

    match = score_lyrics_against_transcript(wrong_song, whisper_like(SUNG_ORDER))

    assert not audio_match_passes(match)
    assert match.coverage < 0.2


def test_a_couple_of_misheard_lines_do_not_fail_a_correct_song():
    """Whisper errors on one or two lines must not flag a right song: only a
    sustained run of unmatched lines counts."""
    heard = whisper_like(V1 + CHORUS + V2 + CHORUS + BRIDGE + CHORUS)
    lyric = list(SUNG_ORDER)
    lyric[2] = "a completely different third line here"
    lyric[13] = "another unrelated stray line inserted"

    match = score_lyrics_against_transcript(lyric, heard)

    assert audio_match_passes(match)
    assert match.worst_run <= 2


def test_no_recognized_singing_cannot_be_verified():
    match = score_lyrics_against_transcript(SUNG_ORDER, "")

    assert not audio_match_passes(match)
    assert "no words" in describe_mismatch(match).lower()


def test_empty_lyrics_do_not_crash():
    match = score_lyrics_against_transcript([], whisper_like(SUNG_ORDER))

    assert not audio_match_passes(match)
    assert match.line_supported == []


def test_parenthesized_backing_vocals_are_not_required_to_be_heard():
    """Real finding, 2026-09-19 ('Like a Prayer'): the lyric file lists long runs of
    quiet backing vocals like "(Just like a prayer)". Whisper can't hear those, and
    they were flagging a song whose lead vocal matched."""
    lyric = V1 + ["(carry me home across the silver sea)", "(the stars will guide us home)", "oh yeah yeah"] + V2
    heard = whisper_like(V1 + V2)

    match = score_lyrics_against_transcript(lyric, heard)

    assert audio_match_passes(match)
    assert match.worst_run == 0


def test_a_line_mixing_lead_and_backing_words_is_judged_on_the_lead_words():
    lyric = V1 + ["the winter came and covered every road (echo echo road)"] + V2[1:]
    heard = whisper_like(V1 + [V2[0]] + V2[1:])

    match = score_lyrics_against_transcript(lyric, heard)

    assert match.line_supported[4] is True


def test_vocalizations_like_whoa_and_mmm_are_not_counted_as_words_to_match():
    lyric = V1 + ["whoa oh-oh-oh", "mmm mm", "yeah yeah yeah yeah"] + V2
    heard = whisper_like(V1 + V2)

    match = score_lyrics_against_transcript(lyric, heard)

    assert audio_match_passes(match)
    assert all(match.line_supported)


def test_a_sung_verse_missing_from_the_lyrics_is_caught():
    """The remaining lines all match, but a whole verse that IS sung is not in the file --
    the video would show nothing (and the aligner would stretch its neighbors) there."""
    lyric_without_verse_two = V1 + CHORUS + CHORUS + BRIDGE + CHORUS

    match = score_lyrics_against_transcript(lyric_without_verse_two, whisper_like(SUNG_ORDER))

    assert match.worst_heard_gap >= 12
    assert not audio_match_passes(match)
    assert "sung words in a row aren't in these lyrics" in describe_mismatch(match)


def test_a_short_stray_phrase_the_recognizer_added_does_not_fail_a_correct_song():
    """Whisper sometimes invents a stray phrase or hears an ad-lib; a few unexplained
    words are normal, a whole missing verse is not."""
    heard = whisper_like(V1) + " thank you so much for watching " + whisper_like(CHORUS + V2 + CHORUS + BRIDGE + CHORUS)

    match = score_lyrics_against_transcript(SUNG_ORDER, heard)

    assert audio_match_passes(match)
    assert 0 < match.worst_heard_gap <= 4


def test_a_correct_song_has_no_large_unexplained_stretch_of_singing():
    match = score_lyrics_against_transcript(SUNG_ORDER, whisper_like(SUNG_ORDER))

    assert match.worst_heard_gap <= 3


def test_a_phrase_the_recognizer_looped_is_counted_once():
    """Real finding, 2026-09-19 ('A Day in the Life'): Whisper hallucinated "I'm gonna be with
    you" five times over an orchestral finale. A loop is one bad guess, not a 16-word gap."""
    heard = whisper_like(V1 + CHORUS + V2) + " im gonna be with you" * 5

    match = score_lyrics_against_transcript(V1 + CHORUS + V2, heard)

    assert match.worst_heard_gap <= 4
    assert audio_match_passes(match)


def test_words_sung_as_parenthesized_backing_vocals_still_explain_what_was_heard():
    """Real finding, 2026-09-19 ('All I Wanna Do'): "All I wanna do (is make love to you)".
    Backing words need not be heard, but if they ARE heard the lyrics must explain them."""
    backing = [f"oh ({line})" for line in CHORUS[:3]]
    lyric = V1 + backing + V2
    heard = whisper_like(V1 + CHORUS[:3] + V2)

    match = score_lyrics_against_transcript(lyric, heard)

    assert match.worst_heard_gap <= 3
    assert audio_match_passes(match)


def test_a_fade_out_vamp_is_excused_when_its_phrase_was_verified_earlier_in_the_song():
    """Real finding, 2026-09-19 ('Billie Jean'): the outro repeats one line ~18 times and the
    recognizer can't hear the fade. The phrase itself was sung and matched earlier."""
    vamp = [CHORUS[0]] * 5
    lyric = V1 + CHORUS + vamp
    heard = whisper_like(V1 + CHORUS)

    match = score_lyrics_against_transcript(lyric, heard)

    assert match.worst_run == 0
    assert audio_match_passes(match)


def test_an_unheard_vamp_whose_phrase_appears_nowhere_else_is_still_unsupported():
    vamp = [OTHER_SONG[0]] * 5
    lyric = V1 + CHORUS + vamp
    heard = whisper_like(V1 + CHORUS)

    match = score_lyrics_against_transcript(lyric, heard)

    assert match.worst_run >= 5
    assert not audio_match_passes(match)


def test_a_genuinely_repeated_sung_line_is_not_trimmed_on_the_lyric_side():
    """Loop-trimming exists to forgive recognizer hallucinations when counting UNEXPLAINED
    sung words; it must not shrink what was really heard when checking the lyric lines.
    Real ('All the Young Dudes' outro): the lead line alternates with DIFFERENT backing lines,
    so the lyric side can't treat it as one repeat, and the singer really does sing it 6 times."""
    backing = ["hey dudes", "where are ya", "stand up", "hands up", "louder now", "one more time"]
    lyric = V1 + [x for b in backing for x in ("carry the news", f"({b})")] + V2
    heard = " ".join(V1 + ["carry the news"] * 6 + V2)

    match = score_lyrics_against_transcript(lyric, heard)

    assert all(match.line_supported)
    assert audio_match_passes(match)


def test_parenthesized_backing_lines_that_are_heard_count_as_supported():
    """Real finding, 2026-09-19 ('Every Breath You Take'): the lead line alternates with
    backing lines like "(Every move you make)" that the recognizer clearly hears. Ignoring
    them entirely turned a 26-line stretch into one unsupported run."""
    backing = [f"({line})" for line in CHORUS[:3]]
    lyric = V1 + ["neon signs are flashing down the avenue"] + backing + ["dance until the sunrise turns the pavement gold"] + V2
    heard = whisper_like(V1 + CHORUS[:3] + V2)

    match = score_lyrics_against_transcript(lyric, heard)

    assert match.line_supported[5:8] == [True, True, True]  # the heard backing lines
    assert match.line_supported[4] is False and match.line_supported[8] is False  # unheard leads stay flagged
    assert match.worst_run == 1
