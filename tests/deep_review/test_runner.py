"""deep_review.runner: the orchestrator. Diagnose every set-aside song; clear the ones that already pass;
leave alignment-only/damaged-audio/unknown songs untouched and reported; for LYRICS_WRONG songs, research a
correction and only apply + redo it when the research came back confident -- and even then, "fixed" is
whatever the REAL post-redo timing check says, never the research step's own self-reported confidence.
Song text is invented."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo.models import LyricLine, Song, Word, load_song, save_song
from lyricvideo.pipeline import HeldBeforeVideo

from deep_review.research import Research, ResearchAttempt
from deep_review.runner import MAX_ATTEMPTS, SongResult
from deep_review.runner import run_deep_review as _real_run_deep_review

LINES = [f"line number {i}" for i in range(1, 9)]


def run_deep_review(work_root, *args, **kwargs):
    """Test-only wrapper: defaults needs_human_dir to somewhere under this test's own tmp_path (derived
    from work_root, always tmp_path / "work" in every test here) instead of the real default -- a call
    that forgets to override it must never write into this actual project's real needs_human folder."""
    kwargs.setdefault("needs_human_dir", Path(work_root).parent / "needs_human")
    return _real_run_deep_review(work_root, *args, **kwargs)


def _song_dir(tmp_path, slug, concern="", audio_path="song.mp3"):
    d = tmp_path / "work" / slug
    d.mkdir(parents=True)
    lines = [LyricLine(words=[Word(w, 10.0 * (i + 1), 10.0 * (i + 1) + 0.5) for w in line.split()])
             for i, line in enumerate(LINES)]
    save_song(Song(title=slug, audio_path=audio_path, lines=lines, lyrics_accuracy_concern=concern), d / "lyrics_timed.json")
    return d


def _already_passing(tmp_path, slug, concern):
    """A song diagnose_song() will call ALREADY_PASSES: a stale concern, nothing actually wrong -- needs a
    transcript that matches the saved timing exactly, or check_saved_song() can't confirm a pass at all."""
    work_dir = _song_dir(tmp_path, slug, concern=concern)
    words = [
        {"word": w, "start": 10.0 * (i + 1), "end": 10.0 * (i + 1) + 0.5}
        for i, line in enumerate(LINES) for w in line.split()
    ]
    (work_dir / "transcript.json").write_text(
        json.dumps({"text": " ".join(w["word"] for w in words), "words": words}), encoding="utf-8",
    )
    return work_dir


def _lyrics_wrong(tmp_path, slug):
    """A song diagnose_song() will call LYRICS_WRONG via a plain (non-gate) accuracy concern -- doesn't
    need a transcript on disk, matching diagnosis.py's own short-circuit for this case."""
    return _song_dir(tmp_path, slug, concern="Only 40% of these lyrics match what is sung; lines 1-4.")


def _lyrics_wrong_with_share(tmp_path, slug, bad_line_count):
    """A song diagnose_song() will call LYRICS_WRONG via the gate path, with a real, controllable
    sync_share: the last `bad_line_count` of 8 lines are placed on the singing but what's actually heard
    there is different words entirely (genuinely wrong lyrics, not just mistimed) -- share = (8 -
    bad_line_count) / 8."""
    work_dir = tmp_path / "work" / slug
    work_dir.mkdir(parents=True)
    lines = [LyricLine(words=[Word(w, 10.0 + 8.0 * k + 0.4 * i, 10.0 + 8.0 * k + 0.4 * i + 0.3)
                               for i, w in enumerate(line.split())])
             for k, line in enumerate(LINES)]
    bad = set(range(8 - bad_line_count, 8))
    heard = []
    for k, line in enumerate(LINES):
        texts = [f"wrong{k}{i}" for i in range(len(line.split()))] if k in bad else line.split()
        heard += [{"word": w, "start": 10.0 + 8.0 * k + 0.4 * i, "end": 10.0 + 8.0 * k + 0.4 * i + 0.3}
                  for i, w in enumerate(texts)]
    concern = (
        f"SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only {(8 - bad_line_count) / 8:.0%} "
        f"of the lines start within half a second of where they are sung (90% are needed); lines "
        f"{', '.join(str(n) for n in sorted(n + 1 for n in bad))} are off."
    )
    save_song(Song(title=slug, audio_path="song.mp3", lines=lines, lyrics_accuracy_concern=concern), work_dir / "lyrics_timed.json")
    (work_dir / "transcript.json").write_text(json.dumps({"text": " ".join(w["word"] for w in heard), "words": heard}), encoding="utf-8")
    return work_dir


