from pathlib import Path

import numpy as np
from PIL import Image

import lyricvideo.library_session as library_session
from lyricvideo.image_library import EMBEDDING_DIM, ImageLibrary
from lyricvideo.library_session import LibrarySession, open_library_session
from lyricvideo.settings import Settings


def _vec(*head: float) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[: len(head)] = head
    return v


class _FakeEmbedder:
    """Prompts map to vectors by exact text; images map to vectors by file name (else `default_image`)."""

    def __init__(self, texts: dict[str, np.ndarray], images: dict[str, np.ndarray] | None = None,
                 default_image: np.ndarray | None = None):
        self.texts, self.images, self.default_image = texts, images or {}, default_image
        self.text_calls = 0
        self.closed = False

    def embed_text(self, texts):
        self.text_calls += 1
        return np.vstack([self.texts[t] for t in texts])

    def embed_images(self, paths):
        return np.vstack([self.images.get(Path(p).name, self.default_image) for p in paths])

    def close(self):
        self.closed = True


def _png(path: Path, color) -> Path:
    Image.new("RGB", (8, 8), color).save(path)
    return path


def _session(tmp_path, texts, min_score=0.5, skip_lookup=False, images=None):
    library = ImageLibrary(tmp_path / "lib")
    embedder = _FakeEmbedder(texts, images)
    return LibrarySession(library, embedder, "my-song", "My Song", min_score, skip_lookup=skip_lookup), library, embedder


def test_a_hit_copies_the_library_image_records_the_reuse_and_returns_the_destination(tmp_path):
    session, library, _ = _session(tmp_path, {"a red door": _vec(1, 0)})
    src = _png(tmp_path / "a.png", (255, 0, 0))
    library.add(src, _vec(1, 0))
    dest = tmp_path / "song" / "line.png"
    dest.parent.mkdir()

    result = session.find_match("a red door", dest)

    assert result == dest and dest.read_bytes() == src.read_bytes()
    assert session.reused == 1 and library.stats()["reuses"] == 1


def test_a_miss_below_the_threshold_returns_none(tmp_path):
    session, library, _ = _session(tmp_path, {"a green field": _vec(0, 1)})
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("a green field", tmp_path / "x.png") is None
    assert session.reused == 0 and not (tmp_path / "x.png").exists()


def test_once_per_song_the_second_line_cannot_take_the_same_image(tmp_path):
    session, library, _ = _session(
        tmp_path, {"first": _vec(1, 0), "second": _vec(1, 0.5)}, min_score=0.4,
    )
    a = library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))
    b = library.add(_png(tmp_path / "b.png", (0, 255, 0)), _vec(0, 1))

    session.find_match("first", tmp_path / "1.png")
    session.find_match("second", tmp_path / "2.png")

    assert (tmp_path / "1.png").read_bytes() == library.image_path(a).read_bytes()
    assert (tmp_path / "2.png").read_bytes() == library.image_path(b).read_bytes()  # next-best, not the same picture


def test_once_per_song_with_no_runner_up_the_second_line_gets_no_match(tmp_path):
    session, library, _ = _session(tmp_path, {"first": _vec(1, 0), "second": _vec(1, 0)}, min_score=0.9)
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("first", tmp_path / "1.png") is not None
    assert session.find_match("second", tmp_path / "2.png") is None


def test_a_purchase_is_added_with_its_prompt_and_never_matched_again_this_song(tmp_path):
    session, library, _ = _session(
        tmp_path, {"same idea": _vec(1, 0)}, images={"bought.png": _vec(1, 0)},
    )
    bought = _png(tmp_path / "bought.png", (10, 20, 30))

    session.record_purchase(bought, "a moody forest", "the line text")

    assert session.bought == 1 and library.count() == 1
    (image_id,) = session.used_ids
    assert library.get(image_id) == {"prompt": "a moody forest", "source_text": "the line text", "song_title": "My Song"}
    assert session.find_match("same idea", tmp_path / "x.png") is None   # its own song's picture is off-limits


def test_a_resumed_song_cannot_repick_a_picture_its_earlier_run_already_used(tmp_path):
    session, library, _ = _session(tmp_path, {"a red door": _vec(1, 0)})
    src = _png(tmp_path / "a.png", (255, 0, 0))
    library.add(src, _vec(1, 0))
    images_dir = tmp_path / "song-images"
    images_dir.mkdir()
    (images_dir / "earlier-line.png").write_bytes(src.read_bytes())   # the earlier run already reused it

    session.seed_used_ids(images_dir)

    assert session.find_match("a red door", tmp_path / "x.png") is None


def test_fresh_images_skips_the_lookup_but_still_files_purchases(tmp_path):
    session, library, embedder = _session(
        tmp_path, {"a red door": _vec(1, 0)}, skip_lookup=True, images={"new.png": _vec(0, 1)},
    )
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("a red door", tmp_path / "x.png") is None
    assert embedder.text_calls == 0

    session.record_purchase(_png(tmp_path / "new.png", (1, 2, 3)), "p", "t")
    assert library.count() == 2


