import json
from types import SimpleNamespace

from PIL import Image

from lyricvideo import thumbnail as th


def solid(color, size=(1280, 720)):
    return Image.new("RGB", size, color)


def test_score_prefers_bright_contrasty_colourful_right_side():
    dark = solid((10, 10, 14))
    bright = Image.new("RGB", (1280, 720), (230, 150, 40))
    for x in range(0, 1280, 40):
        for y in range(0, 720, 40):
            if (x // 40 + y // 40) % 2:
                bright.paste((20, 30, 70), (x, y, x + 40, y + 40))
    assert th.background_score(bright) > th.background_score(dark) + 0.3


def test_dark_pictures_are_lifted_and_bright_ones_left_alone():
    dark = solid((30, 30, 40))
    lifted = th.lift_if_dark(dark)
    assert lifted.getpixel((900, 300))[0] > 30
    bright = solid((200, 180, 150))
    assert th.lift_if_dark(bright).getpixel((900, 300)) == (200, 180, 150)


def test_compose_makes_a_1280x720_jpeg_under_the_limit_with_a_clear_corner(tmp_path):
    bg = solid((200, 120, 60))
    out = th.compose_thumbnail(bg, "Stairway to Heaven", "Led Zeppelin", tmp_path / "t.jpg")
    assert out.stat().st_size < th.MAX_BYTES
    with Image.open(out) as img:
        assert img.size == (1280, 720) and img.format == "JPEG"
        corner = img.crop((1140, 650, 1280, 720))                   # YouTube's duration badge: nothing of ours there
        assert corner.getpixel((70, 35))[0] > 150                  # still the picture, not text/outline
        tag = img.getpixel((60, 80))
        assert tag[0] > 150 and tag[1] < 60                         # the red PLAY ALONG tag, top-left


def test_a_long_title_is_shown_whole_in_a_narrow_font(tmp_path):
    from PIL import ImageDraw
    out = th.compose_thumbnail(solid((90, 90, 120)), "Sorry Seems to Be the Hardest Word", "Elton John", tmp_path / "t.jpg")
    assert out.exists()
    draw = ImageDraw.Draw(solid((0, 0, 0)))
    font, lines, size = th._fit_title(draw, "SORRY SEEMS TO BE THE HARDEST WORD", 780, 390, None)
    assert " ".join(lines) == "SORRY SEEMS TO BE THE HARDEST WORD" and len(lines) <= 3     # nothing dropped
    assert size >= 100                                                                          # and still big


class Claude:
    def __init__(self, text="A guitar glowing in a forest"):
        self.text, self.calls, self.messages = text, [], self

    def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], usage=usage)


def test_the_prompt_asks_for_one_guitar_no_people_no_text_and_a_clear_bottom():
    p = th.build_prompt_request("Blackbird", "The Beatles", "blackbird singing in the dead of night")
    for needle in ("ONE GUITAR", "No people or faces", "no piano", "no text", "bottom edge"):
        assert needle in p


def test_generate_buys_two_candidates_keeps_the_better_and_cleans_up(tmp_path, monkeypatch):
    colours = iter([(10, 10, 14), (230, 150, 40)])                 # the second is brighter, so it wins

    def fake_generate(token, prompt, out_path, **kw):
        Image.new("RGB", (1280, 720), next(colours)).save(out_path)
        return out_path

    monkeypatch.setattr(th, "generate_line_image", fake_generate)
    claude = Claude()
    result = th.generate_thumbnail(tmp_path, claude, "tok", title="Blackbird", artist="The Beatles", lyrics="x", candidates=2)
    assert result.path == tmp_path / th.THUMBNAIL_FILE and result.path.exists()
    assert claude.calls[0]["model"] == "claude-haiku-4-5"
    assert abs(result.cost_usd - (0.001 + 0.0005 + 2 * 0.003)) < 1e-9
    with Image.open(tmp_path / th.THUMBNAIL_BG_FILE) as bg:
        assert bg.getpixel((900, 300)) == (230, 150, 40)
    assert not list(tmp_path.glob("thumbnail_candidate_*"))


def test_one_failed_candidate_is_survivable_but_none_raises(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(token, prompt, out_path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("replicate down")
        Image.new("RGB", (1280, 720), (120, 120, 120)).save(out_path)

    monkeypatch.setattr(th, "generate_line_image", flaky)
    assert th.generate_thumbnail(tmp_path, Claude(), "tok", title="t", artist="a", lyrics="x").path.exists()
    monkeypatch.setattr(th, "generate_line_image", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    import pytest
    with pytest.raises(RuntimeError):
        th.generate_thumbnail(tmp_path / "x", Claude(), "tok", title="t", artist="a", lyrics="x") if (tmp_path / "x").mkdir() is None else None


def test_empty_prompt_raises(tmp_path):
    import pytest
    with pytest.raises(RuntimeError):
        th.write_background_prompt(Claude(text=""), "t", "a", "x")


def test_easy_version_reuses_its_songs_picture_with_its_own_tag(tmp_path):
    song, easy = tmp_path / "song", tmp_path / "song" / "easychords"
    easy.mkdir(parents=True)
    assert th.compose_from_saved_background(easy, song, title="T", artist="A", tag="EASY CHORDS") is None
    solid((200, 120, 60)).save(song / th.THUMBNAIL_BG_FILE)
    out = th.compose_from_saved_background(easy, song, title="T", artist="A", tag="EASY CHORDS")
    assert out == easy / th.THUMBNAIL_FILE and out.exists()


def test_the_subject_is_moved_to_the_right_away_from_the_title():
    left = Image.new("RGB", (1280, 720), (8, 8, 12))
    for x in range(120, 260):
        for y in range(300, 420):
            left.putpixel((x, y), (255, 160, 30))                  # a lit subject on the LEFT
    assert th.subject_centre(left)[0] < 0.3
    moved = th.reframe_subject_right(left)
    assert 0.55 < th.subject_centre(moved)[0] < 0.9               # now on the right


def test_a_picture_with_nothing_standing_out_is_just_zoomed():
    plain = solid((90, 90, 90))
    out = th.reframe_subject_right(plain)
    assert out.size[0] < 1280 and abs(out.size[0] / out.size[1] - 16 / 9) < 0.02
