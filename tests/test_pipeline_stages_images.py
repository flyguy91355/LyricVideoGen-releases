"""The images and render stages (issue #7 review, wave 2): nothing is paid for when every picture already exists (F131);
a song whose every picture failed is not rendered as flat colour (ALL-FALLBACK); the Settings Font reaches real renders
(F065/F132); pictures a Redo replaces are recorded as rejected at once (REJECTED-IDS); an undecodable picture never stops
a render (UNDECODABLE-IMAGE)."""

import json
import logging
from pathlib import Path

import pytest
from PIL import Image

from lyricvideo.assemble import _BackgroundCache
from lyricvideo.imagery import FRAME_SIZE, get_or_generate_image as real_get_or_generate_image
from lyricvideo.layout import instrumental_image_captions
from lyricvideo.models import line_hash, load_song
from lyricvideo.pipeline import prepare_images_for_fresh_regeneration, resolve_font, run_pipeline, song_end_time
from lyricvideo.settings import Settings
from tests.test_pipeline import _patch_common


def _must_not_run(*args, **kwargs):
    raise AssertionError("nothing may be paid for when every picture already exists")


def _picture(path: Path, color=(200, 40, 40), size=(64, 36)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    image.putpixel((0, 0), (1, 2, 3))              # not a single solid colour
    image.save(path)
    return path


def _cache_every_picture(work_dir: Path, skip: int = 0) -> list[str]:
    """Writes a real picture for every line and instrumental caption the images stage will ask for, except the first
    `skip`; returns every text."""
    song = load_song(work_dir / "lyrics_timed.json")
    texts = [line.text for line in song.lines] + instrumental_image_captions(song.lines, song.chord_track, song_end_time(song))
    for text in texts[skip:]:
        _picture(work_dir / "images" / f"{line_hash(text)}.png")
    return texts


# --- F131 ---------------------------------------------------------------------------------------------------------------------

def test_a_rerun_whose_pictures_all_exist_needs_no_claude_call_and_no_replicate_token(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)
    _cache_every_picture(work_dir)
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", real_get_or_generate_image)
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", _must_not_run)
    monkeypatch.setattr("lyricvideo.imagery.build_image_prompt", _must_not_run)
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": _must_not_run}))
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    rendered = []
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: rendered.append(1))

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")

    assert rendered == [1]


def test_pictures_kept_in_an_images_backup_folder_count_as_existing(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)
    _cache_every_picture(work_dir)
    (work_dir / "images").rename(work_dir / "images_backup_20260101")
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", real_get_or_generate_image)
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", _must_not_run)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")

    assert len(list((work_dir / "images").glob("*.png"))) == len(list((work_dir / "images_backup_20260101").glob("*.png")))


def test_one_missing_picture_pays_for_the_song_gist_exactly_once(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)
    _cache_every_picture(work_dir, skip=1)
    gists = []
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: gists.append(1) or "a made-up gist")

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")

    assert gists == [1]


def test_a_placeholder_or_broken_cached_picture_counts_as_missing(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)
    texts = _cache_every_picture(work_dir)
    Image.new("RGB", FRAME_SIZE, (30, 30, 40)).save(work_dir / "images" / f"{line_hash(texts[0])}.png")   # placeholder
    gists = []
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: gists.append(1) or "gist")
    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")
    assert gists == [1]

    _cache_every_picture(work_dir)
    broken = work_dir / "images" / f"{line_hash(texts[-1])}.png"
    broken.write_bytes(broken.read_bytes()[:40])                                  # cut short
    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")
    assert gists == [1, 1]


# --- ALL-FALLBACK -------------------------------------------------------------------------------------------------------------

def test_a_song_whose_every_picture_failed_is_not_rendered(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)

    def placeholder(client, token, gist, text, cache_dir, **kwargs):
        path = Path(cache_dir) / f"{line_hash(text)}.png"
        Image.new("RGB", FRAME_SIZE, (30, 30, 40)).save(path)
        return path

    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", placeholder)
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: pytest.fail("a flat-colour video"))

    with pytest.raises(RuntimeError, match="Every background image failed"):
        run_pipeline(Path("audio.mp3"), tmp_path / "work")


# --- F065 / F132: the Settings Font --------------------------------------------------------------------------------------------

