"""verify_lyrics re-checks ALREADY-MADE songs against their audio (the check built on
2026-09-19 only covers songs made from then on). Song text is invented."""

import json

from lyricvideo.models import LyricLine, Song, Word, load_song, save_song
from lyricvideo.verify_lyrics import verify_all, verify_song
from lyricvideo.youtube_state import YoutubeState, save_youtube_state

SUNG = [
    "the river runs beside the old stone mill",
    "and morning fog lies heavy on the hill",
    "a lantern swings above the wooden door",
    "i wait for you like i have waited before",
    "the winter came and covered every road",
    "we traded all our dreams for heavy loads",
]
WRONG = [
    "neon signs are flashing down the avenue",
    "every stranger's face looks like a stranger's face to you",
    "dance until the sunrise turns the pavement gold",
    "nobody will ever tell the story we were told",
    "the engine hums a lullaby of chrome",
    "we drive until the highway takes us home",
]


def make_song_dir(tmp_path, slug, lines, concern="", with_stem=True):
    work_dir = tmp_path / slug
    work_dir.mkdir()
    stem = work_dir / "vocals.wav"
    if with_stem:
        stem.write_bytes(b"x" * 10)
    song = Song(
        title=slug, audio_path=str(work_dir / "song.mp3"), vocal_stem_path=str(stem),
        lines=[LyricLine(words=[Word(word=w) for w in line.split()]) for line in lines],
        lyrics_accuracy_concern=concern,
    )
    save_song(song, work_dir / "lyrics_timed.json")
    return work_dir


def hears(text):
    return lambda vocals, work_dir: text


def test_matching_lyrics_are_verified_and_the_file_is_left_alone(tmp_path):
    work_dir = make_song_dir(tmp_path, "good", SUNG)
    before = (work_dir / "lyrics_timed.json").read_text()

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True)

    assert verdict.status == "verified"
    assert (work_dir / "lyrics_timed.json").read_text() == before


def test_mismatched_lyrics_are_flagged_for_review_when_asked(tmp_path):
    work_dir = make_song_dir(tmp_path, "bad", WRONG)

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True)

    assert verdict.status == "flagged"
    assert "match what is sung" in load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern


def test_report_mode_never_writes_anything(tmp_path):
    work_dir = make_song_dir(tmp_path, "bad", WRONG)
    before = (work_dir / "lyrics_timed.json").read_text()

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=False)

    assert verdict.status == "mismatch"
    assert (work_dir / "lyrics_timed.json").read_text() == before


def test_an_existing_concern_is_never_overwritten(tmp_path):
    work_dir = make_song_dir(tmp_path, "bad", WRONG, concern="Looks like a different edition.")

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True)

    assert verdict.status == "already-flagged"
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == "Looks like a different edition."


def test_an_uploaded_song_that_fails_is_flagged_too_and_reported_as_uploaded(tmp_path):
    """Owner (2026-09-19): check the older songs too, including ones already on YouTube -- they may
    need replacing there. The flag is recorded on the song; being uploaded keeps it OFF the
    pending 'Flagged for Lyrics Review' list (that list is for songs not yet uploaded)."""
    work_dir = make_song_dir(tmp_path, "uploaded", WRONG)
    save_youtube_state(work_dir, YoutubeState(video_id="abc", uploaded_at="2026-09-01T00:00:00", title="t"))

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True)

    assert verdict.status == "flagged-uploaded"
    assert "match what is sung" in load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern


def test_an_uploaded_song_is_only_reported_in_report_mode(tmp_path):
    work_dir = make_song_dir(tmp_path, "uploaded", WRONG)
    save_youtube_state(work_dir, YoutubeState(video_id="abc", uploaded_at="2026-09-01T00:00:00", title="t"))

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=False)

    assert verdict.status == "mismatch-uploaded"
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == ""


def test_a_song_without_its_vocal_stem_is_skipped_not_crashed(tmp_path):
    work_dir = make_song_dir(tmp_path, "nostem", SUNG, with_stem=False)

    verdict = verify_song(work_dir, transcribe=hears("anything"), flag=True)

    assert verdict.status == "no-stem"


def test_verify_all_survives_one_song_failing(tmp_path):
    make_song_dir(tmp_path, "a-good", SUNG)
    make_song_dir(tmp_path, "b-broken", SUNG)
    make_song_dir(tmp_path, "c-bad", WRONG)

    def transcribe(vocals, work_dir):
        if work_dir.name == "b-broken":
            raise RuntimeError("model exploded")
        return " ".join(SUNG)

    seen = []
    verdicts = verify_all(tmp_path, transcribe=transcribe, flag=True, on_result=seen.append)

    assert {v.slug: v.status for v in verdicts} == {"a-good": "verified", "b-broken": "error", "c-bad": "flagged"}
    assert "model exploded" in next(v for v in verdicts if v.slug == "b-broken").concern
    assert len(seen) == 3  # reported as it goes, so a long run can be watched and resumed


