"""Issue #7, wave-2 integration: the cross-file requests the fix agents could not make themselves. Song words are invented."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from lyricvideo import gui, pipeline, youtube, youtube_schedule
from lyricvideo.gui import LyricVideoGUI
from lyricvideo.key_decision import KeyDecision, load_decision, save_decision, save_owner_key
from lyricvideo.layout import line_hash
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, load_song, save_song
from lyricvideo.settings import Settings
from lyricvideo.youtube_state import STATE_FILENAME

WORDS = ["copper", "kettles", "hum", "a", "morning", "tune"]
TITLE = "Copper Kettles"


def _line(words, start=16.0):
    return LyricLine(words=[Word(w, start + 0.6 * i, start + 0.6 * i + 0.5) for i, w in enumerate(words)],
                     start_time=start, end_time=start + 0.6 * len(words))


def _track(labels, key):
    return ChordTrack(events=[ChordEvent(i * 4.0, (i + 1) * 4.0, label) for i, label in enumerate(labels)], key=key, bpm=90.0)


def _make_song(work_root: Path, name="copper-kettles", *, key="Eb major", labels=("Eb", "Ab", "Bb", "Eb"),
               decision="confirmed", video=True) -> Path:
    song_dir = work_root / name
    song_dir.mkdir(parents=True)
    save_song(Song(title=TITLE, audio_path=str(song_dir / "song.mp3"), lines=[_line(WORDS)], chord_track=_track(labels, key)),
              song_dir / "lyrics_timed.json")
    (song_dir / "song_info.json").write_text(json.dumps({"title": TITLE, "artist": "Nobody", "duration": 32.0}), encoding="utf-8")
    (song_dir / "song.mp3").write_bytes(b"fake audio")
    if video:
        (song_dir / "copper-kettles.mp4").write_bytes(b"the song's video")
    if decision == "confirmed":
        save_decision(song_dir, KeyDecision(status="confirmed", key=key, source="agreed", chord_key=key))
    elif decision == "review":
        save_decision(song_dir, KeyDecision(status="review", chord_key=key, published_key="G major"))
    return song_dir


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


# --- youtube_schedule.schedule_upload (gui-behavior request 1, easy-pipeline request 3) --------------------------------

def test_a_retry_never_resends_a_song_whose_upload_record_is_empty(tmp_path):
    song_dir = _make_song(tmp_path)
    (song_dir / STATE_FILENAME).write_text("", encoding="utf-8")          # cut off mid-write: still means uploaded

    with pytest.raises(youtube_schedule.AlreadyUploaded):
        youtube_schedule.schedule_upload(None, None, song_dir, Settings(), only_if_not_uploaded=True)


def _upload_ready(monkeypatch, sent):
    monkeypatch.setattr(youtube_schedule, "check_video_complete", lambda path, label="": (30.0, 30.0))
    monkeypatch.setattr(youtube_schedule, "generate_video_metadata", lambda *a: ("t", "a description", ["tag"]))

    def fake_upload(client, path, title, *a, **k):
        sent.append(title)
        return "vid-new"
    monkeypatch.setattr(youtube_schedule, "upload_video", fake_upload)


def test_an_upload_that_cannot_be_recorded_names_the_new_video_id(tmp_path, monkeypatch):
    song_dir = _make_song(tmp_path)
    sent = []
    _upload_ready(monkeypatch, sent)

    def no_room(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(youtube_schedule, "save_youtube_state", no_room)

    with pytest.raises(RuntimeError, match="vid-new"):
        youtube_schedule.schedule_upload(None, None, song_dir, Settings(youtube_privacy="unlisted"))
    assert sent                                         # it WAS sent: the error must say so, with its id


def test_an_easy_version_made_before_its_songs_lyrics_changed_is_refused_before_any_paid_call(tmp_path, monkeypatch, renders):
    song_dir = _make_song(tmp_path / "work")
    pipeline.build_capo_variant(song_dir, settings=Settings())
    song = load_song(song_dir / "lyrics_timed.json")
    song.lines = [_line(["silver", "spoons", "ring", "a", "supper", "bell"], 15.0)]      # a Redo: same chords and key
    save_song(song, song_dir / "lyrics_timed.json")
    sent = []
    _upload_ready(monkeypatch, sent)
    monkeypatch.setattr(youtube_schedule, "generate_video_metadata", lambda *a: pytest.fail("no paid call"))

    with pytest.raises(youtube_schedule.EasyChordVersionStale, match="lyrics"):
        youtube_schedule.schedule_upload(None, None, song_dir / "easychords", Settings(youtube_privacy="unlisted"))
    assert sent == []


# --- the lists (easy-pipeline request 2) -------------------------------------------------------------------------------

def test_an_out_of_date_easy_version_is_flagged_for_rebuild_not_pending(tmp_path, renders):
    work = tmp_path / "work"
    song_dir = _make_song(work)
    pipeline.build_capo_variant(song_dir, settings=Settings())
    (song_dir / STATE_FILENAME).write_text(json.dumps({"video_id": "v1", "uploaded_at": "x", "title": "t"}), encoding="utf-8")
    assert "copper-kettles/easychords" in pipeline.list_pending_uploads(work)

    song = load_song(song_dir / "lyrics_timed.json")
    song.lines = [_line(["silver", "spoons", "ring", "a", "supper", "bell"], 15.0)]
    save_song(song, song_dir / "lyrics_timed.json")

    assert "copper-kettles/easychords" not in pipeline.list_pending_uploads(work)
    assert "copper-kettles/easychords" in pipeline.list_flagged_songs(work)
    assert pipeline.review_concern(song_dir / "easychords").startswith(pipeline.EASY_STALE_PREFIX)


# --- prefer_flats reaches the key check and the EASY version (key-core requests 1-3) ------------------------------------

def test_a_render_resume_spells_the_owners_key_per_the_flats_setting(tmp_path, renders):
    song_dir = _make_song(tmp_path, key="A# major", labels=("A#", "D#", "F", "A#"), decision="review", video=False)
    save_owner_key(song_dir, "Bb major")

    pipeline.run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render", settings=Settings(prefer_flats=False))
    assert [e.label for e in load_song(song_dir / "lyrics_timed.json").chord_track.events] == ["A#", "D#", "F", "A#"]
    assert load_decision(song_dir).confirmed

    pipeline.run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render", settings=Settings(prefer_flats=True))
    assert [e.label for e in load_song(song_dir / "lyrics_timed.json").chord_track.events] == ["Bb", "Eb", "F", "Bb"]


def test_the_easy_version_spells_borrowed_chords_per_the_flats_setting(tmp_path, renders):
    sharp_dir = _make_song(tmp_path / "a", labels=("Eb", "E", "Bb", "Eb"))
    flat_dir = _make_song(tmp_path / "b", labels=("Eb", "E", "Bb", "Eb"))

    pipeline.build_capo_variant(sharp_dir, settings=Settings(prefer_flats=False))
    pipeline.build_capo_variant(flat_dir, settings=Settings(prefer_flats=True))

    assert load_song(sharp_dir / "easychords" / "lyrics_timed.json").chord_track.events[1].label == "D#"
    assert load_song(flat_dir / "easychords" / "lyrics_timed.json").chord_track.events[1].label == "Eb"


# --- a respelled chord keeps its picture (easy-pipeline request 1) -----------------------------------------------------

def test_a_render_only_resume_gives_a_respelled_chord_its_own_picture(tmp_path, renders):
    song_dir = _make_song(tmp_path, key="Bb major", labels=("Bb",), video=False)
    song = load_song(song_dir / "lyrics_timed.json")
    song.lines = [_line(WORDS, 30.0)]                                      # 30 s of instrumental before the singing
    song.chord_track = ChordTrack(events=[ChordEvent(0.0, 30.0, "Bb")], key="Bb major", bpm=90.0)
    save_song(song, song_dir / "lyrics_timed.json")
    images = song_dir / "images"
    images.mkdir()
    Image.new("RGB", (8, 8), (10, 200, 30)).save(images / f"{line_hash('[Instrumental — chord: A#]')}.png")

    pipeline.run_pipeline(song_dir / "song.mp3", song_dir, start_stage="render")

    assert (images / f"{line_hash('[Instrumental — chord: Bb]')}.png").is_file()


# --- identify saves the credited artists (lyrics-fix request 1) ---------------------------------------------------------

def test_identify_saves_the_individually_credited_artists(tmp_path, monkeypatch):
    info = SimpleNamespace(title="Copper Kettles", artist="Ann & Bo", duration=30.0, alt_titles=[], artists=["Ann", "Bo"])
    monkeypatch.setattr(pipeline, "extract_metadata", lambda path: info)
    (tmp_path / "song.mp3").write_bytes(b"x")

    pipeline.run_pipeline(tmp_path / "song.mp3", tmp_path / "w", end_stage="identify")

    saved = json.loads((tmp_path / "w" / "song_info.json").read_text(encoding="utf-8"))
    assert saved["artists"] == ["Ann", "Bo"] and saved["artist"] == "Ann & Bo"


# --- youtube.Comment carries the commenter's channel (gui-behavior request 2) ------------------------------------------

def test_a_comment_carries_its_authors_channel_id():
    class Threads:
        def list(self, **kwargs):
            return SimpleNamespace(execute=lambda: {"items": [
                {"snippet": {"topLevelComment": {"id": "c1", "snippet": {
                    "authorDisplayName": "Ann", "textDisplay": "hi", "publishedAt": "p", "authorChannelId": {"value": "UC1"}}}}},
                {"snippet": {"topLevelComment": {"id": "c2", "snippet": {"textDisplay": "no channel"}}}},
            ]})
    client = SimpleNamespace(commentThreads=lambda: Threads())

    comments = youtube.list_new_comments(client, "vid", set())

    assert [c.author_channel_id for c in comments] == ["UC1", ""]


# --- the render's nearest-picture rule (easy-pipeline request 4) -------------------------------------------------------

def test_a_missing_picture_shows_the_nearest_one_in_the_songs_order(tmp_path):
    from lyricvideo.assemble import _BackgroundCache

    colors = {"a": (255, 0, 0), "c": (0, 0, 255), "d": (0, 255, 0)}
    for key, color in colors.items():
        Image.new("RGB", (4, 4), color).save(tmp_path / f"{key}.png")
    cache = _BackgroundCache(tmp_path, (4, 4), (1, 1, 1), order=["d", "c", "b", "a"])

    assert cache.get("b").getpixel((0, 0)) == colors["c"]         # its neighbour, not sorted()[0] ("a")


# --- set-aside videos are not set aside twice (upload-guard request 3) -------------------------------------------------

def test_a_truncated_video_set_aside_by_the_script_is_left_alone(tmp_path):
    from lyricvideo.key_rollout import _set_aside as rollout_set_aside

    for set_aside in (rollout_set_aside, pipeline._set_aside_videos):
        folder = tmp_path / f"{set_aside.__module__}.{set_aside.__name__}"
        folder.mkdir()
        (folder / "song.truncated.mp4").write_bytes(b"x")
        (folder / "song.mp4").write_bytes(b"x")
        set_aside(folder)
        assert sorted(p.name for p in folder.iterdir()) == ["song.previous.mp4", "song.truncated.mp4"]


# --- Set Key for an uploaded song whose EASY version waits (pipeline-stages request 2) --------------------------------

def _uploaded_with_waiting_easy(work: Path) -> Path:
    song_dir = _make_song(work, decision="review")
    (song_dir / STATE_FILENAME).write_text(json.dumps({"video_id": "v1", "uploaded_at": "x", "title": "t"}), encoding="utf-8")
    easy = song_dir / "easychords"
    easy.mkdir()
    save_song(Song(title=TITLE + " EasyChords", audio_path="", lines=[_line(WORDS)], chord_track=_track(("D",), "D major")),
              easy / "lyrics_timed.json")
    return song_dir


def test_an_uploaded_song_whose_easy_version_waits_offers_set_key(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    _uploaded_with_waiting_easy(tmp_path / "work")

    row = LyricVideoGUI._flagged_row_data(SimpleNamespace(), "copper-kettles")

    assert row["key_waiting"] and row["easy_waits"] and "EASY waits" in row["reason"]
    assert "copper-kettles" in pipeline.list_flagged_songs(tmp_path / "work", include_uploaded=True)


def test_set_key_on_an_uploaded_song_confirms_the_key_and_offers_the_easy_rebuild_without_remaking_its_video(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "PROJECT_ROOT", tmp_path)
    song_dir = _uploaded_with_waiting_easy(tmp_path / "work")
    monkeypatch.setattr(gui.simpledialog, "askstring", lambda *a, **k: "Eb major")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: pytest.fail(str(a)))
    rebuilt, rendered = [], []
    stub = SimpleNamespace(
        _running=False, settings=Settings(), root=None,
        _on_rebuild_easy_flagged=lambda slug, confirm=True: rebuilt.append((slug, confirm)),
        _on_render_anyway_flagged=lambda *a, **k: rendered.append(a),
    )
    stub._settle_key_of_uploaded_song = lambda *a: LyricVideoGUI._settle_key_of_uploaded_song(stub, *a)

    LyricVideoGUI._on_set_key_flagged(stub, "copper-kettles")

    assert load_decision(song_dir).confirmed and load_decision(song_dir).key == "Eb major"
    assert rebuilt == [("copper-kettles/easychords", False)] and rendered == []
    assert not pipeline.easy_version_waits_on_key(song_dir)


# --- the Settings preview never raises over a missing font (pipeline-stages request 5) --------------------------------

def test_the_settings_preview_falls_back_to_the_default_font_when_the_chosen_one_is_gone(tmp_path):
    from lyricvideo.settings_preview import render_preview_frame

    frame = render_preview_frame(Settings(font_path=str(tmp_path / "gone.ttf")))

    assert frame.size[0] > 0
