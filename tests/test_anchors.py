"""anchors: where in the song does each lyric line really sit, according to what Whisper heard (with word
timings)? These anchors bound the forced aligner so it cannot drift far from the truth -- the failure seen on
'Girls Just Want to Have Fun' (lines 20-50 s early) and 'Ironic' (everything squeezed into 43 s). Text is invented."""

from lyricvideo.anchors import Block, HeardWord, LineAnchor, line_anchors, plan_windows

L = [
    "the river runs beside the old stone mill",
    "and morning fog lies heavy on the hill",
    "a lantern swings above the wooden door",
    "i wait for you like i have waited before",
    "the winter came and covered every road",
    "we traded all our dreams for heavy loads",
]
CHORUS = "carry me home across the silver sea"


def heard_lines(placed):
    """[(start_seconds, text)] -> Whisper-style words, 0.4 s per word."""
    words = []
    for start, text in placed:
        for i, w in enumerate(text.split()):
            words.append(HeardWord(w, start + i * 0.4, start + i * 0.4 + 0.35))
    return words


def test_every_line_that_was_heard_is_anchored_near_its_real_time():
    placed = [(10.0, L[0]), (16.0, L[1]), (22.0, L[2]), (28.0, L[3])]

    anchors = line_anchors(L[:4], heard_lines(placed))

    assert sorted(anchors) == [0, 1, 2, 3]
    for i, (start, text) in enumerate(placed):
        assert abs(anchors[i].start - start) < 0.6
        assert anchors[i].end > anchors[i].start


def test_a_sung_section_the_lyrics_lack_pushes_later_anchors_to_where_they_really_are():
    """Real ('Girls Just Want to Have Fun'): the audio holds extra chorus repeats the text doesn't list. The
    text lines after them must anchor at their true (later) time, not directly after the previous line."""
    placed = [(10.0, L[0]), (16.0, L[1]), (22.0, CHORUS), (28.0, CHORUS), (34.0, CHORUS), (60.0, L[2]), (66.0, L[3])]

    anchors = line_anchors(L[:4], heard_lines(placed))

    assert abs(anchors[2].start - 60.0) < 0.6
    assert abs(anchors[3].start - 66.0) < 0.6


def test_a_line_with_most_words_misheard_is_not_anchored_but_a_mostly_heard_one_is():
    heard = heard_lines([(10.0, L[0]), (16.0, "and morning fog lies zebra on the hill"), (22.0, "totally unrelated words here now"), (28.0, L[3])])

    anchors = line_anchors(L[:4], heard)

    assert 0 in anchors and 1 in anchors and 3 in anchors
    assert 2 not in anchors


def test_the_start_estimate_allows_for_unheard_words_at_the_front_of_a_line():
    heard = heard_lines([(20.0, "beside the old stone mill")])          # "the river runs" were not heard

    anchor = line_anchors([L[0]], heard)[0]

    assert 18.5 < anchor.start < 20.0                                   # ~3 words x 0.35 s before 20.0


def test_repeated_lyric_lines_anchor_to_their_own_occurrence_in_order():
    lyric = [CHORUS, L[0], CHORUS, L[1], CHORUS]
    heard = heard_lines([(10.0, CHORUS), (20.0, L[0]), (30.0, CHORUS), (40.0, L[1]), (50.0, CHORUS)])

    anchors = line_anchors(lyric, heard)

    assert [round(anchors[i].start) for i in range(5)] == [10, 20, 30, 40, 50]


def test_anchor_times_never_go_backwards():
    placed = [(10.0, L[0]), (16.0, L[1]), (22.0, L[2]), (28.0, L[3]), (34.0, L[4]), (40.0, L[5])]

    anchors = line_anchors(L, heard_lines(placed))
    starts = [anchors[i].start for i in sorted(anchors)]

    assert starts == sorted(starts)


def test_nothing_heard_means_no_anchors():
    assert line_anchors(L, []) == {}
    assert line_anchors([], heard_lines([(1.0, L[0])])) == {}


# --- plan_windows -----------------------------------------------------------------------------

def _a(line, start, end):
    return LineAnchor(line=line, start=start, end=end, matched=5, total=6)


def test_with_no_anchors_the_whole_song_is_one_block():
    assert plan_windows(4, {}, 100.0, pad=3.0) == [Block(0, 3, 0.0, 100.0)]