def test_verify_all_can_skip_songs_already_done(tmp_path):
    make_song_dir(tmp_path, "one", SUNG)
    make_song_dir(tmp_path, "two", SUNG)

    verdicts = verify_all(tmp_path, transcribe=hears(" ".join(SUNG)), skip={"one"})

    assert [v.slug for v in verdicts] == ["two"]


def test_folders_without_a_finished_song_are_ignored(tmp_path):
    (tmp_path / "empty-folder").mkdir()
    make_song_dir(tmp_path, "real", SUNG)

    verdicts = verify_all(tmp_path, transcribe=hears(" ".join(SUNG)))

    assert [v.slug for v in verdicts] == ["real"]
    json.dumps([v.__dict__ for v in verdicts])  # verdicts must be JSON-serialisable for the report file


def test_a_flag_run_redoes_songs_an_earlier_run_only_reported(tmp_path, monkeypatch):
    """An earlier run (or an earlier version) may have left uploaded/mismatched songs merely reported.
    Resuming with --flag must process those again so they actually get flagged."""
    from lyricvideo import verify_lyrics

    report = tmp_path / "report.jsonl"
    report.write_text("\n".join(json.dumps({"slug": s, "status": st}) for s, st in [
        ("done-ok", "verified"), ("was-only-reported", "mismatch-uploaded"), ("earlier-crash", "error"),
        ("already-held", "flagged"),
    ]) + "\n")
    seen = {}
    monkeypatch.setattr(verify_lyrics, "verify_all", lambda *a, **k: seen.update(skip=k["skip"]) or [])

    verify_lyrics.main(["--work-root", str(tmp_path), "--report", str(report), "--flag", "--no-ai"])
    assert seen["skip"] == {"done-ok", "already-held"}

    verify_lyrics.main(["--work-root", str(tmp_path), "--report", str(report), "--no-ai"])  # report-only: reported ones are done
    assert seen["skip"] == {"done-ok", "already-held", "was-only-reported"}


# --- upload hold (owner, 2026-09-19: "stop the uploads until the videos are analyzed") ------

def add_video(work_dir):
    from lyricvideo.pipeline import slugify

    (work_dir / f"{slugify(work_dir.name)}.mp4").write_bytes(b"video")


