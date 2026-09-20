"""precision: how exactly does each alignment land on the singing? Karaoke highlighting is noticed at half a second,
so the check is per WORD against Whisper's own word times -- far stricter than the 2 s line check in sync.py.
Song text is invented."""

from lyricvideo.anchors import HeardWord
from lyricvideo.precision import (
    MIN_MATCHED_WORDS, Precision, blend, choose_alignment, match_words, measure,
)

LINES = [
    "the river runs beside the old stone mill",
    "and morning fog lies heavy on the hill",
    "a lantern swings above the wooden door",
    "i wait for you like i have waited before",
]
WORDS = [w for line in LINES for w in line.split()]
LINE_OF = [i for i, line in enumerate(LINES) for _ in line.split()]
TRUE = [10.0 + 6.0 * li + 0.4 * k for li, line in enumerate(LINES) for k in range(len(line.split()))]
HEARD = [HeardWord(w, t, t + 0.35) for w, t in zip(WORDS, TRUE)]


def shifted(times, by, lines=None):
    return [t + by if lines is None or LINE_OF[k] in lines else t for k, t in enumerate(times)]


def pairs(starts):
    return [(s, s + 0.3) for s in starts]


# --- match_words -----------------------------------------------------------------------------

def test_words_are_matched_to_what_was_heard_in_order():
    evidence = match_words(WORDS, HEARD)

    assert len(evidence) == len(WORDS) and abs(evidence[3] - TRUE[3]) < 1e-9


def test_a_word_that_was_not_heard_has_no_evidence_and_punctuation_is_ignored():
    heard = [h for h in HEARD if h.word != "river"]
    words = ["The", "river,", "runs"] + WORDS[3:]

    evidence = match_words(words, heard)

    assert 1 not in evidence and evidence[0] == TRUE[0] and evidence[2] == TRUE[2]


def test_a_repeated_word_matches_in_order_not_to_an_earlier_copy():
    words = ["go", "go", "go"]
    heard = [HeardWord("go", 1.0, 1.3), HeardWord("go", 5.0, 5.3), HeardWord("go", 9.0, 9.3)]

    assert match_words(words, heard) == {0: 1.0, 1: 5.0, 2: 9.0}


# --- measure ---------------------------------------------------------------------------------

def test_an_exact_alignment_is_fully_precise():
    result = measure(TRUE, LINE_OF, match_words(WORDS, HEARD), len(LINES))

    assert result.share == 1.0 and result.off_lines == 0 and result.matched == len(WORDS)


def test_words_a_second_late_are_not_precise_and_their_lines_are_clearly_off():
    result = measure(shifted(TRUE, 1.4, lines={1, 3}), LINE_OF, match_words(WORDS, HEARD), len(LINES))

    assert 0.4 < result.share < 0.6 and result.off_lines == 2


def test_a_shift_under_half_a_second_is_precise():
    result = measure(shifted(TRUE, 0.3), LINE_OF, match_words(WORDS, HEARD), len(LINES))

    assert result.share == 1.0


def test_lines_with_no_heard_words_are_reported_as_unverifiable_not_wrong():
    heard = [h for h in HEARD if not h.word in LINES[2].split()]

    result = measure(TRUE, LINE_OF, match_words(WORDS, heard), len(LINES))

    assert result.no_evidence >= 1 and result.off_lines == 0


# --- blend -----------------------------------------------------------------------------------

def test_the_better_alignment_is_taken_line_by_line():
    """Real (Go Your Own Way, 2026-09-19): the whole-song pass was exact in the first half and the anchored one was
    better in places; neither was best everywhere."""
    whole = pairs(shifted(TRUE, 4.0, lines={2, 3}))          # exact on lines 0-1, 4 s late after
    anchored = pairs(shifted(TRUE, 1.2, lines={0, 1}))        # 1.2 s late on lines 0-1, exact after
    evidence = match_words(WORDS, HEARD)

    mixed = blend(whole, anchored, LINE_OF, evidence, len(LINES))

    assert mixed is not None
    assert all(abs(m[0] - t) < 1e-9 for m, t in zip(mixed, TRUE))


def test_a_blend_never_puts_lines_out_of_order():
    """Real (Money, The Chain): taking each line's better candidate independently can put a line before one sung
    earlier. The whole-song pass is always in order; whatever mix is returned must be too."""
    import random

    evidence = match_words(WORDS, HEARD)
    for seed in range(300):
        rng = random.Random(seed)
        whole_shift = [rng.uniform(-1.5, 1.5) for _ in LINES]                # in order: lines are 6 s apart, ~3 s long
        anchored_shift = [rng.uniform(-7.0, 7.0) for _ in LINES]              # may be wildly out of order
        whole = pairs([t + whole_shift[LINE_OF[k]] for k, t in enumerate(TRUE)])
        anchored = pairs([t + anchored_shift[LINE_OF[k]] for k, t in enumerate(TRUE)])

        mixed = blend(whole, anchored, LINE_OF, evidence, len(LINES))

        assert mixed is not None
        assert all(mixed[k][0] <= mixed[k + 1][0] for k in range(len(mixed) - 1)), seed


