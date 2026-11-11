"""The pipeline checks every picture before the video (owner, 2026-11-11): a bad one is replaced, a song whose pictures
cannot be checked is held, and a render-only start checks the pictures already on disk."""
import json
from pathlib import Path

import pytest
from PIL import Image

from lyricvideo.models import line_hash
from lyricvideo.pipeline import HELD_MARKER, PICTURES_NOT_CHECKED_PREFIX, HeldBeforeVideo, run_pipeline
from tests.test_pipeline import _patch_common

BAD = (200, 0, 0)


def _real_pictures(monkeypatch, first_color=BAD):
    """get_or_generate_image writes a real picture per line; the very first one bought is `first_color`."""
    bought = []

    def fake_get(anthropic_client, token, gist, text, images_dir, **kwargs):
        path = Path(images_dir) / f"{line_hash(text)}.png"
        if not path.exists():
            color = first_color if not bought else (10, 10 + len(bought), 10)
            Image.new("RGB", (16, 9), color).save(path)
            bought.append(text)
        return path

    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", fake_get)
    return bought


def _verdict_by_colour(monkeypatch, bad_colours=(BAD,)):
    """Claude Code's answer, judged from the picture it was given: the BAD colour is a hand with no arm."""
    asked = []

    def run(args, cwd):
        name = args[-1].split()[3]           # the check prompt: "Read the picture <name> and decide ..."
        asked.append(name)
        bad = Image.open(Path(cwd) / name).convert("RGB").getpixel((0, 0)) in bad_colours
        return json.dumps({"structured_output": {"verdict": "bad" if bad else "good",
                                                 "problem": "a hand with no arm" if bad else ""}})

    monkeypatch.setattr("lyricvideo.picture_check._run_claude", run)
    return asked


def test_a_bad_picture_is_replaced_and_rechecked_before_the_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    bought = _real_pictures(monkeypatch)
    asked = _verdict_by_colour(monkeypatch)
    rendered = []
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video",
                        lambda lines, chords, images_dir, *a, **k: rendered.append(sorted(images_dir.glob("*.png"))))

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    first_line = bought[0]
    assert bought.count(first_line) == 2             # bought, judged bad, bought again
    pictures = rendered[0]
    assert pictures and all(Image.open(p).convert("RGB").getpixel((0, 0)) != BAD for p in pictures)
    assert len(asked) == len(set(bought)) + 1          # every picture checked once, plus the replacement


def test_pictures_that_cannot_be_checked_hold_the_song(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    _real_pictures(monkeypatch)
    monkeypatch.setattr("lyricvideo.picture_check._run_claude",
                        lambda args, cwd: json.dumps({"is_error": True, "result": "Claude usage limit reached"}))

    def must_not_render(*a, **k):
        raise AssertionError("no video from unchecked pictures")

    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", must_not_render)
    work = tmp_path / "work"
    with pytest.raises(HeldBeforeVideo) as held:
        run_pipeline(Path("audio.mp3"), work)
    assert held.value.concern.startswith(PICTURES_NOT_CHECKED_PREFIX)
    assert "usage limit" in held.value.concern
    assert PICTURES_NOT_CHECKED_PREFIX in (work / HELD_MARKER).read_text()


def test_a_render_only_start_checks_the_pictures_already_on_disk(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work = tmp_path / "work"
    bought = _real_pictures(monkeypatch, first_color=(1, 2, 3))
    run_pipeline(Path("audio.mp3"), work)                       # every picture checked good once
    before = len(bought)
    asked = _verdict_by_colour(monkeypatch, bad_colours=((1, 2, 3),))

    run_pipeline(Path("audio.mp3"), work, start_stage="render")

    assert asked == []                  # all already known good: nothing sent again, nothing bought
    assert len(bought) == before


def test_a_render_only_start_replaces_a_picture_since_judged_bad(tmp_path, monkeypatch):
    from lyricvideo.image_library import ImageLibrary, content_id

    _patch_common(monkeypatch, tmp_path)
    work = tmp_path / "work"
    bought = _real_pictures(monkeypatch, first_color=(1, 2, 3))
    run_pipeline(Path("audio.mp3"), work)
    first = work / "images" / f"{line_hash(bought[0])}.png"
    lib = ImageLibrary()
    lib.record_check(content_id(first), "bad", "a broken neck", "opus")        # e.g. found bad in another song since
    lib.close()

    run_pipeline(Path("audio.mp3"), work, start_stage="render")

    assert bought.count(bought[0]) == 2                     # moved out and bought again before the render
    assert Image.open(first).convert("RGB").getpixel((0, 0)) != (1, 2, 3)


def test_a_render_only_start_never_buys_a_picture_that_was_simply_missing(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work = tmp_path / "work"
    bought = _real_pictures(monkeypatch, first_color=(1, 2, 3))
    run_pipeline(Path("audio.mp3"), work)
    (work / "images" / f"{line_hash(bought[0])}.png").unlink()        # a line with no picture (the render shows a neighbour)
    before = len(bought)

    run_pipeline(Path("audio.mp3"), work, start_stage="render")

    assert len(bought) == before