def test_hold_pending_holds_only_unchecked_songs_that_are_waiting_to_upload(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD, hold_unchecked

    waiting = make_song_dir(tmp_path, "waiting", SUNG); add_video(waiting)
    checked_ok = make_song_dir(tmp_path, "checked-ok", SUNG); add_video(checked_ok)
    already_flagged = make_song_dir(tmp_path, "already-flagged", SUNG, concern="Wrong edition."); add_video(already_flagged)
    uploaded = make_song_dir(tmp_path, "uploaded", SUNG); add_video(uploaded)
    save_youtube_state(uploaded, YoutubeState(video_id="abc", uploaded_at="2026-09-01T00:00:00", title="t"))
    make_song_dir(tmp_path, "not-rendered", SUNG)                     # no video yet

    held = hold_unchecked(tmp_path, already_verified={"checked-ok"})

    assert held == ["waiting"]
    assert load_song(waiting / "lyrics_timed.json").lyrics_accuracy_concern == UNCHECKED_HOLD
    assert load_song(checked_ok / "lyrics_timed.json").lyrics_accuracy_concern == ""
    assert load_song(already_flagged / "lyrics_timed.json").lyrics_accuracy_concern == "Wrong edition."
    assert load_song(uploaded / "lyrics_timed.json").lyrics_accuracy_concern == ""


def test_a_held_song_that_checks_out_is_released_when_flagging(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    work_dir = make_song_dir(tmp_path, "held", SUNG, concern=UNCHECKED_HOLD)

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True)

    assert verdict.status == "verified"
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == ""   # uploads may resume


def test_a_held_song_that_fails_gets_the_real_reason_instead_of_the_hold(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    work_dir = make_song_dir(tmp_path, "held", WRONG, concern=UNCHECKED_HOLD)

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True)

    assert verdict.status == "flagged"
    assert "match what is sung" in load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern


def test_report_mode_leaves_a_hold_exactly_as_it_is(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    work_dir = make_song_dir(tmp_path, "held", SUNG, concern=UNCHECKED_HOLD)

    verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=False)

    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == UNCHECKED_HOLD


def test_a_held_song_that_cannot_be_checked_stays_held(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    work_dir = make_song_dir(tmp_path, "held", SUNG, concern=UNCHECKED_HOLD, with_stem=False)

    verdict = verify_song(work_dir, transcribe=hears("x"), flag=True)

    assert verdict.status == "no-stem"
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == UNCHECKED_HOLD


def test_held_songs_are_checked_first_because_they_are_blocking_uploads(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    make_song_dir(tmp_path, "a-free", SUNG)
    make_song_dir(tmp_path, "b-held", SUNG, concern=UNCHECKED_HOLD)
    make_song_dir(tmp_path, "c-held", SUNG, concern=UNCHECKED_HOLD)

    verdicts = verify_all(tmp_path, transcribe=hears(" ".join(SUNG)))

    assert [v.slug for v in verdicts] == ["b-held", "c-held", "a-free"]


# --- AI judge for the older songs (2026-09-19) -------------------------------------------------

def judge_confirms(lines, match, segments):
    from lyricvideo.lyric_arbiter import Arbitration

    return Arbitration(confirmed=True)


def judge_finds_wrong(lines, match, segments):
    from lyricvideo.lyric_arbiter import Arbitration, RangeVerdict

    return Arbitration(False, [RangeVerdict(1, 3, "lyrics_wrong", "the audio has a different verse here")])


WHISPER_ONLY_FLAG = "Only 40% of these lyrics match what is sung; lines 1-6 don't match the audio."


def test_a_song_the_ai_judges_a_recognizer_failure_is_accepted_and_its_hold_released(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    work_dir = make_song_dir(tmp_path, "held", WRONG, concern=UNCHECKED_HOLD)

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True, arbiter=judge_confirms, load_segments=lambda d: [])

    assert verdict.status == "verified-ai"
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == ""


def test_when_the_ai_finds_the_lyrics_wrong_its_reasons_are_recorded_with_the_flag(tmp_path):
    work_dir = make_song_dir(tmp_path, "bad", WRONG)

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True, arbiter=judge_finds_wrong, load_segments=lambda d: [])

    assert verdict.status == "flagged"
    concern = load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern
    assert "match what is sung" in concern and "different verse here" in concern


def test_recheck_clears_an_earlier_whisper_only_flag_when_the_ai_confirms_the_lyrics(tmp_path):
    work_dir = make_song_dir(tmp_path, "flagged-earlier", WRONG, concern=WHISPER_ONLY_FLAG)

    without = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True, arbiter=judge_confirms, load_segments=lambda d: [])
    assert without.status == "already-flagged"                       # no --recheck-flagged: left alone

    verdict = verify_song(
        work_dir, transcribe=hears(" ".join(SUNG)), flag=True, arbiter=judge_confirms,
        load_segments=lambda d: [], recheck=True,
    )

    assert verdict.status == "verified-ai"
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == ""


def test_recheck_never_touches_a_concern_the_audio_check_did_not_write(tmp_path):
    work_dir = make_song_dir(tmp_path, "other-concern", WRONG, concern="Looks like a different edition of the song.")

    verdict = verify_song(
        work_dir, transcribe=hears(" ".join(SUNG)), flag=True, arbiter=judge_confirms,
        load_segments=lambda d: [], recheck=True,
    )

    assert verdict.status == "already-flagged"
    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == "Looks like a different edition of the song."


def test_report_mode_with_the_judge_writes_nothing(tmp_path):
    work_dir = make_song_dir(tmp_path, "bad", WRONG)
    before = (work_dir / "lyrics_timed.json").read_text()

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=False, arbiter=judge_confirms, load_segments=lambda d: [])

    assert verdict.status == "verified-ai"
    assert (work_dir / "lyrics_timed.json").read_text() == before


def test_a_judge_that_fails_leaves_the_song_flagged_on_the_audio_check_alone(tmp_path):
    work_dir = make_song_dir(tmp_path, "bad", WRONG)

    def broken(lines, match, segments):
        raise RuntimeError("API down")

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True, arbiter=broken, load_segments=lambda d: [])

    assert verdict.status == "flagged"
    assert "match what is sung" in load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern


def test_main_runs_the_ai_judge_unless_told_not_to_and_rechecks_flagged_songs_on_request(tmp_path, monkeypatch):
    from lyricvideo import verify_lyrics

    report = tmp_path / "r.jsonl"
    report.write_text("\n".join(json.dumps({"slug": s, "status": st}) for s, st in [
        ("ok", "verified"), ("was-flagged", "flagged"), ("uploaded-flagged", "flagged-uploaded"),
    ]) + "\n")
    seen = {}
    monkeypatch.setattr(verify_lyrics, "_make_arbiter", lambda: "the-arbiter")
    monkeypatch.setattr(verify_lyrics, "verify_all", lambda *a, **k: seen.update(k) or [])

    verify_lyrics.main(["--work-root", str(tmp_path), "--report", str(report), "--flag"])
    assert seen["arbiter"] == "the-arbiter" and seen["recheck"] is False
    assert seen["skip"] == {"ok", "was-flagged", "uploaded-flagged"}

    verify_lyrics.main(["--work-root", str(tmp_path), "--report", str(report), "--flag", "--no-ai"])
    assert seen["arbiter"] is None

    verify_lyrics.main(["--work-root", str(tmp_path), "--report", str(report), "--flag", "--recheck-flagged"])
    assert seen["recheck"] is True and seen["skip"] == {"ok"}       # flagged ones are judged again