def test_a_library_row_whose_file_was_deleted_is_skipped_not_fatal(tmp_path, capsys):
    session, library, _ = _session(tmp_path, {"one": _vec(1, 0), "two": _vec(1, 0.4)}, min_score=0.3)
    gone = library.add(_png(tmp_path / "gone.png", (255, 0, 0)), _vec(1, 0))
    kept = library.add(_png(tmp_path / "kept.png", (0, 255, 0)), _vec(0, 1))
    library.image_path(gone).unlink()

    assert session.find_match("one", tmp_path / "1.png") is None      # best match was the deleted one
    assert session.enabled is True                                     # ...but the library itself is fine
    assert "WARNING" in capsys.readouterr().err
    assert session.find_match("two", tmp_path / "2.png") is not None   # and the next line can still hit the other image
    assert (tmp_path / "2.png").read_bytes() == library.image_path(kept).read_bytes()


def test_an_embedder_error_disables_the_library_for_the_rest_of_the_song(tmp_path, capsys):
    session, library, embedder = _session(tmp_path, {})    # embed_text raises KeyError for any prompt
    library.add(_png(tmp_path / "a.png", (255, 0, 0)), _vec(1, 0))

    assert session.find_match("anything", tmp_path / "x.png") is None
    assert session.enabled is False
    err = capsys.readouterr().err
    assert "WARNING" in err and "disabled" in err

    calls = embedder.text_calls
    assert session.find_match("again", tmp_path / "y.png") is None
    assert embedder.text_calls == calls                                # not even tried again

    session.record_purchase(_png(tmp_path / "p.png", (5, 5, 5)), "p", "t")
    assert session.bought == 1 and library.count() == 1               # counted, but not filed once disabled


def test_the_summary_line_reports_reuses_purchases_and_the_saving(tmp_path):
    session, _, _ = _session(tmp_path, {})
    session.reused, session.bought = 14, 22

    assert session.summary_line() == "Image library: 14 reused, 22 bought (about $0.04 saved at $0.003/image)"


def test_close_frees_the_embedder(tmp_path):
    session, _, embedder = _session(tmp_path, {})

    session.close()

    assert embedder.closed is True


def test_open_library_session_is_none_when_the_setting_is_off(tmp_path):
    assert open_library_session(Settings(), "song", "Song", tmp_path / "images") is None
    assert open_library_session(None, "song", "Song", tmp_path / "images") is None


def test_open_library_session_builds_a_seeded_session_from_the_settings(tmp_path):
    src = _png(tmp_path / "a.png", (255, 0, 0))
    library = ImageLibrary(tmp_path / "lib")
    library.add(src, _vec(1, 0))
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "x.png").write_bytes(src.read_bytes())

    session = open_library_session(
        Settings(use_image_library=True, image_library_min_score=0.33), "song", "Song", images_dir,
        fresh_images=True, embedder=_FakeEmbedder({}), library=library,
    )

    assert session is not None
    assert session.min_score == 0.33 and session.skip_lookup is True and session.song_slug == "song"
    assert len(session.used_ids) == 1


def test_open_library_session_falls_back_quietly_when_the_model_is_not_installed(tmp_path, monkeypatch, capsys):
    class _NoModel:
        def __init__(self, allow_download=False):
            assert allow_download is False

        def check_available(self):
            raise library_session.EmbedderUnavailable("CLIP weights are not downloaded")

    monkeypatch.setattr(library_session, "ClipEmbedder", _NoModel)

    session = open_library_session(Settings(use_image_library=True), "song", "Song", tmp_path / "images")

    assert session is None
    assert "import_image_library" in capsys.readouterr().err


def test_a_library_bought_image_serves_a_second_song_end_to_end(tmp_path, monkeypatch):
    """Song one buys (and files) a picture; song two, with a similar prompt, reuses it and never calls Replicate."""
    from lyricvideo.imagery import get_or_generate_image

    class _Claude:
        def __init__(self, prompt):
            self.messages = self
            self._prompt = prompt

        def create(self, **kwargs):
            block = type("B", (), {"type": "text", "text": self._prompt})()
            return type("R", (), {"content": [block]})()

    bought = []

    def _fake_generate(token, prompt, out_path, *a, **k):
        bought.append(prompt)
        _png(out_path, (200, 10, 10))
        return out_path

    monkeypatch.setattr("lyricvideo.imagery.generate_line_image", _fake_generate)
    library = ImageLibrary(tmp_path / "lib")
    texts = {"a red door at dusk": _vec(1, 0), "a crimson doorway in twilight": _vec(0.95, 0.1)}

    for slug, prompt in [("song-one", "a red door at dusk"), ("song-two", "a crimson doorway in twilight")]:
        images_dir = tmp_path / slug / "images"
        # every picture a song buys is fingerprinted as the same "red door" vector
        session = LibrarySession(library, _FakeEmbedder(texts, default_image=_vec(1, 0)), slug, slug, 0.8)
        session.seed_used_ids(images_dir)
        get_or_generate_image(_Claude(prompt), "tok", "gist", f"{slug} lyric", images_dir, library=session)

    assert bought == ["a red door at dusk"]                     # only song one paid for anything
    assert library.stats() == {"images": 1, "with_prompt": 1, "reuses": 1}
    assert len(list((tmp_path / "song-two" / "images").glob("*.png"))) == 1