def _damaged_audio(tmp_path, slug):
    return _song_dir(tmp_path, slug, concern="SET ASIDE FOR REVIEW -- damaged source audio: partial file.")


class _StubAnthropic:
    """Never actually called in these tests -- run_deep_review's research step is monkeypatched. Stands in
    for "some real client object" so call sites that just pass it through don't need a real one."""


def _patch_research(monkeypatch, result_or_factory, cost_usd=0.0):
    """`result_or_factory` is a bare Research | None (or a factory returning one) -- wrapped here in the real
    ResearchAttempt shape research_lyrics() actually returns, so existing tests don't need to know about
    cost tracking unless they're the ones testing it (pass cost_usd to test that specifically)."""
    calls = []

    def fake(client, title, artist, lines, segments, mismatched_lines=None, **kwargs):
        calls.append((title, lines))
        research = result_or_factory(title) if callable(result_or_factory) else result_or_factory
        return ResearchAttempt(research, cost_usd)

    monkeypatch.setattr("deep_review.runner.research_lyrics", fake)
    return calls


def _patch_pipeline(monkeypatch, outcome_or_factory):
    calls = []

    def fake(audio_path, work_dir, title=None, start_stage="identify", **kwargs):
        calls.append((work_dir, title, start_stage))
        outcome = outcome_or_factory(work_dir) if callable(outcome_or_factory) else outcome_or_factory
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("deep_review.runner.run_pipeline", fake)
    monkeypatch.setattr("deep_review.runner.backup_song_outputs", lambda *a, **k: None)
    return calls


def _result_for(slug, results: list[SongResult]) -> SongResult:
    return next(r for r in results if r.slug == slug)


# --- routing by diagnosis category --------------------------------------------------------

def test_an_already_passing_song_is_cleared_and_counted_as_passed(tmp_path, monkeypatch):
    _already_passing(tmp_path, "song-a", concern="SET ASIDE FOR REVIEW -- only 94% of the words start.")
    _patch_research(monkeypatch, None)
    _patch_pipeline(monkeypatch, Path("song-a.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-a"])

    result = _result_for("song-a", results)
    assert result.category == "already_passes"
    assert result.action == "cleared"
    assert result.passed is True
    assert load_song(tmp_path / "work" / "song-a" / "lyrics_timed.json").lyrics_accuracy_concern == ""


# --- skip research entirely for a near-miss: owner, 2026-09-22, "song like 89.7. not sure if i need to
# spend more money on those as well" -- real evidence (1979: 3 attempts, never moved, its own research said
# the lyrics probably already match the studio album) says a song already this close is more likely a
# borderline alignment case than genuinely wrong lyrics ------------------------------------------------

def test_a_near_miss_song_is_skipped_with_zero_cost_and_no_attempt(tmp_path, monkeypatch):
    _lyrics_wrong_with_share(tmp_path, "song-u", bad_line_count=1)   # 87.5% -- above the 85% cutoff
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], citations=[], confidence="high"))
    pipeline_calls = _patch_pipeline(monkeypatch, Path("song-u.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-u"])

    result = _result_for("song-u", results)
    assert result.action == "left for manual review"
    assert result.passed is False
    assert result.attempts == 0
    assert result.cost_usd == 0.0
    assert research_calls == [] and pipeline_calls == []
    assert "87.5%" in result.detail or "close" in result.detail.lower()


