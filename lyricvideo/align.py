from __future__ import annotations

import re
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

# MMS_FA's own "star" token (bundle.get_dict() includes it by default, and
# get_model(with_star=True) -- also the default -- gives the emission a matching
# extra class): a wildcard that absorbs whatever audio sits at that position.
# The documented escape hatch for a word the alphabet can't spell.
_STAR_TOKEN = "*"

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]
_ORDINAL_IRREGULAR = {
    "one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
    "nine": "ninth", "twelve": "twelfth",
}
_DIGIT_RUN = re.compile(r"\d+")
_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d)")
_DIGIT_HYPHEN = re.compile(r"(?<=\d)-(?=\d)")
_ORDINAL_SUFFIX = re.compile(r"^(\d+)(st|nd|rd|th)$")


def _cardinal(n: int) -> str:
    """0 <= n < 10**12 in English words ('three hundred forty two')."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {_cardinal(rest)}" if rest else "")
    for scale, name in _SCALES:
        if n >= scale:
            major, rest = divmod(n, scale)
            return f"{_cardinal(major)} {name}" + (f" {_cardinal(rest)}" if rest else "")
    raise ValueError(n)  # pragma: no cover -- callers cap the digit-run length below


def _spoken_number(digits: str) -> str:
    """A run of digits the way a singer says it: '31' -> 'thirty one', '1975' ->
    'nineteen seventy five' (a 4-digit run in the year range reads as a year,
    since that's overwhelmingly what a lyric means by one), '2020' -> 'twenty
    twenty'; a run with a leading zero or longer than four digits is read
    digit by digit ('867 5309' -> 'eight six seven five three oh nine'), the
    way phone numbers and codes are sung."""
    if len(digits) > 4 or (len(digits) > 1 and digits[0] == "0"):
        return " ".join("oh" if d == "0" else _ONES[int(d)] for d in digits)
    n = int(digits)
    if len(digits) == 4 and 1100 <= n <= 1999:
        century, rest = divmod(n, 100)
        if rest == 0:
            return f"{_cardinal(century)} hundred"
        return f"{_cardinal(century)} {'oh ' if rest < 10 else ''}{_cardinal(rest)}"
    if len(digits) == 4 and 2010 <= n <= 2099:
        return f"twenty {_cardinal(n - 2000)}"
    return _cardinal(n)


def _spoken_ordinal(digits: str) -> str:
    words = _spoken_number(digits).split()
    last = words[-1]
    if last in _ORDINAL_IRREGULAR:
        words[-1] = _ORDINAL_IRREGULAR[last]
    elif last.endswith("y"):
        words[-1] = last[:-1] + "ieth"
    else:
        words[-1] = last + "th"
    return " ".join(words)


def _normalize_word_for_alignment(word: str) -> str:
    """The alignment-only spelling of one lyric word -- never what's displayed.
    MMS_FA knows a-z and the apostrophe, nothing else, so: digit runs are
    spelled out as sung ('31' -> 'thirtyone', '1st' -> 'first', '2nite' ->
    'twonite'; spaces dropped so the run stays ONE aligner word), '&' reads
    as 'and', everything else outside the alphabet is stripped, and a word
    with nothing left (a bare '...', a dash, an emoji) becomes the model's
    star token rather than an error. Real incident (GitHub issue #3,
    2026-09-14): a lyric line containing the word '31' raised
    AlignmentError here, aborting the whole align stage -- and since Redo
    re-fetches the same lyrics, that song could never be processed at all."""
    lowered = word.lower().replace("&", " and ")
    lowered = _THOUSANDS_COMMA.sub("", lowered)   # "1,000" -> "1000"
    lowered = _DIGIT_HYPHEN.sub("", lowered)      # "867-5309" -> one 7-digit run, read digit by digit
    ordinal = _ORDINAL_SUFFIX.match(lowered.strip())
    if ordinal:
        lowered = _spoken_ordinal(ordinal.group(1))
    else:
        lowered = _DIGIT_RUN.sub(lambda m: f" {_spoken_number(m.group())} ", lowered)
    normalized = "".join(c for c in lowered if c in _ALIGNMENT_ALPHABET)
    return normalized or _STAR_TOKEN


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
