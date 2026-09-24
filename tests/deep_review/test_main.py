"""The pass-rate / goal-met logic in the CLI's summary -- the number the owner actually reads to know
whether the 90% goal was hit, so it gets its own direct tests rather than trusting it by inspection."""

from deep_review.__main__ import PASS_TARGET, summarize
from deep_review.runner import SongResult


def test_no_results_says_so_plainly():
    assert summarize([]) == "No songs to review."


def test_the_pass_rate_only_counts_decided_songs_and_reports_meeting_the_goal():
    results = [
        SongResult(slug="a", category="already_passes", action="cleared", passed=True),
        SongResult(slug="b", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="c", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="d", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="e", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="f", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="g", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="h", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="i", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="j", category="lyrics_wrong", action="researched_and_redone", passed=False, detail="still 60%"),
    ]

    text = summarize(results)

    assert "9 of 10 decided song(s) now pass (90.0%)" in text
    assert "MEETS the goal" in text
    assert "j" in text and "still 60%" in text


def test_a_pass_rate_below_the_target_says_it_does_not_meet_the_goal():
    results = [
        SongResult(slug="a", category="lyrics_wrong", action="researched_and_redone", passed=True),
        SongResult(slug="b", category="lyrics_wrong", action="left for manual review", passed=False),
    ]

    assert "DOES NOT YET MEET the goal" in summarize(results)


def test_dry_run_results_are_excluded_from_the_rate_and_noted_separately():
    results = [
        SongResult(slug="a", category="lyrics_wrong", action="would research_and_redo (dry run)", passed=None),
        SongResult(slug="b", category="already_passes", action="cleared", passed=True),
    ]

    text = summarize(results)

    assert "1 of 1 decided song(s) now pass (100.0%)" in text
    assert "1 not yet decided" in text


def test_damaged_audio_and_alignment_only_and_failures_are_listed_as_needing_attention():
    results = [
        SongResult(slug="a", category="damaged_audio", action="skipped", passed=False),
        SongResult(slug="b", category="alignment_only", action="skipped", passed=False),
        SongResult(slug="c", category="lyrics_wrong", action="left for manual review", passed=False, detail="unsure"),
        SongResult(slug="d", category="lyrics_wrong", action="failed", passed=False, detail="boom"),
        SongResult(slug="e", category="already_passes", action="cleared", passed=True),
    ]

    text = summarize(results)

    assert "4 song(s) still need a human or a different approach" in text
    for slug in ("a", "b", "c", "d"):
        assert slug in text
    # a cleanly-cleared song is not flagged as needing attention
    assert "e (already_passes)" not in text


def test_pass_target_is_ninety_percent():
    assert PASS_TARGET == 0.90


def test_the_total_real_cost_across_every_song_is_reported():
    results = [
        SongResult(slug="a", category="lyrics_wrong", action="researched_and_redone", passed=True, cost_usd=0.03),
        SongResult(slug="b", category="lyrics_wrong", action="left for manual review", passed=False, cost_usd=0.05),
        SongResult(slug="c", category="already_passes", action="cleared", passed=True),   # no API call, no cost
    ]

    text = summarize(results)

    assert "$0.08" in text
    assert "a" in text and "$0.03" in text
    assert "b" in text and "$0.05" in text


def test_a_run_with_no_api_spend_does_not_show_a_cost_line():
    results = [SongResult(slug="a", category="already_passes", action="cleared", passed=True)]

    assert "$" not in summarize(results)
