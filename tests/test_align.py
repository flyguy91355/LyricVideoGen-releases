from pathlib import Path

import pytest
import torch
import torchaudio

from lyricvideo.align import align_words, AlignmentError, _normalize_word_for_alignment


class _FakeSpan:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class _FakeEmission:
    shape = (1, 100, 5)

    def __getitem__(self, idx):
        return self


class _FakeModel:
    def __call__(self, waveform):
        return _FakeEmission(), None


class _FakeTokenizer:
    def __call__(self, words):
        return [f"tok_{w}" for w in words]


class _FakeAligner:
    def __call__(self, emission, tokens):
        n = len(tokens)
        step = 100 // n
        spans = []
        for i in range(n):
            start = i * step
            end = start + step - 1
            spans.append([_FakeSpan(start, end)])
        return spans


class _FakeBundle:
    sample_rate = 16000

    def get_model(self):
        return _FakeModel()

    def get_tokenizer(self):
        return _FakeTokenizer()

    def get_aligner(self):
        return _FakeAligner()


def test_align_words_returns_ordered_timestamps(tmp_path):
    wav_path = tmp_path / "vocals.wav"
    waveform = torch.zeros(1, 16000 * 4)
    torchaudio.save(str(wav_path), waveform, 16000)

    results = align_words(wav_path, ["hello", "there", "friend"], bundle=_FakeBundle())

    assert len(results) == 3
    for start, end in results:
        assert 0.0 <= start <= end <= 4.0
    for (_, prev_end), (next_start, _) in zip(results, results[1:]):
        assert next_start >= prev_end - 1e-6


def test_normalize_word_for_alignment_lowercases_and_strips_punctuation():
    assert _normalize_word_for_alignment("Heaven") == "heaven"
    assert _normalize_word_for_alignment("tell,") == "tell"
    assert _normalize_word_for_alignment("don't") == "don't"
    # hyphen is stripped, not kept: it's literally the model's CTC blank-token index
    assert _normalize_word_for_alignment("well-known") == "wellknown"


def test_normalize_word_for_alignment_raises_on_no_alignable_characters():
    with pytest.raises(AlignmentError):
        _normalize_word_for_alignment("123!?")


def test_align_words_handles_mixed_case_and_punctuation(tmp_path):
    wav_path = tmp_path / "vocals.wav"
    waveform = torch.zeros(1, 16000 * 4)
    torchaudio.save(str(wav_path), waveform, 16000)

    # would previously KeyError on 'H' before normalization was added
    results = align_words(wav_path, ["Heaven", "from", "Hell,"], bundle=_FakeBundle())

    assert len(results) == 3


def test_align_words_empty_raises():
    with pytest.raises(AlignmentError):
        align_words(Path("unused.wav"), [], bundle=_FakeBundle())


def test_align_words_span_count_mismatch_raises(tmp_path):
    wav_path = tmp_path / "vocals.wav"
    torchaudio.save(str(wav_path), torch.zeros(1, 16000), 16000)

    class _BadAligner(_FakeAligner):
        def __call__(self, emission, tokens):
            return super().__call__(emission, tokens)[:-1]

    class _BadBundle(_FakeBundle):
        def get_aligner(self):
            return _BadAligner()

    with pytest.raises(AlignmentError):
        align_words(wav_path, ["a", "b", "c"], bundle=_BadBundle())
