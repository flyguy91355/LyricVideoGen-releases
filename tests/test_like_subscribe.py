import numpy as np
from PIL import Image

from lyricvideo import render
from lyricvideo.render import draw_like_subscribe, like_subscribe_state
from lyricvideo.settings import Settings


def test_state_is_invisible_outside_the_window():
    assert like_subscribe_state(-0.1, 10).alpha == 0
    assert like_subscribe_state(10.0, 10).alpha == 0
    assert like_subscribe_state(1.0, 0).alpha == 0


def test_state_fades_in_and_out_and_clicks_in_order():
    s0, s_mid, s_end = like_subscribe_state(0.05, 10), like_subscribe_state(5.0, 10), like_subscribe_state(9.95, 10)
    assert 0 < s0.alpha < 0.5 and s_mid.alpha == 1.0 and 0 < s_end.alpha < 0.5
    assert like_subscribe_state(0.1, 10).cursor is None            # cursor not there yet
    assert 0 < like_subscribe_state(0.6, 10).cursor < 1             # gliding in
    assert like_subscribe_state(1.1, 10).pressed and not like_subscribe_state(1.1, 10).subscribed
    after = like_subscribe_state(1.3, 10)
    assert after.subscribed and not after.pressed and after.ring
    assert like_subscribe_state(5.0, 10).subscribed and not like_subscribe_state(5.0, 10).ring   # rings only briefly


def test_a_short_count_in_still_plays_the_whole_click():
    window = 2.0                                                    # speed-up: the click finishes inside it
    states = [like_subscribe_state(i * 0.05, window) for i in range(40)]
    assert any(s.pressed for s in states) and any(s.subscribed for s in states)
    assert states[-1].alpha < 1.0                                   # fading out at the end


def _frame():
    return Image.new("RGB", (1920, 1080), (40, 60, 90))


def test_draw_leaves_the_frame_untouched_outside_the_window(test_font_path):
    frame = _frame()
    out = draw_like_subscribe(frame, 12.0, 10.0, test_font_path)
    assert out is frame


def test_draw_paints_the_upper_right_inside_the_window(test_font_path):
    frame = _frame()
    out = draw_like_subscribe(frame, 5.0, 10.0, test_font_path, benefit_text="More songs")
    diff = np.argwhere(np.any(np.array(out) != np.array(frame), axis=2))
    ys, xs = diff[:, 0], diff[:, 1]
    assert ys.size and xs.min() > 1920 // 2 and ys.max() < 1080 // 3          # upper-right only
    assert np.array_equal(np.array(frame), np.array(_frame()))                  # copy-on-write


def test_under_the_support_label_it_sits_lower(test_font_path):
    plain = draw_like_subscribe(_frame(), 5.0, 10.0, test_font_path)
    under = draw_like_subscribe(_frame(), 5.0, 10.0, test_font_path, support_text="Support us")
    top = lambda im: np.argwhere(np.any(np.array(im) != np.array(_frame()), axis=2))[:, 0].min()
    assert top(under) > top(plain)


def test_benefit_line_is_optional_and_makes_it_taller(test_font_path):
    bottom = lambda im: np.argwhere(np.any(np.array(im) != np.array(_frame()), axis=2))[:, 0].max()
    without = draw_like_subscribe(_frame(), 5.0, 10.0, test_font_path)
    with_line = draw_like_subscribe(_frame(), 5.0, 10.0, test_font_path, benefit_text="New songs every day")
    assert bottom(with_line) > bottom(without)


def test_caches_are_cleared_with_the_others(test_font_path):
    draw_like_subscribe(_frame(), 5.0, 10.0, test_font_path)
    assert render._like_subscribe_patch.cache_info().currsize > 0
    render.clear_overlay_caches()
    assert render._like_subscribe_patch.cache_info().currsize == 0


def test_settings_defaults_and_wrong_types():
    s = Settings()
    assert s.show_like_subscribe and s.like_subscribe_on_countdown and s.like_subscribe_lead_seconds == 10.0
    assert s.like_subscribe_text == "New Play Along songs every day"
    k = s.render_kwargs()
    assert k["show_like_subscribe"] is True and k["like_subscribe_text"] == s.like_subscribe_text
