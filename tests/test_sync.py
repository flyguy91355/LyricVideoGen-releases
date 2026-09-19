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
