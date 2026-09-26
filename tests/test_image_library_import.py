import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

from lyricvideo.image_library import EMBEDDING_DIM, ImageLibrary
from lyricvideo.image_library_import import collect_source_texts, find_image_files, import_images
from lyricvideo.layout import instrumental_caption
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, line_hash, save_song

BAD = b"not a png"


class _HashEmbedder:
    """A deterministic fake: each file's vector comes from its bytes; the 'not a png' file raises."""

    def __init__(self):
        self.calls = 0

    def embed_images(self, paths):
        self.calls += 1
        rows = []
        for path in paths:
            data = Path(path).read_bytes()
            if data == BAD:
                raise ValueError("cannot identify image file")
            rng = np.random.default_rng(int(hashlib.sha256(data).hexdigest()[:8], 16))
            rows.append(rng.standard_normal(EMBEDDING_DIM).astype(np.float32))
        return np.vstack(rows)


def _color_png(path: Path, color, size=(16, 16)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def _song(work: Path, slug: str, title: str, lines: list[str], chords=("C",)) -> Path:
    folder = work / slug
    folder.mkdir(parents=True, exist_ok=True)
    song = Song(
        title=title, audio_path="a.mp3",
        lines=[LyricLine(words=[Word(word=w, start_time=0.0, end_time=1.0) for w in text.split()],
                         start_time=0.0, end_time=1.0) for text in lines],
        chord_track=ChordTrack(events=[ChordEvent(0.0, 5.0, c) for c in chords]),
    )
    save_song(song, folder / "lyrics_timed.json")
    return folder


def _work(tmp_path) -> Path:
    work = tmp_path / "work"
    one = _song(work, "song-one", "Song One", ["hello there", "my friend"], chords=("C", "G"))
    _color_png(one / "images" / f"{line_hash('hello there')}.png", (255, 0, 0))
    _color_png(one / "images" / f"{line_hash('my friend')}.png", (0, 255, 0))
    _color_png(one / "images" / f"{line_hash(instrumental_caption('G'))}.png", (0, 0, 255))
    _color_png(one / "images" / "deadbeefdeadbeef.png", (9, 9, 9))                       # older lyric version: no text
    _color_png(one / "images_backup_2026" / f"{line_hash('hello there')}.png", (255, 0, 0))  # same picture as above
    _color_png(one / "easychords" / "images" / "copy.png", (1, 2, 3))                    # nested variant: a copy, skipped
    two = _song(work, "song-two", "Song Two", ["hello there"])
    _color_png(two / "images" / f"{line_hash('hello there')}.png", (250, 0, 0))            # same line, different picture
    _color_png(two / "images" / "placeholder.png", (30, 30, 40), size=(1920, 1080))       # the plain-colour fallback
    return work


def test_find_image_files_covers_images_and_backups_but_not_nested_variants(tmp_path):
    files = find_image_files(_work(tmp_path))

    names = {f.parent.name for f in files}
    assert names == {"images", "images_backup_2026"}
    assert all("easychords" not in str(f) for f in files)


def test_collect_source_texts_recovers_lyric_lines_and_instrumental_captions(tmp_path):
    texts, titles = collect_source_texts(_work(tmp_path))

    assert texts[line_hash("hello there")] == ("hello there", "Song One")
    assert texts[line_hash(instrumental_caption("G"))][0] == instrumental_caption("G")
    assert texts[line_hash(instrumental_caption(None))][0] == "[Instrumental]"
    assert titles == {"song-one": "Song One", "song-two": "Song Two"}


def test_import_adds_distinct_pictures_and_recovers_their_text(tmp_path):
    work = _work(tmp_path)
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert summary.seen == 7                    # song-one: 4 + 1 backup copy; song-two: 2 (the nested variant isn't walked)
    assert summary.skipped_placeholder == 1
    assert summary.skipped_duplicate == 1       # the backup copy of the red picture
    assert summary.added == 5 and library.count() == 5
    assert summary.text_recovered == 4          # every added image but the old-lyric-version one
    hello_there = library._db.execute("SELECT id FROM images WHERE source_text = 'hello there'").fetchall()
    assert len(hello_there) == 2                # the same line bought in two songs is two different pictures
    assert library.get(hello_there[0][0])["song_title"] in {"Song One", "Song Two"}


def test_a_second_import_adds_nothing(tmp_path):
    work = _work(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    again = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert again.added == 0 and again.skipped_duplicate == 6 and library.count() == 5


def test_dry_run_counts_but_never_embeds_or_writes(tmp_path):
    class _Boom:
        def embed_images(self, paths):
            raise AssertionError("a dry run must not embed anything")

    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _Boom(), _work(tmp_path), dry_run=True, progress=lambda *_: None)

    assert summary.to_add == 5 and summary.added == 0 and library.count() == 0


def test_limit_caps_how_many_are_added(tmp_path):
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), _work(tmp_path), limit=2, progress=lambda *_: None)

    assert summary.added == 2 and library.count() == 2


def test_a_corrupt_png_is_skipped_and_the_rest_still_import(tmp_path):
    work = _work(tmp_path)
    (work / "song-one" / "images" / "corrupt.png").write_bytes(BAD)
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert summary.failed == 1 and summary.added == 5 and library.count() == 5


def test_an_unreadable_lyrics_file_does_not_stop_the_import(tmp_path, capsys):
    work = _work(tmp_path)
    (work / "song-two" / "lyrics_timed.json").write_text("{ this is not json", encoding="utf-8")
    library = ImageLibrary(tmp_path / "lib")

    summary = import_images(library, _HashEmbedder(), work, progress=lambda *_: None)

    assert summary.added == 5                       # every picture still imports, just without song two's own text
    assert "WARNING" in capsys.readouterr().err
