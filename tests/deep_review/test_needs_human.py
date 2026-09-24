"""deep_review.needs_human: a small marker file per song this program could not get to a real pass, in
its own folder -- never inside the song's own work/<slug>/ folder, which the rest of the app expects to
find at its usual path -- so the owner can review just the ones that need a human, in one place."""

from deep_review.needs_human import clear_marker, marker_path, write_marker
from deep_review.runner import SongResult


def test_a_result_that_did_not_pass_gets_a_readable_marker_file(tmp_path):
    result = SongResult(
        slug="1979", category="lyrics_wrong", action="researched_and_redone", passed=False,
        before_share=0.62, after_share=0.897, attempts=3, cost_usd=0.0734, detail="lines 1, 16, 19 are off",
    )

    write_marker(tmp_path, result)

    text = marker_path(tmp_path, "1979").read_text(encoding="utf-8")
    assert "1979" in text and "lyrics_wrong" in text and "researched_and_redone" in text
    assert "62.0%" in text and "89.7%" in text
    assert "3" in text and "$0.07" in text
    assert "lines 1, 16, 19 are off" in text


def test_clearing_a_marker_that_exists_removes_it(tmp_path):
    result = SongResult(slug="1979", category="lyrics_wrong", action="researched_and_redone", passed=False)
    write_marker(tmp_path, result)

    clear_marker(tmp_path, "1979")

    assert not marker_path(tmp_path, "1979").exists()


def test_clearing_a_marker_that_never_existed_does_not_raise(tmp_path):
    clear_marker(tmp_path, "never-marked")   # just must not raise