def test_a_song_further_from_passing_still_gets_researched(tmp_path, monkeypatch):
    _lyrics_wrong_with_share(tmp_path, "song-v", bad_line_count=2)   # 75% -- below the cutoff
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], citations=[], confidence="low"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-v"])

    result = _result_for("song-v", results)
    assert len(research_calls) > 0
    assert result.attempts > 0


def test_a_damaged_audio_song_is_reported_and_never_touched(tmp_path, monkeypatch):
    _damaged_audio(tmp_path, "song-b")
    research_calls = _patch_research(monkeypatch, None)
    pipeline_calls = _patch_pipeline(monkeypatch, Path("song-b.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-b"])

    result = _result_for("song-b", results)
    assert result.category == "damaged_audio"
    assert result.action == "skipped"
    assert result.passed is False
    assert research_calls == [] and pipeline_calls == []


# --- the LYRICS_WRONG path ------------------------------------------------------------------

def test_a_confident_research_result_is_applied_and_redone_and_counted_by_the_real_recheck(tmp_path, monkeypatch):
    work_dir = _lyrics_wrong(tmp_path, "song-c")
    _patch_research(monkeypatch, Research(lines=["corrected one", "corrected two"], citations=["genius.com/x"], confidence="high"))

    def succeed(wd):
        # A real redo would rewrite lyrics_timed.json with a cleared concern -- simulate that here so the
        # runner's own post-redo recheck sees a passing song.
        song = load_song(wd / "lyrics_timed.json")
        save_song(song.__class__(**{**song.__dict__, "lyrics_accuracy_concern": ""}), wd / "lyrics_timed.json")
        return wd / "song-c.mp4"

    pipeline_calls = _patch_pipeline(monkeypatch, succeed)

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-c"])

    result = _result_for("song-c", results)
    assert result.action == "researched_and_redone"
    assert result.passed is True
    assert pipeline_calls == [(work_dir, "song-c", "fetch_lyrics")]
    assert (work_dir / "lyrics_owner.txt").read_text(encoding="utf-8").splitlines() == ["corrected one", "corrected two"]
    # A passing result must not still show the ORIGINAL pre-fix concern -- real incident, 2026-09-22: "dreams"
    # passed and the report still read "SET ASIDE FOR REVIEW..." next to a [PASS] mark, reading like a failure.
    assert "match what is sung" not in result.detail