def test_an_anchored_line_gets_a_window_around_its_anchor_and_gaps_get_their_own_block():
    anchors = {0: _a(0, 10.0, 14.0), 3: _a(3, 40.0, 44.0)}       # lines 1-2 were not heard

    blocks = plan_windows(4, anchors, 100.0, pad=3.0)

    assert blocks == [
        Block(0, 0, 7.0, 17.0),
        Block(1, 2, 11.0, 43.0),                                 # between the neighbours' anchors, padded
        Block(3, 3, 37.0, 47.0),
    ]


def test_lines_before_the_first_and_after_the_last_anchor_are_bounded_by_the_song_edges():
    anchors = {1: _a(1, 20.0, 24.0), 2: _a(2, 30.0, 34.0)}

    blocks = plan_windows(4, anchors, 60.0, pad=3.0)

    assert blocks[0] == Block(0, 0, 0.0, 23.0)
    assert blocks[-1] == Block(3, 3, 31.0, 60.0)


def test_blocks_cover_every_line_exactly_once_and_windows_stay_inside_the_song():
    anchors = {0: _a(0, 0.5, 4.0), 5: _a(5, 96.0, 99.8)}

    blocks = plan_windows(6, anchors, 100.0, pad=3.0)

    covered = [i for b in blocks for i in range(b.first, b.last + 1)]
    assert covered == list(range(6))
    assert all(0.0 <= b.start < b.end <= 100.0 for b in blocks)


# --- ignoring words Whisper "heard" in silence ('Girls Just Want to Have Fun', 2026-09-19) ---------------

def test_words_heard_where_the_vocal_track_is_silent_are_dropped():
    """Real: the vocal stem was silent after ~220 s but Whisper 'heard' 'just wanna, just wanna' at 222-228 s
    (a hallucination), which anchored line 48 at 222 s and dragged the last 8 lines into the silent tail."""
    from lyricvideo.anchors import drop_words_in_silence

    heard = [HeardWord("real", 10.0, 10.4), HeardWord("ghost", 222.0, 222.4), HeardWord("ghost2", 226.0, 226.4)]
    loudness = [0.0] * 500                      # 0.5 s per step; silent...
    for step in range(15, 30):
        loudness[step] = 0.6                    # ...except 7.5 s-15 s

    kept = drop_words_in_silence(heard, loudness, hop=0.5)

    assert [w.word for w in kept] == ["real"]


def test_a_word_that_overlaps_any_voiced_stretch_is_kept():
    from lyricvideo.anchors import drop_words_in_silence

    loudness = [0.0, 0.0, 0.8, 0.0, 0.0]        # voiced only at 1.0-1.5 s
    heard = [HeardWord("edge", 0.8, 1.2), HeardWord("late", 2.0, 2.4)]

    assert [w.word for w in drop_words_in_silence(heard, loudness, hop=0.5)] == ["edge"]


def test_a_quiet_song_is_judged_against_its_own_loudness_not_an_absolute_level():
    from lyricvideo.anchors import drop_words_in_silence

    loudness = [0.01] * 10 + [0.0] * 10        # very quiet but clearly voiced, then true silence
    heard = [HeardWord("soft", 1.0, 1.4), HeardWord("ghost", 8.0, 8.4)]

    assert [w.word for w in drop_words_in_silence(heard, loudness, hop=0.5)] == ["soft"]


def test_no_loudness_information_keeps_every_word():
    from lyricvideo.anchors import drop_words_in_silence

    heard = [HeardWord("a", 1.0, 1.4)]

    assert drop_words_in_silence(heard, [], hop=0.5) == heard


# --- lrclib timestamps as a second opinion (repeated choruses are ambiguous to text matching) ----------

def _wa(line, start):
    return LineAnchor(line=line, start=start, end=start + 3.0, matched=5, total=6)


COUNTS = [8] * 12
LRC = [10.0 + 6.0 * i for i in range(12)]                # lines every 6 s


def test_without_lrclib_times_the_whisper_anchors_are_unchanged():
    from lyricvideo.anchors import combine_anchors

    whisper = {0: _wa(0, 10.0), 1: _wa(1, 16.0)}

    assert combine_anchors(whisper, None, COUNTS) == whisper
    assert combine_anchors(whisper, LRC[:5], COUNTS) == whisper           # line counts differ: unusable


