"""Issue #7 review, "check easy chords for proper capo and chords": EASY CHORD (capo) versions were rendered with the factory
render settings instead of the owner's (F067/F072-F076); stayed uploadable with the OLD lyrics, timing, chords or key after
their song was redone (F061/F066); and the backfill list offered songs the build then refused, hid songs whose EASY render
never finished, and counted a song that was not built as built (F035/F060). Song words are invented."""

import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo import gui, pipeline
from lyricvideo.gui import LyricVideoGUI
from lyricvideo.key_decision import KeyDecision, save_decision
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, load_song, save_song
from lyricvideo.owner_verified import mark_verified, verification
from lyricvideo.pipeline import (
    EASY_STALE_PREFIX, HELD_MARKER, build_capo_variant, easy_chord_backfill_listing, easy_chord_build_problem,
    easy_variant_problem, list_easy_chord_backfill_candidates, list_flagged_songs, list_pending_uploads,
    list_rendered_songs, run_pipeline,
)
from lyricvideo.settings import Settings

WORDS = ["copper", "kettles", "hum", "a", "morning", "tune"]
TITLE = "Copper Kettles"


def _track(labels, key):
    return ChordTrack(events=[ChordEvent(i * 4.0, (i + 1) * 4.0, label) for i, label in enumerate(labels)], key=key, bpm=90.0)


def _line(words, start=16.0):
    return LyricLine(words=[Word(w, start + 0.6 * i, start + 0.6 * i + 0.5) for i, w in enumerate(words)],
                     start_time=start, end_time=start + 0.6 * len(words))


def _make_song(work_root: Path, name="copper-kettles", *, key="Eb major", decision="confirmed", audio=True, video=True,
               labels=("Eb", "Ab", "Bb", "Cm", "Eb", "Ab", "Bb", "Eb")) -> Path:
    song_dir = work_root / name
    song_dir.mkdir(parents=True)
    save_song(Song(title=TITLE, audio_path=str(song_dir / "song.mp3"), lines=[_line(WORDS)], chord_track=_track(labels, key)),
              song_dir / "lyrics_timed.json")
    (song_dir / "song_info.json").write_text(json.dumps({"title": TITLE, "artist": "Nobody", "duration": 32.0}), encoding="utf-8")
    if audio:
        (song_dir / "song.mp3").write_bytes(b"fake audio")
    if video:
        (song_dir / "copper-kettles.mp4").write_bytes(b"the song's video")
    if decision == "confirmed":
        save_decision(song_dir, KeyDecision(status="confirmed", key=key, source="agreed", chord_key=key))
    elif decision == "review":
        save_decision(song_dir, KeyDecision(status="review", chord_key=key, published_key="G major"))
    (song_dir / "images").mkdir()
    (song_dir / "images" / "picture.png").write_bytes(b"a picture")
    return song_dir


def _redo(song_dir: Path, words=("silver", "spoons", "ring", "a", "supper", "bell"), key=None, labels=None) -> None:
    """What a Redo leaves in the song's folder: new lyric timing (and maybe a new key and chords)."""
    song = load_song(song_dir / "lyrics_timed.json")
    song.lines = [_line(list(words), 15.0)]
    if key is not None:
        song.chord_track = _track(labels, key)
        save_decision(song_dir, KeyDecision(status="confirmed", key=key, source="agreed", chord_key=key))
    save_song(song, song_dir / "lyrics_timed.json")


@pytest.fixture
def renders(monkeypatch, tmp_path):
    calls = []

    def fake_assemble(lines, chord_track, image_dir, audio_path, out_path, font_path, *args, **kwargs):
        calls.append({"out_path": Path(out_path), "chord_track": chord_track, **kwargs})
        Path(out_path).write_bytes(b"rendered video")

    monkeypatch.setattr(pipeline, "assemble_video", fake_assemble)
    monkeypatch.setattr(pipeline, "default_font", lambda: "font.ttf")
    monkeypatch.setattr("lyricvideo.cleared_log.LOG_FILE", tmp_path / "cleared.json")
    return calls


OWNER = dict(resolution="720p (1280x720)", fps=30, encoder="libx265", crf=18, countdown_beats=2,
             support_overlay_text="Support the channel: link below", text_color="#ffeecc", chord_legend_size=80,
             image_min_hold_seconds=3.5, image_transition_seconds=0.5)


def _expected_render_kwargs(settings: Settings) -> dict:
    return {**settings.render_kwargs(), "fps": settings.fps, "encoder": settings.encoder, "crf": settings.crf,
            "countdown_beats": settings.countdown_beats}


