import json
from pathlib import Path

import numpy as np
from PIL import Image

from lyricvideo.image_library import EMBEDDING_DIM, ImageLibrary, content_id
from lyricvideo.layout import instrumental_caption
from lyricvideo.library_preview import (
    build_rows, cached_prompt_maker, reuse_counts, song_keys, write_report,
)
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, line_hash, load_song, save_song


def _vec(*head):
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[: len(head)] = head
    return v


class _TextEmbedder:
    def __init__(self, mapping):
        self.mapping = mapping

    def embed_text(self, texts):
        return np.vstack([self.mapping[t] for t in texts])


def _png(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), color).save(path)
    return path


def _song_dir(tmp_path) -> Path:
    folder = tmp_path / "work" / "my-song"
    folder.mkdir(parents=True)
    lines = ["red door", "green field", "red door"]      # a repeated line is one picture, so one row
    song = Song(
        title="My <Song>", audio_path="a.mp3",
        lines=[LyricLine(words=[Word(word=w, start_time=float(i), end_time=i + 0.5) for w in text.split()],
                         start_time=float(i), end_time=i + 0.5) for i, text in enumerate(lines)],
        chord_track=ChordTrack(events=[ChordEvent(0.0, 30.0, "Am")]),
    )
    save_song(song, folder / "lyrics_timed.json")
    return folder


def test_song_keys_are_the_distinct_lines_then_the_instrumental_captions(tmp_path):
    song = load_song(_song_dir(tmp_path) / "lyrics_timed.json")

    keys = song_keys(song)

    assert keys[:2] == ["red door", "green field"]
    assert instrumental_caption("Am") in keys[2:]


def test_cached_prompt_maker_asks_once_per_text_even_across_runs(tmp_path):
    cache = tmp_path / "prompts.json"
    made = []
    first = cached_prompt_maker(cache, lambda text: made.append(text) or f"prompt for {text}")

    assert first("a") == "prompt for a" and first("a") == "prompt for a" and first.calls == 1

    second = cached_prompt_maker(cache, lambda text: made.append(text) or "SHOULD NOT BE CALLED")
    assert second("a") == "prompt for a" and second.calls == 0 and made == ["a"]
    assert json.loads(cache.read_text(encoding="utf-8")) == {"a": "prompt for a"}


def test_build_rows_excludes_the_songs_own_pictures_from_the_offers(tmp_path):
    song_dir = _song_dir(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    own = _png(song_dir / "images" / f"{line_hash('red door')}.png", (255, 0, 0))
    library.add(own, _vec(1, 0))                                          # the song's own picture, perfect match
    other = library.add(_png(tmp_path / "other.png", (0, 255, 0)), _vec(0.9, 0.3))
    embedder = _TextEmbedder({"p-red": _vec(1, 0), "p-green": _vec(0, 1), "p-inst": _vec(1, 0)})
    prompts = {"red door": "p-red", "green field": "p-green", instrumental_caption("Am"): "p-inst"}

    rows, own_ids = build_rows(song_dir, library, embedder, lambda text: prompts[text])

    assert own_ids == {content_id(own)}
    assert [r.text for r in rows][:2] == ["red door", "green field"]
    assert rows[0].own_image == own and rows[1].own_image is None
    assert rows[0].match.image_id == other          # never its own picture, even though that scored 1.0


def test_reuse_counts_apply_the_once_per_song_rule_at_each_threshold(tmp_path):
    song_dir = _song_dir(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    embedder = _TextEmbedder({"p1": _vec(1, 0), "p2": _vec(1, 0), "p3": _vec(0, 1)})
    prompts = {"red door": "p1", "green field": "p2", instrumental_caption("Am"): "p3"}
    rows, own_ids = build_rows(song_dir, library, embedder, lambda text: prompts[text])

    counts = reuse_counts(rows, library, own_ids, [0.5, 0.99])

    assert counts == [(0.5, 1), (0.99, 1)]      # p1 and p2 both want the single picture: only one line gets it


def test_write_report_makes_a_self_contained_page_with_escaped_text_and_thumbnails(tmp_path):
    song_dir = _song_dir(tmp_path)
    library = ImageLibrary(tmp_path / "lib")
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    _png(song_dir / "images" / f"{line_hash('red door')}.png", (0, 0, 255))
    embedder = _TextEmbedder({"<b>p</b>": _vec(1, 0)})
    rows, own_ids = build_rows(song_dir, library, embedder, lambda text: "<b>p</b>")
    counts = reuse_counts(rows, library, own_ids, [0.3, 0.6])

    index = write_report(tmp_path / "out", "My <Song>", rows, counts, library, current_threshold=0.28)

    html_text = index.read_text(encoding="utf-8")
    assert index.name == "index.html"
    assert "My &lt;Song&gt;" in html_text and "&lt;b&gt;p&lt;/b&gt;" in html_text and "<b>p</b>" not in html_text
    assert "0.30" in html_text and "0.60" in html_text                   # the threshold table
    assert list((tmp_path / "out" / "thumbs").glob("*.jpg"))