def test_a_redo_that_keeps_being_held_exhausts_every_attempt_before_giving_up(tmp_path, monkeypatch):
    """A HeldBeforeVideo that never resolves (the fixture's on-disk state never actually changes, so
    re-diagnosis keeps finding the same lyrics_wrong problem) must try every attempt, not just one, before
    landing on manual review -- owner: "if all avenues fail then straight to human attention after all
    attemps have been made"."""
    research_calls = _patch_research(monkeypatch, Research(lines=["a guess"], citations=[], confidence="high"))
    _lyrics_wrong(tmp_path, "song-d")
    pipeline_calls = _patch_pipeline(monkeypatch, HeldBeforeVideo("still only 60% in sync"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-d"])

    result = _result_for("song-d", results)
    assert result.action == "left for manual review"
    assert result.passed is False
    assert "60%" in result.detail
    assert result.attempts == MAX_ATTEMPTS
    assert len(research_calls) == MAX_ATTEMPTS
    assert len(pipeline_calls) == MAX_ATTEMPTS


# --- retries: "try for 100%... after all attempts have been made", and "not identical re attemps" ---------

def test_a_low_confidence_first_attempt_is_retried_with_what_failed_and_a_confident_second_attempt_succeeds(tmp_path, monkeypatch):
    _lyrics_wrong(tmp_path, "song-p")
    seen_previous_attempts = []

    def sequence(client, title, artist, lines, segments, mismatched_lines=None, previous_attempts=None, **kwargs):
        seen_previous_attempts.append(previous_attempts)
        if len(seen_previous_attempts) == 1:
            research = Research(lines=["a shaky first guess"], citations=[], confidence="low", notes="not sure")
        else:
            research = Research(lines=["a confident second guess"], citations=["genius.com/x"], confidence="high")
        return ResearchAttempt(research, 0.01)

    monkeypatch.setattr("deep_review.runner.research_lyrics", sequence)

    def succeed(wd):
        song = load_song(wd / "lyrics_timed.json")
        save_song(song.__class__(**{**song.__dict__, "lyrics_accuracy_concern": ""}), wd / "lyrics_timed.json")
        return wd / "song-p.mp4"

    _patch_pipeline(monkeypatch, succeed)

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-p"])

    result = _result_for("song-p", results)
    assert result.action == "researched_and_redone" and result.passed is True
    assert result.attempts == 2
    assert result.cost_usd == pytest.approx(0.02)   # both attempts' real cost accumulated, not just the winner's
    assert len(seen_previous_attempts) == 2
    assert seen_previous_attempts[0] is None                                        # nothing tried yet
    assert "shaky first guess" in seen_previous_attempts[1][0]["lines"][0]           # told what already failed


def test_exhausting_every_attempt_without_a_confident_result_is_left_for_manual_review(tmp_path, monkeypatch):
    _lyrics_wrong(tmp_path, "song-q")
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], citations=[], confidence="low"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-q"])

    result = _result_for("song-q", results)
    assert result.action == "left for manual review"
    assert result.passed is False
    assert result.attempts == MAX_ATTEMPTS
    assert len(research_calls) == MAX_ATTEMPTS


def test_retries_stop_early_once_the_song_is_no_longer_lyrics_wrong(tmp_path, monkeypatch):
    """A redo can genuinely fix most of a song's lyrics while timing still falls short -- if what's left
    is now alignment-only (correct lyrics, imprecise sync -- a different, separate problem), this program
    must stop spending more research attempts on it rather than burning through the rest pointlessly."""
    from deep_review.diagnosis import Category as DiagCategory
    from deep_review.diagnosis import Diagnosis

    _lyrics_wrong(tmp_path, "song-r")
    state = {"redone": False}

    def fake_diagnose(work_dir):
        if state["redone"]:
            return Diagnosis(DiagCategory.ALIGNMENT_ONLY, detail="now only alignment is off", sync_share=0.875)
        return Diagnosis(DiagCategory.LYRICS_WRONG, detail="lyrics wrong", sync_share=0.625, mismatched_lines=[6, 7, 8])

    monkeypatch.setattr("deep_review.runner.diagnose_song", fake_diagnose)
    call_count = []
    monkeypatch.setattr(
        "deep_review.runner.research_lyrics",
        lambda *a, **k: (call_count.append(1), ResearchAttempt(Research(lines=["x"], citations=[], confidence="high"), 0.0))[1],
    )

    def fake_pipeline(audio_path, wd, title=None, start_stage="identify", **kwargs):
        state["redone"] = True
        return wd / "song-r.mp4"

    monkeypatch.setattr("deep_review.runner.run_pipeline", fake_pipeline)
    monkeypatch.setattr("deep_review.runner.backup_song_outputs", lambda *a, **k: None)

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-r"])

    result = _result_for("song-r", results)
    assert call_count == [1]      # stopped after ONE attempt -- no point trying more lyrics research
    assert result.action == "left for manual review"
    assert result.passed is False
    assert "alignment_only" in result.detail
    assert result.after_share == 0.875


# --- per-song cost cap: owner, 2026-09-22, "i cant do any more than 5 cents per song ... if i have to do
# more, my opinion, its not worth doing" -----------------------------------------------------------------

def test_an_attempt_that_alone_exceeds_the_cost_cap_stops_further_attempts(tmp_path, monkeypatch):
    """The cap can't prevent a single expensive call (its cost is only known after it happens), but it must
    stop any compounding beyond that -- one attempt that already blows the budget gets no second attempt."""
    research_calls = _patch_research(
        monkeypatch, Research(lines=["a guess"], citations=[], confidence="high"), cost_usd=0.08,
    )
    _lyrics_wrong(tmp_path, "song-s")
    pipeline_calls = _patch_pipeline(monkeypatch, HeldBeforeVideo("still off"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-s"])

    result = _result_for("song-s", results)
    assert result.action == "left for manual review"
    assert result.attempts == 1
    assert len(research_calls) == 1 and len(pipeline_calls) == 1
    assert result.cost_usd == pytest.approx(0.08)
    assert "cost cap" in result.detail.lower() and "0.08" in result.detail


def test_attempts_that_stay_under_the_cap_are_still_retried_normally(tmp_path, monkeypatch):
    research_calls = _patch_research(
        monkeypatch, Research(lines=["a guess"], citations=[], confidence="high"), cost_usd=0.01,
    )
    _lyrics_wrong(tmp_path, "song-t")
    _patch_pipeline(monkeypatch, HeldBeforeVideo("still off"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-t"])

    result = _result_for("song-t", results)
    assert result.attempts == MAX_ATTEMPTS      # 3 x $0.01 = $0.03, never reaches the $0.05 cap
    assert len(research_calls) == MAX_ATTEMPTS
    assert result.action == "left for manual review"   # still exhausted normally, just not cost-capped


def test_low_confidence_research_is_never_applied_or_redone(tmp_path, monkeypatch):
    _lyrics_wrong(tmp_path, "song-e")
    _patch_research(monkeypatch, Research(lines=["a shaky guess"], citations=[], confidence="low", notes="couldn't confirm"))
    pipeline_calls = _patch_pipeline(monkeypatch, Path("song-e.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-e"])

    result = _result_for("song-e", results)
    assert result.action == "left for manual review"
    assert result.passed is False
    assert pipeline_calls == []


def test_no_usable_research_result_is_left_for_manual_review(tmp_path, monkeypatch):
    _lyrics_wrong(tmp_path, "song-f")
    _patch_research(monkeypatch, None)
    pipeline_calls = _patch_pipeline(monkeypatch, Path("song-f.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-f"])

    assert _result_for("song-f", results).action == "left for manual review"
    assert pipeline_calls == []


# --- dry run + limits ------------------------------------------------------------------------

def test_dry_run_never_touches_a_real_file_or_triggers_a_redo(tmp_path, monkeypatch):
    work_dir = _lyrics_wrong(tmp_path, "song-g")
    before = (work_dir / "lyrics_timed.json").read_text(encoding="utf-8")
    _patch_research(monkeypatch, Research(lines=["corrected"], citations=["x"], confidence="high"))
    pipeline_calls = _patch_pipeline(monkeypatch, Path("song-g.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-g"], dry_run=True)

    result = _result_for("song-g", results)
    assert result.passed is None
    assert "corrected" in result.detail
    assert pipeline_calls == []
    assert not (work_dir / "lyrics_owner.txt").exists()
    assert (work_dir / "lyrics_timed.json").read_text(encoding="utf-8") == before


def test_a_limit_caps_how_many_songs_are_processed(tmp_path, monkeypatch):
    for slug in ("song-h", "song-i", "song-j"):
        _lyrics_wrong(tmp_path, slug)
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], citations=[], confidence="low"))
    _patch_pipeline(monkeypatch, Path("x.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-h", "song-i", "song-j"], limit=2)

    assert len(results) == 2
    songs_researched = {title for title, _lines in research_calls}
    assert songs_researched == {"song-h", "song-i"}   # never song-j -- the limit applies to songs, not attempts


# --- ordering: closest to passing first -----------------------------------------------------

def test_fixable_songs_are_researched_closest_to_passing_first(tmp_path, monkeypatch):
    """Owner request, 2026-09-22: work the easy ones first -- a song already at 75% is one small correction
    away, a song at 50% needs much more to get right. Order should reflect that. (All three stay under the
    85% near-miss cutoff -- see test_a_near_miss_song_is_skipped_with_zero_cost_and_no_attempt for that.)"""
    _lyrics_wrong_with_share(tmp_path, "song-far", bad_line_count=4)     # 50.0%
    _lyrics_wrong_with_share(tmp_path, "song-close", bad_line_count=2)   # 75.0%
    _lyrics_wrong_with_share(tmp_path, "song-mid", bad_line_count=3)     # 62.5%
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], citations=[], confidence="low"))

    run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-far", "song-close", "song-mid"])

    order = list(dict.fromkeys(title for title, _lines in research_calls))   # first-seen order, deduped
    assert order == ["song-close", "song-mid", "song-far"]


