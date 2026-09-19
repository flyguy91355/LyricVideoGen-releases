"""align_blocks: run the forced aligner one bounded window at a time. The aligner is injected, so these tests
use a fake that spreads a block's words evenly across the window (or fails when the window is too small)."""

import pytest

from lyricvideo.align import AlignmentError, align_blocks
from lyricvideo.anchors import Block

SPF = 0.1            # seconds per model frame


def even_aligner(fail_below_frames=0, always_fail=False):
    """A fake align_window(first_frame, last_frame_exclusive, n_words) -> [(start_frame, end_frame)] relative to
    the window start; words spread evenly across it."""
    calls = []

    def align_window(index, f0, f1, n_words):
        calls.append((f0, f1, n_words))
        frames = f1 - f0
        if always_fail or frames < fail_below_frames:
            raise AlignmentError("window too small")
        step = frames / n_words
        return [(round(k * step), round(k * step + step * 0.8)) for k in range(n_words)]

    align_window.calls = calls
    return align_window


def test_words_land_inside_their_blocks_window():
    blocks = [Block(0, 0, 10.0, 20.0), Block(1, 1, 30.0, 40.0)]

    times, info = align_blocks(blocks, [3, 2], 600, SPF, even_aligner())

    assert len(times) == 5
    assert all(10.0 <= s and e <= 20.0 + 1e-6 for s, e in times[:3])
    assert all(30.0 <= s and e <= 40.0 + 1e-6 for s, e in times[3:])
    assert not any(i.fell_back or i.widened for i in info)


def test_a_window_the_aligner_rejects_is_widened_until_it_works():
    blocks = [Block(0, 0, 10.0, 11.0)]                   # 10 frames: too small for this fake

    align = even_aligner(fail_below_frames=40)
    times, info = align_blocks(blocks, [4], 600, SPF, align)

    assert info[0].widened and not info[0].fell_back
    assert len(align.calls) > 1                          # first try failed, a wider window succeeded
    assert len(times) == 4


def test_a_block_the_aligner_can_never_handle_is_spread_evenly_across_its_window():
    blocks = [Block(0, 0, 10.0, 14.0)]

    times, info = align_blocks(blocks, [4], 600, SPF, even_aligner(always_fail=True))

    assert info[0].fell_back
    assert [round(s, 1) for s, _ in times] == [10.0, 11.0, 12.0, 13.0]
    assert all(10.0 <= s and e <= 14.0 + 1e-6 for s, e in times)


def test_overlapping_windows_never_produce_overlapping_or_backwards_words():
    blocks = [Block(0, 0, 10.0, 22.0), Block(1, 1, 18.0, 30.0)]     # 4 s of overlap

    times, _ = align_blocks(blocks, [3, 3], 600, SPF, even_aligner())

    for (s1, e1), (s2, e2) in zip(times, times[1:]):
        assert s2 >= e1 - 1e-6 and e2 >= s2


def test_windows_are_clamped_to_the_song():
    blocks = [Block(0, 0, 0.0, 60.0)]

    times, _ = align_blocks(blocks, [5], 600, SPF, even_aligner())

    assert times[0][0] >= 0.0 and times[-1][1] <= 60.0 + 1e-6


def test_the_word_count_is_preserved_across_many_blocks():
    blocks = [Block(i, i, i * 10.0, i * 10.0 + 9.0) for i in range(6)]

    times, _ = align_blocks(blocks, [2, 3, 1, 4, 2, 5], 900, SPF, even_aligner())

    assert len(times) == 17


def test_a_block_with_no_words_is_skipped():
    blocks = [Block(0, 0, 0.0, 5.0), Block(1, 1, 5.0, 10.0)]

    times, _ = align_blocks(blocks, [0, 2], 200, SPF, even_aligner())

    assert len(times) == 2


# --- the real glue: one model pass, then windows sliced from it -------------------------------------

import torch
import torchaudio

from lyricvideo.align import align_words_anchored
from lyricvideo.anchors import LineAnchor


class _Span:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Slice:
    def __init__(self, n):
        self.n = n


class _Emission:
    shape = (1, 200, 5)

    def __getitem__(self, idx):
        return _Slice(idx.stop - idx.start) if isinstance(idx, slice) else self


class _Aligner:
    """Spreads the tokens over whatever slice it is given; refuses one that is too short for them."""

    def __call__(self, emission_slice, tokens):
        if emission_slice.n < len(tokens) * 3:
            raise RuntimeError("targets longer than the emission")
        step = emission_slice.n / len(tokens)
        return [[_Span(int(i * step), int(i * step + step * 0.8))] for i in range(len(tokens))]


class _Bundle:
    sample_rate = 16000

    def get_model(self):
        return lambda waveform: (_Emission(), None)

    def get_tokenizer(self):
        return lambda words: [f"tok_{w}" for w in words]

    def get_aligner(self):
        return _Aligner()


def _wav(tmp_path, seconds=8):
    path = tmp_path / "vocals.wav"
    torchaudio.save(str(path), torch.zeros(1, 16000 * seconds), 16000)
    return path


def test_each_line_is_aligned_inside_its_own_anchored_window(tmp_path):
    lines = [["hello", "there"], ["big", "wide", "world"]]
    anchors = {0: LineAnchor(0, 1.0, 2.0, 2, 2), 1: LineAnchor(1, 5.0, 6.5, 3, 3)}

    times, infos = align_words_anchored(_wav(tmp_path), lines, anchors, bundle=_Bundle(), pad=0.5)

    assert len(times) == 5
    assert all(0.45 <= s and e <= 2.6 for s, e in times[:2])
    assert all(4.45 <= s and e <= 7.1 for s, e in times[2:])


def test_without_anchors_the_whole_song_is_a_single_window(tmp_path):
    times, infos = align_words_anchored(_wav(tmp_path), [["a", "b"], ["c", "d"]], {}, bundle=_Bundle())

    assert len(times) == 4 and len(infos) == 1
    assert times[0][0] >= 0.0 and times[-1][1] <= 8.0 + 1e-6


def test_no_words_is_an_error(tmp_path):
    with pytest.raises(AlignmentError):
        align_words_anchored(_wav(tmp_path), [[], []], {}, bundle=_Bundle())


def test_vocal_loudness_is_zero_in_silence_and_positive_where_there_is_sound(tmp_path):
    from lyricvideo.align import vocal_loudness

    wave = torch.cat([torch.zeros(1, 16000 * 2), torch.randn(1, 16000 * 2) * 0.3], dim=1)
    path = tmp_path / "vocals.wav"
    torchaudio.save(str(path), wave, 16000)

    levels = vocal_loudness(path, hop=0.5)

    assert len(levels) == 8
    assert all(l < 1e-4 for l in levels[:4]) and all(l > 0.05 for l in levels[4:])
