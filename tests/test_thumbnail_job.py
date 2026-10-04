import json
from pathlib import Path

from PIL import Image

from lyricvideo import thumbnail_job as job
from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.thumbnail import THUMBNAIL_BG_FILE, THUMBNAIL_FILE


def make_song(folder: Path, title="Blackbird", artist="The Beatles"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "song_info.json").write_text(json.dumps({"title": title, "artist": artist}), encoding="utf-8")
    line = LyricLine(words=[Word(word="blackbird", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    save_song(Song(title=title, audio_path="a.m4a", lines=[line]), folder / "lyrics_timed.json")


def test_existing_thumbnail_is_kept_and_nothing_is_bought(tmp_path, monkeypatch):
    make_song(tmp_path)
    (tmp_path / THUMBNAIL_FILE).write_bytes(b"x")
    monkeypatch.setattr(job, "generate_thumbnail", lambda *a, **k: (_ for _ in ()).throw(AssertionError("bought")))
    assert job.ensure_thumbnail(tmp_path, object(), "tok") == tmp_path / THUMBNAIL_FILE


def test_makes_one_from_the_folders_title_artist_and_lyrics(tmp_path, monkeypatch):
    make_song(tmp_path)
    seen = {}

    def fake(work_dir, client, token, *, title, artist, lyrics, **kw):
        seen.update(title=title, artist=artist, lyrics=lyrics)
        (work_dir / THUMBNAIL_FILE).write_bytes(b"jpg")
        from lyricvideo.thumbnail import ThumbnailResult
        return ThumbnailResult(work_dir / THUMBNAIL_FILE, 0.01)

    monkeypatch.setattr(job, "generate_thumbnail", fake)
    assert job.ensure_thumbnail(tmp_path, object(), "tok") == tmp_path / THUMBNAIL_FILE
    assert seen == {"title": "Blackbird", "artist": "The Beatles", "lyrics": "blackbird"}


def test_missing_keys_or_title_make_nothing_and_never_raise(tmp_path):
    make_song(tmp_path)
    assert job.ensure_thumbnail(tmp_path, None, "tok") is None
    assert job.ensure_thumbnail(tmp_path, object(), "") is None
    assert job.ensure_thumbnail(tmp_path / "nowhere", object(), "tok") is None


def test_a_failure_inside_is_swallowed(tmp_path, monkeypatch):
    make_song(tmp_path)
    monkeypatch.setattr(job, "generate_thumbnail", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("replicate down")))
    assert job.ensure_thumbnail(tmp_path, object(), "tok") is None


def test_easy_folder_reuses_its_songs_picture_with_no_purchase(tmp_path, monkeypatch):
    song, easy = tmp_path / "blackbird", tmp_path / "blackbird" / "easychords"
    make_song(song)
    make_song(easy)
    Image.new("RGB", (1280, 720), (200, 120, 60)).save(song / THUMBNAIL_BG_FILE)
    monkeypatch.setattr(job, "generate_thumbnail", lambda *a, **k: (_ for _ in ()).throw(AssertionError("bought")))
    out = job.ensure_thumbnail(easy, object(), "tok")
    assert out == easy / THUMBNAIL_FILE and out.exists()


def test_set_thumbnail_sends_the_file_to_the_video(tmp_path):
    from lyricvideo.youtube import set_thumbnail
    image = tmp_path / "t.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    calls = {}

    class Thumbs:
        def set(self, **kw):
            calls.update(kw)
            return SimpleNamespaceExec()

    class SimpleNamespaceExec:
        def execute(self):
            calls["executed"] = True

    class Client:
        def thumbnails(self):
            return Thumbs()

    set_thumbnail(Client(), "vid1", image)
    assert calls["videoId"] == "vid1" and calls["executed"] and calls["media_body"].mimetype() == "image/jpeg"


def test_pipeline_hook_respects_the_setting_and_never_raises(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lyricvideo import pipeline
    called = []
    monkeypatch.setattr(job, "ensure_thumbnail", lambda *a, **k: called.append(a) or None)
    pipeline._make_thumbnail_after_render(tmp_path, SimpleNamespace(generate_thumbnails=False))
    pipeline._make_thumbnail_after_render(tmp_path, None)
    assert called == []
    monkeypatch.setattr(pipeline.anthropic, "Anthropic", lambda: object(), raising=False)
    pipeline._make_thumbnail_after_render(tmp_path, SimpleNamespace(generate_thumbnails=True))
    assert len(called) == 1
    monkeypatch.setattr(job, "ensure_thumbnail", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipeline._make_thumbnail_after_render(tmp_path, SimpleNamespace(generate_thumbnails=True))     # swallowed


def test_the_songs_own_chords_go_on_its_thumbnail_and_the_setting_turns_them_off(tmp_path, monkeypatch):
    from lyricvideo.models import ChordEvent, ChordTrack
    folder = tmp_path / "s"
    make_song(folder)
    song = job.load_song(folder / "lyrics_timed.json")
    song.chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G")], bpm=100.0)
    job.save_song(song, folder / "lyrics_timed.json") if hasattr(job, "save_song") else __import__("lyricvideo.models", fromlist=["save_song"]).save_song(song, folder / "lyrics_timed.json")
    got = []

    def fake(work_dir, client, token, *, title, artist, lyrics, chord_labels=None, **kw):
        got.append(chord_labels)
        (work_dir / THUMBNAIL_FILE).write_bytes(b"jpg")
        from lyricvideo.thumbnail import ThumbnailResult
        return ThumbnailResult(work_dir / THUMBNAIL_FILE, 0.0)

    monkeypatch.setattr(job, "generate_thumbnail", fake)
    job.ensure_thumbnail(folder, object(), "tok")
    (folder / THUMBNAIL_FILE).unlink()
    job.ensure_thumbnail(folder, object(), "tok", show_chords=False)
    assert got == [["C", "G"], None]


def test_easy_thumbnail_says_easy_chords_and_the_capo(tmp_path, monkeypatch):
    import json
    song, easy = tmp_path / "s", tmp_path / "s" / "easychords"
    make_song(song)
    make_song(easy)
    (easy / "easy_chord_capo.json").write_text(json.dumps({"capo_fret": 2, "shape_key": "D"}), encoding="utf-8")
    Image.new("RGB", (1280, 720), (200, 120, 60)).save(song / THUMBNAIL_BG_FILE)
    got = {}
    real = job.compose_from_saved_background
    monkeypatch.setattr(job, "compose_from_saved_background", lambda *a, **k: (got.update(k), real(*a, **k))[1])
    assert job.ensure_thumbnail(easy, object(), "tok") is not None
    assert got["sub_tag"] == "EASY CHORDS · CAPO 2"
    assert job._easy_badge(tmp_path) == "EASY CHORDS"          # no marker / no fret: just EASY CHORDS


def test_the_songs_own_picture_is_used_with_no_image_purchase(tmp_path, monkeypatch):
    from lyricvideo import thumbnail as th
    make_song(tmp_path)
    for i in range(4):
        (tmp_path / "images").mkdir(exist_ok=True)
        Image.new("RGB", (1280, 720), (60 + 20 * i, 90, 120)).save(tmp_path / "images" / f"i{i}.png")
    monkeypatch.setattr(job, "generate_thumbnail", lambda *a, **k: (_ for _ in ()).throw(AssertionError("bought")))
    monkeypatch.setattr(job, "pick_song_image", lambda client, t, a, l, d: (sorted(Path(d).glob("*.png"))[1], 0.004))
    out = job.ensure_thumbnail(tmp_path, object(), "tok")
    assert out == tmp_path / THUMBNAIL_FILE and out.exists() and (tmp_path / THUMBNAIL_BG_FILE).exists()


def test_no_song_pictures_falls_back_to_generating_and_the_setting_can_force_it(tmp_path, monkeypatch):
    make_song(tmp_path)
    monkeypatch.setattr(job, "pick_song_image", lambda *a: (None, 0.0))
    made = []

    def fake(work_dir, client, token, **kw):
        made.append(1)
        (work_dir / THUMBNAIL_FILE).write_bytes(b"jpg")
        from lyricvideo.thumbnail import ThumbnailResult
        return ThumbnailResult(work_dir / THUMBNAIL_FILE, 0.01)

    monkeypatch.setattr(job, "generate_thumbnail", fake)
    assert job.ensure_thumbnail(tmp_path, object(), "tok") is not None and made == [1]
    (tmp_path / THUMBNAIL_FILE).unlink()
    monkeypatch.setattr(job, "pick_song_image", lambda *a: (_ for _ in ()).throw(AssertionError("asked")))
    assert job.ensure_thumbnail(tmp_path, object(), "tok", use_song_images=False) is not None and made == [1, 1]
