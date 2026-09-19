"""sync: does the aligned timing agree with where Whisper heard each line? Picks the whole-song alignment when it is
already right, the anchored one when it drifted, and reports 'set aside for review' when neither can be trusted."""

from lyricvideo.anchors import LineAnchor
from lyricvideo.sync import decide_alignment, sync_agreement


def anchors_at(starts):
    return {i: LineAnchor(i, s, s + 3.0, 5, 6) for i, s in enumerate(starts)}


TRUE_STARTS = [10.0, 16.0, 22.0, 28.0, 34.0, 40.0, 46.0, 52.0]


def test_lines_at_their_anchors_agree_fully():
    report = sync_agreement(TRUE_STARTS, anchors_at(TRUE_STARTS))

    assert report.agreement == 1.0 and report.verifiable


def test_lines_far_from_their_anchors_do_not_agree():
    """Real ('Girls Just Want to Have Fun'): lines 20-50 s early."""
    early = [s - 25.0 for s in TRUE_STARTS[:]]
    early = [max(0.0, s) for s in early]

    report = sync_agreement(early, anchors_at(TRUE_STARTS))

    assert report.agreement < 0.2


def test_a_small_error_is_tolerated():
    report = sync_agreement([s + 1.2 for s in TRUE_STARTS], anchors_at(TRUE_STARTS))

    assert report.agreement == 1.0


def test_too_few_anchors_cannot_verify_anything():
    report = sync_agreement(TRUE_STARTS, {0: LineAnchor(0, 10.0, 13.0, 5, 6)})

    assert not report.verifiable


def test_a_whole_song_alignment_that_already_agrees_is_kept():
    decision = decide_alignment(TRUE_STARTS, [s + 0.3 for s in TRUE_STARTS], anchors_at(TRUE_STARTS))

    assert decision.method == "whole-song" and decision.ok and decision.concern == ""


def test_a_drifted_whole_song_alignment_is_replaced_by_the_anchored_one():
    drifted = [s - 25.0 for s in TRUE_STARTS]

    decision = decide_alignment(drifted, TRUE_STARTS, anchors_at(TRUE_STARTS))

    assert decision.method == "anchored" and decision.ok


def test_when_neither_alignment_agrees_the_song_is_set_aside_with_a_reason():
    both_bad = [s - 25.0 for s in TRUE_STARTS]
    also_bad = [s + 30.0 for s in TRUE_STARTS]

    decision = decide_alignment(both_bad, also_bad, anchors_at(TRUE_STARTS))

    assert not decision.ok
    assert "timing" in decision.concern.lower() and "review" in decision.concern.lower()


def test_when_the_timing_cannot_be_checked_the_whole_song_alignment_is_used_and_nothing_is_flagged():
    decision = decide_alignment(TRUE_STARTS, TRUE_STARTS, {})

    assert decision.method == "whole-song" and decision.ok and decision.concern == ""
    assert not decision.report.verifiable


def test_the_small_early_bias_anchors_have_does_not_count_against_a_good_alignment():
    """Real ('Girls Just Want to Have Fun'): Whisper's first-word times run ~2 s before the true line start, and
    lrclib agreed with the alignment, not the anchors."""
    report = sync_agreement([s + 2.3 for s in TRUE_STARTS], anchors_at(TRUE_STARTS))

    assert report.agreement == 1.0


def test_a_constant_large_offset_is_still_disagreement():
    report = sync_agreement([s + 6.0 for s in TRUE_STARTS], anchors_at(TRUE_STARTS))

    assert report.agreement < 0.2


def test_a_drifting_alignment_disagrees_even_though_its_median_offset_is_small():
    drifting = [s + (k - 4) * 3.0 for k, s in enumerate(TRUE_STARTS)]      # -12 s ... +9 s

    assert sync_agreement(drifting, anchors_at(TRUE_STARTS)).agreement < 0.5


# --- the lyrics source's own timestamps as a second opinion ------------------------------------------------------

def noisy_anchors(starts, wrong_every=3):
    """Whisper anchors on a loud recording: every third line is heard somewhere else."""
    return {i: LineAnchor(i, s + (9.0 if i % wrong_every == 0 else 0.0), s + 3.0, 5, 6) for i, s in enumerate(starts)}


LONG_STARTS = [10.0 + 6.0 * i for i in range(12)]


def test_source_timestamps_that_agree_rescue_a_song_whisper_could_only_half_confirm():
    """Real ('Back in the Saddle'): NetEase's own line times agreed within 3 s on 31 of 33 lines and none were 5 s off,
    yet Whisper's noisy anchors on a loud rock recording confirmed only 67-70% and the song was set aside."""
    source = [s + 0.4 for s in LONG_STARTS]

    decision = decide_alignment(LONG_STARTS, LONG_STARTS, noisy_anchors(LONG_STARTS), source_times=source)

    assert decision.ok and decision.concern == ""


def test_without_source_timestamps_the_same_song_is_still_set_aside():
    decision = decide_alignment(LONG_STARTS, LONG_STARTS, noisy_anchors(LONG_STARTS))

    assert not decision.ok


def test_source_timestamps_that_disagree_do_not_rescue_a_song():
    """Real (old 'The Chain'): 60% within 3 s and lines up to 32 s off."""
    source = [s + (30.0 if i % 3 == 0 else 0.4) for i, s in enumerate(LONG_STARTS)]

    decision = decide_alignment(LONG_STARTS, LONG_STARTS, noisy_anchors(LONG_STARTS), source_times=source)

    assert not decision.ok


def test_one_line_far_from_its_source_time_blocks_the_rescue():
    source = [s + 0.4 for s in LONG_STARTS]
    source[5] += 8.0                                       # a single line 8 s away

    decision = decide_alignment(LONG_STARTS, LONG_STARTS, noisy_anchors(LONG_STARTS), source_times=source)

    assert not decision.ok


def test_source_timestamps_never_rescue_a_song_whisper_contradicts():
    """Whisper agreeing on almost nothing means the source's timing is not evidence the video is right."""
    hopeless = {i: LineAnchor(i, s + 25.0, s + 28.0, 5, 6) for i, s in enumerate(LONG_STARTS)}

    decision = decide_alignment(LONG_STARTS, LONG_STARTS, hopeless, source_times=list(LONG_STARTS))

    assert not decision.ok


def test_a_consistent_offset_of_a_second_or_two_from_the_source_is_allowed():
    source = [s - 2.0 for s in LONG_STARTS]                # another edition with a slightly longer intro

    decision = decide_alignment(LONG_STARTS, LONG_STARTS, noisy_anchors(LONG_STARTS), source_times=source)

    assert decision.ok


def test_missing_or_mismatched_source_timestamps_are_ignored():
    for bad in (None, [], LONG_STARTS[:-1]):
        assert not decide_alignment(LONG_STARTS, LONG_STARTS, noisy_anchors(LONG_STARTS), source_times=bad).ok
