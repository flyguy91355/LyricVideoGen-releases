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


def _forget_verdicts():
    """As if the song was made before the picture check existed (2026-11-11): no picture of it was ever judged."""
    from lyricvideo.image_library import ImageLibrary

    lib = ImageLibrary()
    lib._db.execute("DELETE FROM picture_checks")
    lib._db.commit()
    lib.close()


def test_a_video_made_before_the_check_is_held_at_upload_for_a_bad_picture_then_fixed_by_render_anyway(
        tmp_path, monkeypatch):
    """Owner, 2026-11-13 (Quarters and Dimes, made 11-07): "a cut off hand on a truck fender.. i thought we got rid of
    that stuff?" -- a video made before the check had never had its pictures looked at."""
    from lyricvideo.gui import _stage_to_resume
    from lyricvideo.models import load_song
    from lyricvideo.owner_verified import mark_verified
    from lyricvideo.pipeline import BAD_PICTURES_PREFIX, hold_for_bad_pictures_before_upload, review_concern

    _patch_common(monkeypatch, tmp_path)
    work = tmp_path / "work"
    bought = _real_pictures(monkeypatch)
    run_pipeline(Path("audio.mp3"), work)
    _forget_verdicts()
    video = work / "test-song.mp4"
    video.write_bytes(b"the old video")
    mark_verified(work)                                     # the owner approved its lyrics and timing
    asked = _verdict_by_colour(monkeypatch)

    concern = hold_for_bad_pictures_before_upload(work, load_song(work / "lyrics_timed.json"))

    assert concern.startswith(BAD_PICTURES_PREFIX) and repr(bought[0]) in concern and "Render Anyway" in concern
    assert len(asked) == len(set(bought))                   # every picture looked at once
    assert not video.exists() and (work / "test-song.previous.mp4").exists()    # out of Pending and Upload
    assert json.loads((work / HELD_MARKER).read_text())["reason"] == concern
    assert (work / "images" / f"{line_hash(bought[0])}.png").exists()           # moved out only by the render below
    assert review_concern(work) == concern                  # Flagged says why, though the song is verified

    asked.clear()
    run_pipeline(Path("audio.mp3"), work, start_stage=_stage_to_resume(work))   # Render Anyway

    assert bought.count(bought[0]) == 2                     # the bad picture bought again, nothing else
    assert len(asked) == 1                                  # only the replacement is checked
    assert not (work / HELD_MARKER).exists()
    assert hold_for_bad_pictures_before_upload(work, load_song(work / "lyrics_timed.json")) == ""
    assert len(asked) == 1