def test_a_mix_that_saves_precision_on_later_lines_survives_an_early_conflict():
    """Line 0: whole 0.8 s late, anchored exact. Lines 1-3: whole exact, anchored 5 s late. Best = anchored for line 0
    only, provided that keeps the order (anchored line 0 ends before whole's line 1 starts)."""
    whole = pairs(shifted(TRUE, 0.8, lines={0}))
    anchored = pairs(shifted(TRUE, 5.0, lines={1, 2, 3}))
    evidence = match_words(WORDS, HEARD)

    mixed = blend(whole, anchored, LINE_OF, evidence, len(LINES))

    assert all(abs(m[0] - t) < 1e-9 for m, t in zip(mixed, TRUE))


def test_lines_with_no_evidence_follow_the_previous_lines_choice():
    heard = [h for h in HEARD if h.word not in LINES[1].split()]
    whole = pairs(TRUE)
    anchored = pairs(shifted(TRUE, 0.8))
    mixed = blend(whole, anchored, LINE_OF, match_words(WORDS, heard), len(LINES))

    assert mixed is not None
    line1 = [k for k in range(len(WORDS)) if LINE_OF[k] == 1]
    assert all(abs(mixed[k][0] - TRUE[k]) < 1e-9 for k in line1)      # followed line 0's choice (whole-song, exact)


# --- choose_alignment ------------------------------------------------------------------------

def test_the_more_precise_alignment_is_chosen_and_a_good_one_passes():
    evidence = match_words(WORDS, HEARD)

    choice = choose_alignment({"whole-song": pairs(shifted(TRUE, 0.8)), "anchored": pairs(TRUE)}, LINE_OF, evidence, len(LINES))

    assert choice.method in ("anchored", "blended") and choice.ok and choice.concern == ""


def test_ties_go_to_the_whole_song_alignment():
    evidence = match_words(WORDS, HEARD)

    choice = choose_alignment({"whole-song": pairs(TRUE), "anchored": pairs(TRUE)}, LINE_OF, evidence, len(LINES))

    assert choice.method == "whole-song"


def test_an_alignment_that_is_close_by_the_old_2_second_rule_but_not_precise_is_set_aside():
    """A line 1.9 s late passed the old check; on screen the singer is a whole line ahead of the highlight."""
    evidence = match_words(WORDS, HEARD)
    late = pairs(shifted(TRUE, 1.9))

    choice = choose_alignment({"whole-song": late, "anchored": late}, LINE_OF, evidence, len(LINES))

    assert not choice.ok
    assert "half a second" in choice.concern and "review" in choice.concern.lower()


def test_a_few_stray_lines_in_an_otherwise_precise_song_are_tolerated():
    lines = [f"the lantern number {n} swings above the wooden door" for n in range(20)]
    words = [w for line in lines for w in line.split()]
    line_of = [i for i, line in enumerate(lines) for _ in line.split()]
    true = [5.0 + 5.0 * li + 0.4 * k for li, line in enumerate(lines) for k in range(len(line.split()))]
    heard = [HeardWord(w, t, t + 0.3) for w, t in zip(words, true)]
    starts = [t + 3.0 if line_of[k] == 4 else t for k, t in enumerate(true)]      # one of twenty lines is 3 s late

    choice = choose_alignment({"whole-song": pairs(starts)}, line_of, match_words(words, heard), len(lines))

    assert choice.ok


def test_too_little_evidence_means_no_judgement_can_be_made():
    heard = HEARD[:MIN_MATCHED_WORDS - 1]

    choice = choose_alignment({"whole-song": pairs(TRUE)}, LINE_OF, match_words(WORDS, heard), len(LINES))

    assert choice is None


def test_precision_carries_its_own_summary():
    assert isinstance(measure(TRUE, LINE_OF, match_words(WORDS, HEARD), len(LINES)), Precision)


# --- reporting -------------------------------------------------------------------------------

def test_the_set_aside_reason_names_the_lines_that_are_off_so_the_owner_can_find_them():
    """Real (Go Your Own Way): lines 27, 34 and 36 sat in guitar solos; the owner needs to know WHICH lines to look at."""
    evidence = match_words(WORDS, HEARD)

    choice = choose_alignment({"whole-song": pairs(shifted(TRUE, 1.9, lines={1, 3}))}, LINE_OF, evidence, len(LINES))

    assert not choice.ok
    assert "lines 2, 4" in choice.concern                      # 1-based, like the lyrics editor


def test_a_blend_identical_to_one_candidate_is_reported_under_that_candidates_name():
    evidence = match_words(WORDS, HEARD)

    choice = choose_alignment({"whole-song": pairs(shifted(TRUE, 3.0)), "anchored": pairs(TRUE)}, LINE_OF, evidence, len(LINES))

    assert choice.method == "anchored"


def test_a_candidate_with_words_out_of_order_is_never_chosen_over_a_valid_one():
    """The renderer refuses a timeline that goes backwards (combine.AlignmentSanityError), so an out-of-order candidate
    must lose even if it happens to score the same."""
    evidence = match_words(WORDS, HEARD)
    backwards = pairs([t if k != 12 else 0.0 for k, t in enumerate(shifted(TRUE, 30.0))])

    choice = choose_alignment({"whole-song": backwards, "anchored": pairs(shifted(TRUE, 30.0))}, LINE_OF, evidence, len(LINES))

    assert choice.method == "anchored"
