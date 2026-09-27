"""deep_review fixes from the issue #7 full review: a lyric-text concern is never cleared by a timing pass; EASY
CHORD variants are never redone; key-only songs are neither reviewed nor counted as passes; the owner's own
lyrics file survives a failed review; every judgement uses the owner's timing bar; a dry run writes nothing;
the research cost includes the web-search fee. All song text is invented."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo import timing_gate
from lyricvideo.key_decision import KeyDecision, save_decision
from lyricvideo.models import LyricLine, Song, Word, load_song, save_song
from lyricvideo.pipeline import HeldBeforeVideo
from lyricvideo.settings import Settings
from lyricvideo.timing_gate import check_saved_song

import deep_review.__main__ as dr_main
from deep_review import runner
from deep_review.diagnosis import Category, diagnose_song
from deep_review.needs_human import clear_marker, marker_path, write_marker
from deep_review.research import Research, ResearchAttempt, _cost_usd, research_lyrics
from deep_review.runner import SongResult, _clear_concern, list_songs_to_review, run_deep_review

WORDS = [
    "amber", "bramble", "cobalt", "driftwood", "ember", "fennel", "granite", "harbor", "indigo", "juniper",
    "kestrel", "lantern", "meadow", "nettle", "orchard", "pebble", "quarry", "russet", "saffron", "thistle",
    "umber", "velvet", "willow", "yonder", "zephyr", "acorn", "birch", "cedar", "dune", "elm",
    "fjord", "glade", "heath", "isle", "jasper", "knoll", "loam", "marsh", "nook", "oak",
]
LYRIC_TEXT_CONCERN = "Only 58% of these lyrics match what is sung; lines 1-3, 5 could not be matched."


def _lines(n: int) -> list[list[str]]:
    return [WORDS[4 * k:4 * k + 4] for k in range(n)]


def _start(k: int) -> float:
    return 10.0 + 8.0 * k        # 8 s apart: the 2.5 s search window never reaches a neighboring line


def _song(work_root: Path, slug: str, n_lines: int = 8, shifted: tuple[int, ...] = (), concern: str = "",
          mp4: bool = True, transcript: bool = True, key_confirmed: bool = True) -> Path:
    """A rendered song whose lines are placed exactly where they are sung, except `shifted` ones (placed 1 s late:
    out of sync, same words) -- so its gate share is (n - len(shifted)) / n."""
    work_dir = work_root / slug
    work_dir.mkdir(parents=True)
    lines = []
    heard = []
    for k, words in enumerate(_lines(n_lines)):
        offset = 1.0 if k in shifted else 0.0
        lines.append(LyricLine(words=[Word(w, _start(k) + 0.4 * i + offset, _start(k) + 0.4 * i + 0.3 + offset)
                                      for i, w in enumerate(words)]))
        heard += [{"word": w, "start": _start(k) + 0.4 * i, "end": _start(k) + 0.4 * i + 0.3} for i, w in enumerate(words)]
    title = Path(slug).name if Path(slug).name != "easychords" else slug.replace("/", "-")
    save_song(Song(title=title, audio_path="song.mp3", lines=lines, lyrics_accuracy_concern=concern),
              work_dir / "lyrics_timed.json")
    if transcript:
        (work_dir / "transcript.json").write_text(
            json.dumps({"text": " ".join(w["word"] for w in heard), "words": heard,
                        "segments": [{"start": _start(0), "end": _start(n_lines), "text": "invented words"}]}),
            encoding="utf-8",
        )
    if mp4:
        from lyricvideo.pipeline import slugify
        (work_dir / f"{slugify(title)}.mp4").write_bytes(b"not really a video")
    if key_confirmed:
        save_decision(work_dir, KeyDecision(status="confirmed", key="G major", source="owner", chord_key="G major"))
    return work_dir


def _gate_concern(work_dir: Path, needed: float) -> str:
    return check_saved_song(work_dir, needed).concern


class _Stub:
    """Never called: research and the redo are monkeypatched."""


def _patch_research(monkeypatch, research, cost=0.0):
    calls = []

    def fake(client, title, artist, lines, segments, mismatched_lines=None, **kwargs):
        calls.append(title)
        return ResearchAttempt(research, cost)

    monkeypatch.setattr("deep_review.runner.research_lyrics", fake)
    return calls


def _patch_pipeline(monkeypatch, outcome, backup_root: Path | None = None):
    calls = []

    def fake(audio_path, work_dir, title=None, start_stage="identify", **kwargs):
        calls.append(Path(work_dir))
        result = outcome(Path(work_dir)) if callable(outcome) else outcome
        if isinstance(result, Exception):
            raise result
        return result

    backups = []

    def fake_backup(work_dir, slug, now=None):
        if backup_root is None:
            return None
        backups.append(1)
        backup = Path(work_dir) / f"redo_backup_{len(backups)}"
        backup.mkdir(exist_ok=True)
        return backup

    monkeypatch.setattr("deep_review.runner.run_pipeline", fake)
    monkeypatch.setattr("deep_review.runner.backup_song_outputs", fake_backup)
    monkeypatch.setattr("deep_review.runner.load_redo_inputs", lambda wd: (Path(wd) / "song.mp3", Path(wd).name))
    return calls


def _review(work_root, **kwargs):
    kwargs.setdefault("needs_human_dir", Path(work_root).parent / "needs_human")
    return run_deep_review(work_root, _Stub(), **kwargs)


@pytest.fixture(autouse=True)
def _default_bar():
    timing_gate.use_pass_share_from(None)
    yield
    timing_gate.use_pass_share_from(None)


# --- F001: a lyric-text concern is never cleared by a passing timing check ------------------------------------

def test_a_lyric_text_concern_on_a_song_whose_timing_passes_is_still_lyrics_wrong(tmp_path):
    work_dir = _song(tmp_path, "song", concern=LYRIC_TEXT_CONCERN)      # every line exactly on the singing
    assert check_saved_song(work_dir).passes

    assert diagnose_song(work_dir).category == Category.LYRICS_WRONG


@pytest.mark.parametrize("concern", [
    LYRIC_TEXT_CONCERN + " SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only 80% of the lines start "
    "within half a second of where they are sung (90% are needed); lines 2 are off.",
    "AI review found the lyrics likely wrong at lines 3-4: a different verse is sung.",
    "The lyrics look like a different song's.",                      # the older Claude text check: free text
])
def test_combined_and_other_lyric_text_concerns_are_lyrics_wrong_too(tmp_path, concern):
    work_dir = _song(tmp_path, "song", concern=concern)

    assert diagnose_song(work_dir).category == Category.LYRICS_WRONG


def test_the_review_never_clears_a_lyric_text_concern(tmp_path, monkeypatch):
    work_dir = _song(tmp_path / "work", "song", concern=LYRIC_TEXT_CONCERN)
    _patch_research(monkeypatch, Research(lines=["x"], confidence="low"))
    pipeline_calls = _patch_pipeline(monkeypatch, None)

    results = _review(tmp_path / "work", songs=["song"])

    assert results[0].action != "cleared" and results[0].passed is False
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == LYRIC_TEXT_CONCERN
    assert pipeline_calls == []


def test_clearing_refuses_a_lyric_text_concern_outright(tmp_path):
    work_dir = _song(tmp_path, "song", concern=LYRIC_TEXT_CONCERN)

    with pytest.raises(ValueError):
        _clear_concern(work_dir)
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == LYRIC_TEXT_CONCERN


def test_a_song_not_yet_checked_against_the_audio_is_neither_cleared_nor_researched(tmp_path, monkeypatch):
    """verify_lyrics' "not checked yet" hold carries no verdict on the words: the free local check must answer it,
    not paid research -- and a passing timing re-check must never clear it either."""
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    work_dir = _song(tmp_path / "work", "song", concern=UNCHECKED_HOLD)     # timing passes
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], confidence="high"))
    pipeline_calls = _patch_pipeline(monkeypatch, None)

    assert diagnose_song(work_dir).category == Category.UNKNOWN
    results = _review(tmp_path / "work", songs=["song"])

    assert results[0].action == "skipped" and results[0].passed is None
    assert research_calls == [] and pipeline_calls == []
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == UNCHECKED_HOLD
    with pytest.raises(ValueError):
        _clear_concern(work_dir)


def test_the_held_marker_name_diagnosis_reads_is_the_one_the_pipeline_writes():
    """diagnosis.py copies pipeline.HELD_MARKER (importing pipeline there pulls in torch): the two must not drift."""
    from lyricvideo.pipeline import HELD_MARKER
    from deep_review import diagnosis

    assert diagnosis._HELD_MARKER == HELD_MARKER


def test_a_stale_timing_concern_that_now_passes_is_still_cleared(tmp_path, monkeypatch):
    stale = "SET ASIDE FOR REVIEW -- only 94% of the words start within half a second of where they are sung."
    work_dir = _song(tmp_path / "work", "song", concern=stale)

    results = _review(tmp_path / "work", songs=["song"])

    assert results[0].action == "cleared" and results[0].passed is True
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == ""


# --- F002: EASY CHORD variants are never redone, and a nested slug never crashes the run -----------------------

def test_the_default_list_leaves_out_easy_chord_variants(tmp_path):
    work = tmp_path / "work"
    _song(work, "song-a", concern=LYRIC_TEXT_CONCERN)
    _song(work, "song-a/easychords", concern=LYRIC_TEXT_CONCERN)

    assert list_songs_to_review(work) == ["song-a"]


def test_a_named_easy_chord_variant_is_reported_but_never_researched_or_redone(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _song(work, "song-a", concern=LYRIC_TEXT_CONCERN)
    _song(work, "song-a/easychords", concern=LYRIC_TEXT_CONCERN)
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], confidence="high"), cost=0.01)
    pipeline_calls = _patch_pipeline(monkeypatch, HeldBeforeVideo("still off"))

    results = _review(work, songs=["song-a/easychords", "song-a"])

    variant = next(r for r in results if r.slug == "song-a/easychords")
    assert variant.action == "skipped" and variant.passed is None and variant.cost_usd == 0.0
    assert all(call.name != "easychords" for call in pipeline_calls)
    assert "song-a-easychords" not in research_calls


def test_a_nested_slug_gets_a_flat_marker_file_that_can_be_cleared(tmp_path):
    write_marker(tmp_path, SongResult(slug="song-a/easychords", category="unknown", action="skipped"))

    assert marker_path(tmp_path, "song-a/easychords").parent == tmp_path
    assert marker_path(tmp_path, "song-a/easychords").exists()
    clear_marker(tmp_path, "song-a/easychords")
    assert not marker_path(tmp_path, "song-a/easychords").exists()


def test_a_marker_that_cannot_be_written_never_stops_the_rest(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _song(work, "song-a", concern="SET ASIDE FOR REVIEW -- damaged source audio: partial file.")
    _song(work, "song-b", concern="SET ASIDE FOR REVIEW -- damaged source audio: partial file.")

    def broken(root, result):
        raise OSError("disk full")

    monkeypatch.setattr("deep_review.runner.write_marker", broken)

    results = _review(work, songs=["song-a", "song-b"])

    assert [r.slug for r in results] == ["song-a", "song-b"]


def test_a_fixed_song_rebuilds_its_existing_easy_chord_version(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work_dir = _song(work, "song-a", concern=LYRIC_TEXT_CONCERN)
    _song(work, "song-a/easychords", concern=LYRIC_TEXT_CONCERN)
    _patch_research(monkeypatch, Research(lines=["new words"], confidence="high"))

    def fixed(wd):
        song = load_song(wd / "lyrics_timed.json")
        save_song(Song(**{**song.__dict__, "lyrics_accuracy_concern": ""}), wd / "lyrics_timed.json")
        return wd / "song-a.mp4"

    _patch_pipeline(monkeypatch, fixed)
    rebuilt = []
    monkeypatch.setattr("deep_review.runner.build_capo_variant", lambda wd, **kw: rebuilt.append(Path(wd)))

    results = _review(work, songs=["song-a"])

    assert results[0].passed is True
    assert rebuilt == [work_dir]


# --- F024: key-only songs are not lyrics problems, and never passes --------------------------------------------

def test_a_song_flagged_only_for_its_key_is_not_listed_and_the_pilot_goes_to_a_real_lyrics_problem(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _song(work, "aaa-key-only", key_confirmed=False)                    # made before the key check: key unchecked
    _song(work, "zzz-wrong-lyrics", concern=LYRIC_TEXT_CONCERN)
    research_calls = _patch_research(monkeypatch, Research(lines=["x"], confidence="low"))

    assert list_songs_to_review(work) == ["zzz-wrong-lyrics"]
    results = _review(work, limit=1)

    assert [r.slug for r in results] == ["zzz-wrong-lyrics"]
    assert research_calls


def test_a_song_held_at_the_key_check_is_waiting_for_key_and_never_a_pass(tmp_path):
    work = tmp_path / "work"
    work_dir = _song(work, "held", mp4=False, key_confirmed=False)
    (work_dir / "held_before_video.json").write_text(
        json.dumps({"reason": "Key check: the song's key needs your confirmation.", "at": "now"}), encoding="utf-8",
    )

    assert diagnose_song(work_dir).category == Category.WAITING_FOR_KEY
    results = _review(work, songs=["held"])
    assert results[0].passed is None and results[0].action.startswith("skipped")
    text = dr_main.summarize(results)
    assert "[PASS]" not in text and "waiting for their key" in text
    assert not (tmp_path / "needs_human").exists() or not any((tmp_path / "needs_human").iterdir())


def test_a_redo_that_fixes_the_lyrics_and_then_waits_for_the_key_is_reported_as_fixed(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work_dir = _song(work, "song", concern=LYRIC_TEXT_CONCERN)
    _patch_research(monkeypatch, Research(lines=["corrected words"], confidence="high"))

    def held_for_key(wd):
        song = load_song(wd / "lyrics_timed.json")
        save_song(Song(**{**song.__dict__, "lyrics_accuracy_concern": ""}), wd / "lyrics_timed.json")
        return HeldBeforeVideo("Key check: the song's key needs your confirmation.")

    _patch_pipeline(monkeypatch, held_for_key)

    results = _review(work, songs=["song"])

    assert results[0].action == "lyrics fixed; waiting for Set Key"
    assert results[0].passed is True and results[0].attempts == 1
    assert (work_dir / "lyrics_owner.txt").read_text(encoding="utf-8").splitlines() == ["corrected words"]


# --- F025: the owner's own lyrics file is never lost to a failed review ----------------------------------------

def test_a_failed_review_puts_the_owners_own_lyrics_back_and_backs_them_up(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work_dir = _song(work, "song", concern=LYRIC_TEXT_CONCERN)
    owner_bytes = "the owner's own hand-fixed line\r\nand a second one\n".encode("utf-8")   # mixed endings: kept exactly
    (work_dir / "lyrics_owner.txt").write_bytes(owner_bytes)
    _patch_research(monkeypatch, Research(lines=["a rejected guess"], confidence="high"))
    _patch_pipeline(monkeypatch, HeldBeforeVideo("SET ASIDE FOR REVIEW -- the lyric timing is still off"), backup_root=work_dir)

    results = _review(work, songs=["song"])

    assert results[0].passed is False and results[0].attempts == runner.MAX_ATTEMPTS
    assert (work_dir / "lyrics_owner.txt").read_bytes() == owner_bytes
    assert (work_dir / "redo_backup_1" / "lyrics_owner.txt").read_bytes() == owner_bytes
    assert "redo_backup_1" in results[0].detail


def test_a_failed_review_leaves_no_guessed_owner_lyrics_behind(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work_dir = _song(work, "song", concern=LYRIC_TEXT_CONCERN)
    _patch_research(monkeypatch, Research(lines=["a rejected guess"], confidence="high"))
    _patch_pipeline(monkeypatch, RuntimeError("redo blew up"))

    results = _review(work, songs=["song"])

    assert results[0].passed is False
    assert not (work_dir / "lyrics_owner.txt").exists()


# --- F089 / F090: the owner's timing bar, and a dry run that writes nothing ------------------------------------

def test_a_song_between_the_default_bar_and_the_owners_higher_bar_is_not_cleared(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work_dir = _song(work, "song", n_lines=10, shifted=(9,))            # 90%: passes 90, fails the owner's 95
    concern = _gate_concern(work_dir, 0.95)
    save_song(Song(**{**load_song(work_dir / "lyrics_timed.json").__dict__, "lyrics_accuracy_concern": concern}),
              work_dir / "lyrics_timed.json")
    _patch_research(monkeypatch, Research(lines=["x"], confidence="low"))

    results = _review(work, songs=["song"], settings=Settings(timing_pass_percent=95))

    assert results[0].action != "cleared" and results[0].passed is not True
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == concern


def test_the_default_listing_holds_at_the_owners_lower_bar_not_the_default(tmp_path):
    work = tmp_path / "work"
    work_dir = _song(work, "song", n_lines=8, shifted=(7,))             # 87.5%: fails 90, passes the owner's 85
    before = (work_dir / "lyrics_timed.json").read_bytes()

    assert list_songs_to_review(work, needed=0.85) == []
    assert (work_dir / "lyrics_timed.json").read_bytes() == before


def test_a_dry_run_without_a_song_list_writes_nothing_at_all(tmp_path, monkeypatch):
    work = tmp_path / "work"
    failing = _song(work, "failing", n_lines=8, shifted=(6, 7))         # 75%, no concern written yet
    stale = _song(work, "stale", concern="SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only 50% of "
                  "the lines start within half a second of where they are sung (90% are needed); lines 1 are off.")
    before = {p: p.read_bytes() for p in (failing / "lyrics_timed.json", stale / "lyrics_timed.json")}
    log_file = tmp_path / "cleared.json"
    monkeypatch.setattr("lyricvideo.cleared_log.LOG_FILE", log_file)
    _patch_research(monkeypatch, Research(lines=["x"], confidence="low"))

    results = _review(work, dry_run=True)

    # "stale" passes the gate now: a real run would release its hold (and log it); a dry run only leaves it out.
    assert [r.slug for r in results] == ["failing"]
    assert results[0].category == "alignment_only"          # judged failing, not "no concern recorded"
    for path, content in before.items():
        assert path.read_bytes() == content
    assert not log_file.exists()
    assert not (tmp_path / "needs_human").exists()


def test_the_cli_judges_at_the_owners_saved_bar_and_puts_the_default_back(tmp_path, monkeypatch):
    monkeypatch.setattr(dr_main.Settings, "load", staticmethod(lambda *a, **k: Settings(timing_pass_percent=95)))
    monkeypatch.setattr(dr_main.anthropic, "Anthropic", lambda *a, **k: _Stub())
    monkeypatch.setattr(dr_main, "load_dotenv", lambda *a, **k: None)
    seen = {}

    def fake_run(work, client, **kwargs):
        seen["bar"] = timing_gate.pass_share()
        seen["settings"] = kwargs["settings"].timing_pass_percent
        return []

    monkeypatch.setattr(dr_main, "run_deep_review", fake_run)

    assert dr_main.main(["--work", str(tmp_path)]) == 0
    assert seen == {"bar": 0.95, "settings": 95}
    assert timing_gate.pass_share() == timing_gate.PASS_SHARE


# --- F091: the web-search fee counts toward the cost and the 5-cent cap ----------------------------------------

def _usage(input_tokens, output_tokens, searches=None, **extra):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, **extra)
    if searches is not None:
        usage.server_tool_use = SimpleNamespace(web_search_requests=searches, web_fetch_requests=0)
    return usage


def test_the_cost_includes_one_cent_per_web_search():
    assert _cost_usd(_usage(12_000, 1_500, searches=3)) == pytest.approx(0.069)


def test_a_usage_block_without_search_counts_still_prices_its_tokens():
    assert _cost_usd(_usage(12_000, 1_500)) == pytest.approx(0.039)
    assert _cost_usd(_usage(12_000, 1_500, server_tool_use=None)) == pytest.approx(0.039)
    assert _cost_usd(None) == 0.0


def test_cached_tokens_are_priced_at_their_own_rates():
    usage = _usage(0, 0, cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert _cost_usd(usage) == pytest.approx(2.00 * 1.25 + 2.00 * 0.10)


class _FakeClient:
    def __init__(self, usage):
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)
        self.usage = usage

    def _create(self, **kwargs):
        self.calls += 1
        reply = json.dumps({"lines": ["x"], "citations": [], "confidence": "low", "notes": "unsure"})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)], usage=self.usage)


def test_an_attempt_whose_searches_take_it_over_the_cap_gets_no_second_attempt(tmp_path):
    work = tmp_path / "work"
    _song(work, "song", concern=LYRIC_TEXT_CONCERN)
    client = _FakeClient(_usage(12_000, 1_500, searches=3))            # $0.039 of tokens + $0.03 of searches

    results = run_deep_review(work, client, songs=["song"], needs_human_dir=tmp_path / "needs_human")

    assert client.calls == 1
    assert results[0].cost_usd == pytest.approx(0.069)
    assert "cost cap" in results[0].detail