def test_pictures_that_cannot_be_checked_at_upload_change_nothing(tmp_path, monkeypatch):
    from lyricvideo.models import load_song
    from lyricvideo.picture_check import PictureCheckUnavailable
    from lyricvideo.pipeline import hold_for_bad_pictures_before_upload

    _patch_common(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _real_pictures(monkeypatch, first_color=(1, 2, 3))
    run_pipeline(Path("audio.mp3"), work)
    _forget_verdicts()
    video = work / "test-song.mp4"
    video.write_bytes(b"the old video")
    monkeypatch.setattr("lyricvideo.picture_check._run_claude",
                        lambda args, cwd: json.dumps({"is_error": True, "result": "Claude usage limit reached"}))

    with pytest.raises(PictureCheckUnavailable, match="usage limit"):
        hold_for_bad_pictures_before_upload(work, load_song(work / "lyrics_timed.json"))
    assert video.exists() and not (work / HELD_MARKER).exists()


def test_a_render_that_throws_out_the_thumbnails_picture_makes_a_new_thumbnail(tmp_path, monkeypatch):
    from lyricvideo.image_library import ImageLibrary, content_id
    from lyricvideo.thumbnail import THUMBNAIL_BG_FILE, THUMBNAIL_FILE, THUMBNAIL_OWN_FILE

    _patch_common(monkeypatch, tmp_path)
    work = tmp_path / "work"
    bought = _real_pictures(monkeypatch, first_color=(1, 2, 3))
    run_pipeline(Path("audio.mp3"), work)
    first, second = (work / "images" / f"{line_hash(t)}.png" for t in bought[:2])
    for path in (work / THUMBNAIL_FILE, work / THUMBNAIL_OWN_FILE):
        path.write_text("1")
    Image.open(first).convert("RGB").save(work / THUMBNAIL_BG_FILE)          # ensure_thumbnail re-saves its pick
    lib = ImageLibrary()
    lib.record_check(content_id(second), "bad", "warped fingers", "opus")       # not the thumbnail's picture
    lib.close()

    run_pipeline(Path("audio.mp3"), work, start_stage="render")
    assert (work / THUMBNAIL_FILE).exists()                 # its picture was fine: kept

    lib = ImageLibrary()
    lib.record_check(content_id(first), "bad", "a hand with no arm", "opus")
    lib.close()
    run_pipeline(Path("audio.mp3"), work, start_stage="render")

    assert not (work / THUMBNAIL_FILE).exists() and not (work / THUMBNAIL_BG_FILE).exists()
    assert not (work / THUMBNAIL_OWN_FILE).exists()


def test_the_upload_remakes_a_video_made_before_the_check_with_a_new_picture(tmp_path, monkeypatch):
    """Owner, 2026-11-13: "if a bad pic is found then its replaced and then it rendered anyways, without me doing it..
    then i will review after" -- what Render Anyway does, done by the upload itself."""
    from lyricvideo.models import load_song
    from lyricvideo.owner_verified import mark_verified, verification
    from lyricvideo.pipeline import hold_for_bad_pictures_before_upload, remake_video_with_new_pictures

    _patch_common(monkeypatch, tmp_path)
    work = tmp_path / "work"
    bought = _real_pictures(monkeypatch)
    run_pipeline(Path("audio.mp3"), work)
    _forget_verdicts()
    mark_verified(work)
    (work / "test-song.mp4").write_bytes(b"the old video")
    asked = _verdict_by_colour(monkeypatch)
    rendered = []

    def render(lines, chords, images_dir, audio_path, final_path, *a, **k):
        rendered.append(sorted(images_dir.glob("*.png")))
        Path(final_path).write_bytes(b"the new video")

    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", render)

    concern = hold_for_bad_pictures_before_upload(work, load_song(work / "lyrics_timed.json"), remaking=True)
    assert "being replaced" in json.loads((work / HELD_MARKER).read_text())["reason"] == concern
    asked.clear()
    remake_video_with_new_pictures(work, None)

    assert bought.count(bought[0]) == 2 and len(asked) == 1          # only the bad picture bought again and checked
    assert rendered and all(Image.open(p).convert("RGB").getpixel((0, 0)) != BAD for p in rendered[-1])
    assert (work / "test-song.mp4").read_bytes() == b"the new video" and not (work / HELD_MARKER).exists()
    assert verification(work) is not None                           # the owner's approval of the lyrics still stands
    assert hold_for_bad_pictures_before_upload(work, load_song(work / "lyrics_timed.json")) == ""


def test_a_video_started_while_another_is_being_made_waits_for_it(tmp_path, monkeypatch):
    """A remake inside an upload runs while the window looks idle: a Generate started then waits, never runs beside it."""
    import threading

    from lyricvideo import pipeline

    _patch_common(monkeypatch, tmp_path)
    done = []
    pipeline._ONE_VIDEO_AT_A_TIME.acquire()                         # the upload's remake is running
    try:
        other = threading.Thread(target=lambda: done.append(run_pipeline(Path("audio.mp3"), tmp_path / "work")))
        other.start()
        other.join(timeout=1.0)
        assert other.is_alive() and not done
    finally:
        pipeline._ONE_VIDEO_AT_A_TIME.release()
    other.join(timeout=60)
    assert done
