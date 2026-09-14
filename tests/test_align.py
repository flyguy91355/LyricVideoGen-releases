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


def test_normalize_word_for_alignment_spells_numbers_out_as_sung():
    """GitHub issue #3: the lyric word '31' raised AlignmentError and aborted
    the whole align stage. Digits are now spelled out the way they're sung,
    joined into ONE aligner word (a space would split it into two spans)."""
    assert _normalize_word_for_alignment("31") == "thirtyone"
    assert _normalize_word_for_alignment("123!?") == "onehundredtwentythree"
    assert _normalize_word_for_alignment("7") == "seven"
    assert _normalize_word_for_alignment("2nite") == "twonite"
    assert _normalize_word_for_alignment("$100") == "onehundred"
    assert _normalize_word_for_alignment("1,000") == "onethousand"


def test_normalize_word_for_alignment_reads_years_and_ordinals_as_sung():
    assert _normalize_word_for_alignment("1975") == "nineteenseventyfive"
    assert _normalize_word_for_alignment("1900") == "nineteenhundred"
    assert _normalize_word_for_alignment("1905") == "nineteenohfive"
    assert _normalize_word_for_alignment("2020") == "twentytwenty"
    assert _normalize_word_for_alignment("2005") == "twothousandfive"
    assert _normalize_word_for_alignment("1st") == "first"
    assert _normalize_word_for_alignment("2nd") == "second"
    assert _normalize_word_for_alignment("31st") == "thirtyfirst"
    assert _normalize_word_for_alignment("20th") == "twentieth"
    assert _normalize_word_for_alignment("12th") == "twelfth"


def test_normalize_word_for_alignment_reads_long_digit_runs_digit_by_digit():
    assert _normalize_word_for_alignment("867-5309") == "eightsixseven" + "fivethreeohnine"
    assert _normalize_word_for_alignment("007") == "ohohseven"


def test_normalize_word_for_alignment_ampersand_reads_as_and():
    assert _normalize_word_for_alignment("&") == "and"
    assert _normalize_word_for_alignment("rock&roll") == "rockandroll"


def test_normalize_word_for_alignment_uses_the_star_token_instead_of_raising():
    """A word with nothing the model can spell (a bare ellipsis, a dash, an
    emoji) must never abort the stage -- MMS_FA's own '*' wildcard token
    absorbs whatever audio sits there."""
    assert _normalize_word_for_alignment("...") == "*"
    assert _normalize_word_for_alignment("--") == "*"
    assert _normalize_word_for_alignment("\U0001F3B8") == "*"


def test_every_normalized_spelling_is_accepted_by_the_real_mms_fa_tokenizer():
    """The exact failure class behind issue #3 is a character the model's
    dictionary doesn't have. bundle.get_tokenizer() needs no model download
    -- it's just the dictionary -- so check every kind of output the
    normalizer can produce against the real one, star token included."""
    tokenizer = torchaudio.pipelines.MMS_FA.get_tokenizer()
    words = ["Heaven", "don't", "31", "1975", "31st", "&", "...", "867-5309", "2nite"]

    tokens = tokenizer([_normalize_word_for_alignment(w) for w in words])

    assert len(tokens) == len(words)
    assert all(len(t) > 0 for t in tokens)


def test_align_words_accepts_numeric_and_symbol_only_words(tmp_path):
    wav_path = tmp_path / "vocals.wav"
    torchaudio.save(str(wav_path), torch.zeros(1, 16000 * 4), 16000)

    results = align_words(wav_path, ["I", "was", "31", "&", "..."], bundle=_FakeBundle())

    assert len(results) == 5


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
