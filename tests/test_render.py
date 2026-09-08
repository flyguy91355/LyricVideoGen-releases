import numpy as np
from PIL import Image

from lyricvideo.render import apply_ken_burns, draw_scene, ken_burns_preset_for_key, FRAME_SIZE, KEN_BURNS_PRESETS
from lyricvideo.layout import Scene, SceneLine, SceneWord


def test_apply_ken_burns_returns_frame_sized_image():
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    out_start = apply_ken_burns(img, progress=0.0)
    out_end = apply_ken_burns(img, progress=1.0)
    assert out_start.size == FRAME_SIZE
    assert out_end.size == FRAME_SIZE


def test_apply_ken_burns_pans_toward_end_position():
    # Use a gradient image so different crop regions are actually distinguishable.
    img = Image.new("RGB", (800, 600))
    for x in range(800):
        for y in range(0, 600, 50):
            img.paste((x % 256, 0, 0), (x, y, x + 1, y + 50))

    # left -> right pan at full zoom: start should differ from end
    start = np.array(apply_ken_burns(img, progress=0.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    end = np.array(apply_ken_burns(img, progress=1.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    assert not np.array_equal(start, end)


def test_apply_ken_burns_can_zoom_out():
    img = Image.new("RGB", (800, 600), (50, 50, 50))
    tight = apply_ken_burns(img, progress=0.0, zoom_start=1.2, zoom_end=1.0)
    wide = apply_ken_burns(img, progress=1.0, zoom_start=1.2, zoom_end=1.0)
    assert tight.size == wide.size == FRAME_SIZE


def test_ken_burns_preset_for_key_is_deterministic():
    assert ken_burns_preset_for_key("same-key") == ken_burns_preset_for_key("same-key")


def test_ken_burns_preset_for_key_varies_across_keys():
    presets_seen = {ken_burns_preset_for_key(f"key-{i}") for i in range(len(KEN_BURNS_PRESETS) * 3)}
    # With enough distinct keys, more than just one single preset should appear.
    assert len(presets_seen) > 1


def test_draw_scene_renders_without_error_and_draws_text(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    scene = Scene(
        lines=[
            SceneLine(
                words=[SceneWord(text="hello", chord="G"), SceneWord(text="there")],
                is_current=True,
                distance_from_current=0,
            ),
        ],
        image_key="abc123",
        ken_burns_progress=0.5,
    )

    frame = draw_scene(scene, bg, test_font_path)

    assert frame.size == FRAME_SIZE
    assert frame.getextrema() != ((0, 0), (0, 0), (0, 0))


def test_draw_scene_scroll_progress_shifts_line_position(test_font_path):
    def make_scene(scroll_progress):
        return Scene(
            lines=[
                SceneLine(words=[SceneWord(text="hello")], is_current=True, distance_from_current=0),
            ],
            image_key="abc123",
            ken_burns_progress=0.0,
            scroll_progress=scroll_progress,
        )

    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    frame_at_start = np.array(draw_scene(make_scene(0.0), bg, test_font_path))
    frame_at_mid = np.array(draw_scene(make_scene(0.5), bg, test_font_path))

    # Same text, different vertical position -> the two rendered frames must
    # actually differ (a real regression here would be scroll_progress being
    # silently ignored and every frame rendering identically).
    assert not np.array_equal(frame_at_start, frame_at_mid)


def test_draw_scene_shows_instrumental_chord_instead_of_lines(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    scene = Scene(
        lines=[
            SceneLine(words=[SceneWord(text="should not appear")], is_current=True, distance_from_current=0),
        ],
        image_key="abc123",
        ken_burns_progress=0.0,
        instrumental_chord="Em7",
    )

    frame = draw_scene(scene, bg, test_font_path)

    assert frame.size == FRAME_SIZE
    assert frame.getextrema() != ((0, 0), (0, 0), (0, 0))