# --- the owner's Settings reach the EASY render (F067/F072-F076) -------------------------------------------------------

def test_the_easy_version_is_rendered_with_the_owners_settings(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")
    settings = Settings(**OWNER)

    build_capo_variant(song_dir, settings=settings)

    easy = renders[-1]
    for name, value in _expected_render_kwargs(settings).items():
        if name != "font_path":
            assert easy[name] == value, name
    assert easy["capo"] == 1 and easy["key_label"] == "Eb major"


def test_without_settings_the_easy_version_uses_the_saved_settings(tmp_path, renders):
    Settings(**OWNER).save()

    build_capo_variant(_make_song(tmp_path / "work"))

    assert renders[-1]["support_overlay_text"] == OWNER["support_overlay_text"]
    assert renders[-1]["fps"] == 30 and renders[-1]["frame_size"] == (1280, 720)


def test_a_generate_with_easy_versions_on_renders_both_videos_with_the_same_settings(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")
    settings = Settings(**OWNER, generate_easy_chord_versions=True)

    run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render", settings=settings)

    assert [call["out_path"].name for call in renders] == ["copper-kettles.mp4", "copper-kettles-easychords.mp4"]
    song_call, easy_call = renders
    for name in ("frame_size", "fps", "encoder", "crf", "countdown_beats", "support_overlay_text", "text_color",
                 "chord_legend_scale", "min_hold_seconds", "image_transition_seconds"):
        assert easy_call[name] == song_call[name], name
    assert easy_call["capo"] == 1 and song_call["capo"] is None


# --- a stale EASY version is never left uploadable (F061/F066) ---------------------------------------------------------

def test_redoing_the_song_without_easy_versions_sets_its_old_easy_version_aside_and_flags_it(tmp_path, renders):
    work = tmp_path / "work"
    song_dir = _make_song(work)
    build_capo_variant(song_dir, settings=Settings())
    assert easy_variant_problem(song_dir / "easychords") == ""

    _redo(song_dir)
    run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render", settings=Settings())

    easy_dir = song_dir / "easychords"
    assert not (easy_dir / "copper-kettles-easychords.mp4").exists()
    assert (easy_dir / "copper-kettles-easychords.previous.mp4").exists()          # set aside, never deleted
    assert (easy_dir / HELD_MARKER).exists()
    assert load_song(easy_dir / "lyrics_timed.json").lyrics_accuracy_concern.startswith(EASY_STALE_PREFIX)
    assert "copper-kettles/easychords" not in list_pending_uploads(work)
    assert "copper-kettles/easychords" not in list_rendered_songs(work)
    assert "copper-kettles/easychords" in list_flagged_songs(work)


def test_redoing_the_song_with_easy_versions_on_remakes_its_easy_version_from_the_new_lyrics(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")
    build_capo_variant(song_dir, settings=Settings())

    _redo(song_dir)
    run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render", settings=Settings(generate_easy_chord_versions=True))

    easy_dir = song_dir / "easychords"
    assert load_song(easy_dir / "lyrics_timed.json").lines[0].words[0].word == "silver"
    assert (easy_dir / "copper-kettles-easychords.mp4").read_bytes() == b"rendered video"
    assert not (easy_dir / HELD_MARKER).exists()
    assert easy_variant_problem(easy_dir) == ""


def test_a_song_whose_key_is_now_easy_has_its_easy_version_set_aside_without_a_flag(tmp_path, renders):
    work = tmp_path / "work"
    song_dir = _make_song(work)
    build_capo_variant(song_dir, settings=Settings())

    _redo(song_dir, words=WORDS, key="C major", labels=("C", "F", "G", "Am", "C", "F", "G", "C"))
    run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render", settings=Settings(generate_easy_chord_versions=True))

    easy_dir = song_dir / "easychords"
    assert not (easy_dir / "copper-kettles-easychords.mp4").exists()
    assert not (easy_dir / HELD_MARKER).exists()
    assert "copper-kettles/easychords" not in list_flagged_songs(work) + list_pending_uploads(work) + list_rendered_songs(work)


def test_an_unchanged_song_leaves_its_easy_version_alone(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")
    build_capo_variant(song_dir, settings=Settings())

    run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render", settings=Settings())

    assert (song_dir / "easychords" / "copper-kettles-easychords.mp4").exists()
    assert not (song_dir / "easychords" / HELD_MARKER).exists()


def test_a_rebuild_whose_render_fails_never_leaves_the_old_video_as_the_current_one(tmp_path, renders, monkeypatch):
    work = tmp_path / "work"
    song_dir = _make_song(work)
    build_capo_variant(song_dir, settings=Settings())
    _redo(song_dir)

    def killed(*args, **kwargs):
        raise RuntimeError("render killed")

    monkeypatch.setattr(pipeline, "assemble_video", killed)
    with pytest.raises(RuntimeError, match="render killed"):
        build_capo_variant(song_dir, settings=Settings())

    easy_dir = song_dir / "easychords"
    assert not (easy_dir / "copper-kettles-easychords.mp4").exists()                 # the old lyrics can never upload
    assert (easy_dir / HELD_MARKER).exists()
    assert "copper-kettles/easychords" in list_flagged_songs(work)


def test_a_failed_re_render_of_an_unchanged_easy_version_keeps_its_video(tmp_path, renders, monkeypatch):
    song_dir = _make_song(tmp_path / "work")
    build_capo_variant(song_dir, settings=Settings())
    monkeypatch.setattr(pipeline, "assemble_video", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no disk")))

    with pytest.raises(RuntimeError):
        build_capo_variant(song_dir, settings=Settings())

    assert (song_dir / "easychords" / "copper-kettles-easychords.mp4").exists()
    assert not (song_dir / "easychords" / HELD_MARKER).exists()


def test_easy_variant_problem_names_what_changed(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")
    build_capo_variant(song_dir, settings=Settings())
    easy_dir = song_dir / "easychords"
    assert easy_variant_problem(easy_dir) == ""
    assert easy_variant_problem(song_dir) == ""                                        # not an EASY version

    _redo(song_dir)
    assert "lyrics or their timing" in easy_variant_problem(easy_dir)

    build_capo_variant(song_dir, settings=Settings())
    song = load_song(song_dir / "lyrics_timed.json")
    song.chord_track = _track(["Eb", "Fm", "Bb", "Cm", "Eb", "Ab", "Bb", "Eb"], "Eb major")    # same key, other chords
    save_song(song, song_dir / "lyrics_timed.json")
    assert "chords are no longer" in easy_variant_problem(easy_dir)

    build_capo_variant(song_dir, settings=Settings())
    save_decision(song_dir, KeyDecision(status="confirmed", key="Ab major", source="owner", chord_key="Eb major"))
    assert "Ab major" in easy_variant_problem(easy_dir)


# --- the owner's verification carries over (wave-1 request) ------------------------------------------------------------

def test_the_owners_approval_of_the_song_covers_its_easy_version(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")
    mark_verified(song_dir)

    build_capo_variant(song_dir, settings=Settings())

    record = verification(song_dir / "easychords")
    assert record is not None and record["carried_from"] == "copper-kettles"


def test_a_render_of_a_song_with_no_key_still_checks_its_easy_version(tmp_path, renders):
    """Review of the F061 fix: a song whose chord track has no key (None) made the post-render hook crash on
    is_easy_key(None) before it ever looked at the EASY version, leaving a stale one uploadable."""
    work = tmp_path / "work"
    song_dir = _make_song(work)
    build_capo_variant(song_dir, settings=Settings())
    song = load_song(song_dir / "lyrics_timed.json")
    song.lines = [_line(["silver", "spoons", "ring"], 15.0)]
    song.chord_track = ChordTrack(events=[], key=None, bpm=90.0)
    save_song(song, song_dir / "lyrics_timed.json")

    pipeline._update_easy_version_after_render(song_dir, song, Settings(generate_easy_chord_versions=True), None)

    easy_dir = song_dir / "easychords"
    assert not (easy_dir / "copper-kettles-easychords.mp4").exists()
    assert (easy_dir / HELD_MARKER).exists()


def test_an_unverified_song_gives_its_easy_version_no_approval(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")

    build_capo_variant(song_dir, settings=Settings())

    assert verification(song_dir / "easychords") is None


# --- the backfill list offers exactly what will build (F035/F060/F061) -------------------------------------------------

def _passing(song_dir: Path) -> Path:
    mark_verified(song_dir)                      # "if i decide its a good video its a good video": passes for upload
    return song_dir


def test_the_backfill_list_offers_only_songs_the_build_will_make(tmp_path, renders):
    work = tmp_path / "work"
    _passing(_make_song(work, "ready"))
    _passing(_make_song(work, "unchecked-key", decision=None))
    _passing(_make_song(work, "key-in-review", decision="review"))
    _passing(_make_song(work, "audio-gone", audio=False))
    _passing(_make_song(work, "easy-key", key="G major", labels=("G", "C", "D", "Em", "G", "C", "D", "G")))

    listing = easy_chord_backfill_listing(work)

    assert listing.songs == ["ready"] == list_easy_chord_backfill_candidates(work)
    assert listing.waiting_for_key == 2
    assert "not been checked" in easy_chord_build_problem(work / "unchecked-key")
    assert "confirm" in easy_chord_build_problem(work / "key-in-review")
    assert "audio" in easy_chord_build_problem(work / "audio-gone")
    assert "already easy" in easy_chord_build_problem(work / "easy-key")


def test_the_backfill_list_looks_at_the_easy_video_not_the_folder(tmp_path, renders):
    work = tmp_path / "work"
    current = _passing(_make_song(work, "current"))
    build_capo_variant(current, settings=Settings())
    no_video = _passing(_make_song(work, "no-video"))
    (no_video / "easychords").mkdir()                                  # a killed render leaves a folder, no video
    stale = _passing(_make_song(work, "stale"))
    build_capo_variant(stale, settings=Settings())
    save_decision(stale, KeyDecision(status="confirmed", key="Ab major", source="owner", chord_key="Eb major"))

    listing = easy_chord_backfill_listing(work)

    assert listing.songs == ["no-video", "stale"]
    assert "no video" in listing.labels["no-video"] and "out of date" in listing.labels["stale"]


def test_a_song_left_out_for_its_key_is_not_built_either(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work", decision=None)

    assert build_capo_variant(song_dir, settings=Settings()) is None
    assert not (song_dir / "easychords").exists() and renders == []


# --- the GUI worker (F035, wave-1 requests) ----------------------------------------------------------------------------

def test_the_backfill_worker_passes_the_owners_settings_reports_why_a_song_was_not_built_and_frees_memory(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _make_song(work, "unchecked-key", decision=None)
    _make_song(work, "ready")
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    seen, freed = [], []

    def fake_build(work_dir, settings=None, **kwargs):
        seen.append((Path(work_dir).name, settings))
        return None if Path(work_dir).name == "unchecked-key" else Path(work_dir) / "easychords" / "x.mp4"

    monkeypatch.setattr(gui, "build_capo_variant", fake_build)
    monkeypatch.setattr(gui, "release_memory", lambda: freed.append(1))
    owner = Settings(**OWNER)
    stub = SimpleNamespace(_queue=queue.Queue(), settings=owner)

    LyricVideoGUI._run_easy_chord_backfill_worker(stub, ["unchecked-key", "ready"])

    messages = []
    while not stub._queue.empty():
        messages.append(stub._queue.get_nowait())
    results = dict(messages)["easy_chord_backfill_done"]
    assert results["succeeded"] == ["ready"]
    assert results["failed"][0][0] == "unchecked-key"
    assert results["failed"][0][1].startswith("not built -- ") and "not been checked" in results["failed"][0][1]
    assert [name for name, _ in seen] == ["unchecked-key", "ready"]
    assert all(settings == owner and settings is not owner for _, settings in seen)    # a snapshot of the owner's
    assert freed == [1, 1]


def test_the_backfill_summary_lists_songs_not_built_apart_from_failures(monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message: shown.append(message))
    button = SimpleNamespace(configure=lambda **kw: None)
    stub = SimpleNamespace(
        _running=True, generate_button=button, redo_button=button, batch_button=button,
        generate_easy_chord_backfill_button=button, status_var=SimpleNamespace(set=lambda v: None),
        progress_bar=SimpleNamespace(set=lambda v: None), _invalidate_easy_chord_backfill_list=lambda: None,
        _refresh_retry_upload_options=lambda: None,
    )

    LyricVideoGUI._on_easy_chord_backfill_done(stub, {"succeeded": ["a"], "failed": [
        ("b", "not built -- its key has not been checked yet"), ("c", "RuntimeError: boom")]})

    assert "Built 1 EASY CHORD version(s)." in shown[0]
    assert "1 not built:\n  b: not built -- its key has not been checked yet" in shown[0]
    assert "1 failed:\n  c: RuntimeError: boom" in shown[0]


def test_the_backfill_list_scan_labels_rows_and_counts_songs_waiting_for_a_key(tmp_path, renders, monkeypatch):
    work = tmp_path / "work"
    _passing(_make_song(work, "ready"))
    _passing(_make_song(work, "unchecked-key", decision=None))
    stale = _passing(_make_song(work, "stale"))
    build_capo_variant(stale, settings=Settings())
    _redo(stale)
    _passing(stale)
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(gui, "load_dismissed", lambda list_name: set())

    data = LyricVideoGUI._scan_easy_chord_backfill_list(SimpleNamespace())

    assert data["songs"] == ["ready", "stale"]
    assert "out of date" in data["labels"]["stale"]
    assert data["note"] == "1 more wait for a settled key"
