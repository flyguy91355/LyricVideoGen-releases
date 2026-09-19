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


def test_an_uploaded_song_is_reported_but_never_flagged(tmp_path):
    """An uploaded video can't be recalled and flagged songs only matter while pending."""
    work_dir = make_song_dir(tmp_path, "uploaded", WRONG)
    save_youtube_state(work_dir, YoutubeState(video_id="abc", uploaded_at="2026-09-01T00:00:00", title="t"))

    verdict = verify_song(work_dir, transcribe=hears(" ".join(SUNG)), flag=True)

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