def test_a_pilot_limit_after_sorting_picks_the_easiest_songs_first(tmp_path, monkeypatch):
    _lyrics_wrong_with_share(tmp_path, "song-far", bad_line_count=4)
    _lyrics_wrong_with_share(tmp_path, "song-close", bad_line_count=2)
    _lyrics_wrong_with_share(tmp_path, "song-mid", bad_line_count=3)
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], citations=[], confidence="low"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-far", "song-close", "song-mid"], limit=1)

    assert [r.slug for r in results] == ["song-close"]
    assert {title for title, _ in research_calls} == {"song-close"}


# --- needs_human marker wiring --------------------------------------------------------------

def test_a_song_that_does_not_pass_gets_a_needs_human_marker(tmp_path, monkeypatch):
    _damaged_audio(tmp_path, "song-m")
    marker_dir = tmp_path / "needs_human"

    run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-m"], needs_human_dir=marker_dir)

    assert (marker_dir / "song-m.txt").exists()
    assert "damaged_audio" in (marker_dir / "song-m.txt").read_text(encoding="utf-8")


def test_a_song_that_passes_has_no_marker_and_clears_any_earlier_one(tmp_path, monkeypatch):
    _already_passing(tmp_path, "song-n", concern="SET ASIDE FOR REVIEW -- only 94% of the words start.")
    marker_dir = tmp_path / "needs_human"
    marker_dir.mkdir()
    (marker_dir / "song-n.txt").write_text("stale: from an earlier failing run", encoding="utf-8")

    run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-n"], needs_human_dir=marker_dir)

    assert not (marker_dir / "song-n.txt").exists()


