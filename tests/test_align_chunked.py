"""Long songs must not need the whole vocal stem in the model at once: 'Tuesday's Gone' (7.5 min) was killed at 10.3 GB and
'November Rain' (9 min) would be worse (owner's redo queue, 2026-09-20; the known 20+ minute limit was the same problem).
The emission is computed in one-minute pieces with a little overlap and stitched, frame for frame identical to one pass."""

import torch

from lyricvideo.align import _emission_in_pieces

HOP, RECEPTIVE, SAMPLE_RATE = 320, 400, 16000


class _LocalModel:
    """Stands in for wav2vec2: one frame per 320 samples, each frame a function of only its own 400-sample window."""

    def __init__(self):
        self.lengths = []

    def __call__(self, waveform):
        self.lengths.append(waveform.shape[1])
        windows = waveform[0].unfold(0, RECEPTIVE, HOP)                       # (frames, 400)
        return torch.stack([windows.mean(dim=1), windows.abs().max(dim=1).values], dim=1).unsqueeze(0), None


def _noise(seconds):
    generator = torch.Generator().manual_seed(7)
    return torch.randn(1, int(seconds * SAMPLE_RATE) + 123, generator=generator)      # not a multiple of the hop


def test_pieces_reproduce_the_one_pass_emission_exactly_for_a_local_model():
    waveform = _noise(5.3)
    whole, _ = _LocalModel()(waveform)

    pieces = _emission_in_pieces(_LocalModel(), waveform, piece_seconds=1.0, pad_seconds=0.2)

    assert pieces.shape == whole.shape
    assert torch.allclose(pieces, whole)


def test_the_model_never_sees_more_than_a_piece_plus_its_padding():
    model, waveform = _LocalModel(), _noise(5.3)

    _emission_in_pieces(model, waveform, piece_seconds=1.0, pad_seconds=0.2)

    assert max(model.lengths) <= int(1.4 * SAMPLE_RATE) and len(model.lengths) >= 5


def test_a_short_song_is_still_a_single_pass():
    model = _LocalModel()

    _emission_in_pieces(model, _noise(1.2), piece_seconds=1.0, pad_seconds=0.2)

    assert len(model.lengths) == 1


def test_lengths_that_end_exactly_on_a_piece_boundary_work():
    waveform = torch.randn(1, 3 * SAMPLE_RATE)
    whole, _ = _LocalModel()(waveform)

    pieces = _emission_in_pieces(_LocalModel(), waveform, piece_seconds=1.0, pad_seconds=0.2)

    assert torch.allclose(pieces, whole)
