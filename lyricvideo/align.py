from __future__ import annotations

from pathlib import Path

import torch
import torchaudio


class AlignmentError(Exception):
    pass


# MMS_FA's tokenizer dictionary contains lowercase a-z plus ' - * , but '-' is
# literally the model's CTC blank-token index (0) -- passing it through as a real
# target character makes forced_align reject the whole sequence ("targets Tensor
# shouldn't contain blank index"). Real lyric words (mixed case, commas/periods/
# hyphens from OCR or PDF extraction) are normalized down to just letters + '
# before tokenizing, or it KeyErrors on the first unrecognized character.
_ALIGNMENT_ALPHABET = set("abcdefghijklmnopqrstuvwxyz'")


def _normalize_word_for_alignment(word: str) -> str:
    normalized = "".join(c for c in word.lower() if c in _ALIGNMENT_ALPHABET)
    if not normalized:
        raise AlignmentError(f"word {word!r} has no alignable characters after normalization")
    return normalized


def align_words(vocals_wav_path: Path, words: list[str], bundle=None) -> list[tuple[float, float]]:
    if not words:
        raise AlignmentError("no words to align")

    bundle = bundle or torchaudio.pipelines.MMS_FA
    model = bundle.get_model()
    tokenizer = bundle.get_tokenizer()
    aligner = bundle.get_aligner()

    waveform, sample_rate = torchaudio.load(str(vocals_wav_path))
    if sample_rate != bundle.sample_rate:
        waveform = torchaudio.functional.resample(waveform, sample_rate, bundle.sample_rate)
    waveform = waveform.mean(dim=0, keepdim=True)

    with torch.inference_mode():
        emission, _ = model(waveform)

    normalized_words = [_normalize_word_for_alignment(w) for w in words]
    tokens = tokenizer(normalized_words)
    token_spans = aligner(emission[0], tokens)

    if len(token_spans) != len(words):
        raise AlignmentError(
            f"aligner returned {len(token_spans)} spans for {len(words)} input words"
        )

    num_frames = emission.shape[1]
    seconds_per_frame = waveform.shape[1] / num_frames / bundle.sample_rate

    results = []
    for spans in token_spans:
        start = spans[0].start * seconds_per_frame
        end = spans[-1].end * seconds_per_frame
        results.append((start, end))
    return results