def test_a_dry_run_never_touches_the_needs_human_folder(tmp_path, monkeypatch):
    _damaged_audio(tmp_path, "song-o")
    marker_dir = tmp_path / "needs_human"

    run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-o"], dry_run=True, needs_human_dir=marker_dir)

    assert not marker_dir.exists()


def test_one_songs_crash_does_not_stop_the_rest(tmp_path, monkeypatch):
    """A research call that keeps raising is just another failed avenue -- it exhausts every attempt like
    any other failure mode (still ending in manual review, with the real error kept in the detail), and
    never stops the rest of the run."""
    _lyrics_wrong(tmp_path, "song-k")
    _lyrics_wrong(tmp_path, "song-l")

    def flaky(client, title, artist, lines, segments, mismatched_lines=None, **kwargs):
        if title == "song-k":
            raise RuntimeError("network blew up")
        return ResearchAttempt(Research(lines=["x"], citations=[], confidence="low"), 0.0)

    monkeypatch.setattr("deep_review.runner.research_lyrics", flaky)
    _patch_pipeline(monkeypatch, Path("x.mp4"))

    results = run_deep_review(tmp_path / "work", _StubAnthropic(), songs=["song-k", "song-l"])

    assert _result_for("song-k", results).action == "left for manual review"
    assert "network blew up" in _result_for("song-k", results).detail
    assert _result_for("song-k", results).attempts == MAX_ATTEMPTS
    assert _result_for("song-l", results).action == "left for manual review"