def test_consistent_whisper_anchors_are_kept_and_missing_lines_take_lrclibs_position_plus_the_offset():
    from lyricvideo.anchors import combine_anchors

    # the recording runs 2 s later than lrclib's edition; Whisper heard lines 0-3 and 8-11 but not 4-7
    whisper = {i: _wa(i, LRC[i] + 2.0) for i in (0, 1, 2, 3, 8, 9, 10, 11)}

    combined = combine_anchors(whisper, LRC, COUNTS)

    assert sorted(combined) == list(range(12))
    assert combined[1].start == whisper[1].start                         # trusted anchors untouched
    assert abs(combined[5].start - (LRC[5] + 2.0)) < 0.3                 # filled in from lrclib + the offset


def test_isolated_anchors_that_contradict_lrclib_are_replaced():
    """Real ('Girls Just Want to Have Fun'): a chorus repeated more often in the text than Whisper heard it, so
    the few heard occurrences were handed to the wrong copies (20-30 s off) -- but lrclib knows where each is."""
    from lyricvideo.anchors import combine_anchors

    whisper = {i: _wa(i, LRC[i] + 1.0) for i in (0, 1, 2, 3, 4)}          # a trusted run
    whisper[6] = _wa(6, LRC[6] - 22.0)                                    # wrong copy
    whisper[9] = _wa(9, LRC[9] - 30.0)                                    # wrong copy
    whisper.update({i: _wa(i, LRC[i] + 1.0) for i in (10, 11)})

    combined = combine_anchors(whisper, LRC, COUNTS)

    assert abs(combined[6].start - (LRC[6] + 1.0)) < 0.5
    assert abs(combined[9].start - (LRC[9] + 1.0)) < 0.5


def test_a_real_section_shift_between_editions_is_kept_because_it_forms_a_consistent_run():
    from lyricvideo.anchors import combine_anchors

    # lines 0-4 run +1 s from lrclib; from line 5 on the recording has an extra 12 s section (+13 s)
    whisper = {i: _wa(i, LRC[i] + 1.0) for i in range(5)}
    whisper.update({i: _wa(i, LRC[i] + 13.0) for i in range(5, 12)})

    combined = combine_anchors(whisper, LRC, COUNTS)

    assert all(combined[i].start == whisper[i].start for i in range(12))


def test_with_no_run_of_agreeing_anchors_nothing_can_be_validated_so_whisper_is_left_alone():
    from lyricvideo.anchors import combine_anchors

    scattered = {0: _wa(0, 40.0), 3: _wa(3, 5.0), 7: _wa(7, 90.0)}

    assert combine_anchors(scattered, LRC, COUNTS) == scattered


def test_a_short_run_that_jumps_far_from_the_dominant_offset_is_not_believed():
    """Real ('Girls Just Want to Have Fun'): three anchors on wrong chorus copies happened to share an offset (-33 s)
    and were believed as a 'section shift'. A big jump needs proportionally more evidence than a small one."""
    from lyricvideo.anchors import combine_anchors

    lrc = [10.0 + 4.0 * i for i in range(30)]
    counts = [8] * 30
    whisper = {i: _wa(i, lrc[i] + 0.5) for i in range(0, 20)}            # a long, well-supported run at +0.5 s
    whisper.update({i: _wa(i, lrc[i] - 33.0) for i in (22, 25, 29)})      # three wrong copies, all ~-33 s

    combined = combine_anchors(whisper, lrc, counts)

    for i in (22, 25, 29):
        assert abs(combined[i].start - (lrc[i] + 0.5)) < 0.6            # replaced by lrclib + the trusted offset


def test_a_large_shift_with_plenty_of_supporting_anchors_is_still_believed():
    from lyricvideo.anchors import combine_anchors

    lrc = [10.0 + 4.0 * i for i in range(30)]
    whisper = {i: _wa(i, lrc[i] + 0.5) for i in range(0, 12)}
    whisper.update({i: _wa(i, lrc[i] + 30.0) for i in range(12, 30)})    # an extra 30 s section, then everything shifted

    combined = combine_anchors(whisper, lrc, [8] * 30)

    assert all(combined[i].start == whisper[i].start for i in range(30))