def _render_font(monkeypatch, tmp_path, **run_kwargs):
    fonts = []
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda lines, chords, images, audio, out, font, *a, **k: fonts.append(font))
    monkeypatch.setattr("lyricvideo.pipeline.default_font", lambda: "DEFAULT-FONT")
    run_pipeline(Path("audio.mp3"), tmp_path / "work", **run_kwargs)
    return fonts[0]


def test_the_settings_font_is_the_font_real_videos_are_drawn_with(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    chosen = tmp_path / "Chosen-Bold.ttf"
    chosen.write_bytes(b"a font file")

    font = _render_font(monkeypatch, tmp_path, settings=Settings(font_path=str(chosen), generate_easy_chord_versions=False))

    assert font == str(chosen)


def test_a_settings_font_that_is_gone_falls_back_to_the_default_font(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)

    font = _render_font(
        monkeypatch, tmp_path, settings=Settings(font_path=str(tmp_path / "moved.ttf"), generate_easy_chord_versions=False),
    )

    assert font == "DEFAULT-FONT"


def test_the_callers_own_font_still_wins_over_the_settings_font(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    cli_font, settings_font = tmp_path / "cli.ttf", tmp_path / "settings.ttf"
    cli_font.write_bytes(b"x")
    settings_font.write_bytes(b"y")

    font = _render_font(
        monkeypatch, tmp_path, font_path=str(cli_font),
        settings=Settings(font_path=str(settings_font), generate_easy_chord_versions=False),
    )

    assert font == str(cli_font)


def test_resolve_font_with_nothing_chosen_is_the_default_font(monkeypatch):
    monkeypatch.setattr("lyricvideo.pipeline.default_font", lambda: "DEFAULT-FONT")
    assert resolve_font(None) == resolve_font("") == "DEFAULT-FONT"


# --- REJECTED-IDS ----------------------------------------------------------------------------------------------------------------

def test_pictures_moved_aside_for_new_images_are_recorded_as_rejected_at_once(tmp_path):
    from lyricvideo.image_library import content_id
    from lyricvideo.library_session import REJECTED_IMAGES_FILE

    work_dir = tmp_path / "some-song"
    first = _picture(work_dir / "images" / "a.png", (10, 200, 10))
    second = _picture(work_dir / "images" / "b.png", (10, 10, 200))
    ids = {content_id(first), content_id(second)}

    moved = prepare_images_for_fresh_regeneration(work_dir / "images")

    assert moved is not None and moved.name.startswith("images_prior_")
    assert set(json.loads((work_dir / REJECTED_IMAGES_FILE).read_text(encoding="utf-8"))) == ids


def test_a_failure_to_record_the_rejected_pictures_never_stops_the_redo(tmp_path, monkeypatch):
    def broken(work_dir):
        raise OSError("disk full")

    monkeypatch.setattr("lyricvideo.library_session.rejected_image_ids", broken)
    work_dir = tmp_path / "some-song"
    _picture(work_dir / "images" / "a.png")

    moved = prepare_images_for_fresh_regeneration(work_dir / "images")

    assert moved is not None and (moved / "a.png").exists()


# --- UNDECODABLE-IMAGE ------------------------------------------------------------------------------------------------------------

def test_an_undecodable_picture_shows_its_nearest_readable_neighbour_and_is_warned_about_once(tmp_path, caplog):
    images = tmp_path / "images"
    _picture(images / "k1.png", (250, 0, 0))
    broken = _picture(images / "k2.png", (0, 250, 0))
    broken.write_bytes(broken.read_bytes()[:30])
    _picture(images / "k3.png", (0, 0, 250))
    cache = _BackgroundCache(images, (32, 18), (30, 30, 40), order=["k3", "k2", "k1"])

    with caplog.at_level(logging.WARNING, logger="lyricvideo.assemble"):
        first = cache.get("k2")
        again = cache.get("k2")

    assert first.getpixel((10, 10)) == (0, 0, 250)                                # k3: the picture before it in the song
    assert again is first
    assert sum("k2.png" in record.getMessage() for record in caplog.records) == 1


def test_a_song_whose_every_picture_is_undecodable_renders_the_fallback_colour_instead_of_failing(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "only.png").write_bytes(b"not a png at all")
    cache = _BackgroundCache(images, (32, 18), (30, 30, 40))

    assert cache.get("only").getpixel((5, 5)) == (30, 30, 40)
    assert cache.get("some-missing-key").getpixel((5, 5)) == (30, 30, 40)
