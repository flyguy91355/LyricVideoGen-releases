from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import torch
import torchaudio

from .anchors import Block


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


@dataclass
class PreparedAlignment:
    """The expensive part of alignment, done ONCE per song: the model's per-frame output for the whole vocal
    stem. Both the whole-song aligner and the windowed (anchored) aligner read slices of it."""
    bundle: object
    emission: object            # (1, frames, classes)
    num_frames: int
    seconds_per_frame: float
    audio_seconds: float


_FRAME_HOP = 320                 # samples per model frame (20 ms at 16 kHz); wav2vec2's receptive field is 400 samples


def _emission_in_pieces(model, waveform, piece_seconds: float = 75.0, pad_seconds: float = 4.0, sample_rate: int = 16000):
    """The model's frame-by-frame output for `waveform` (1, samples), computed one piece at a time.

    Feeding a whole 7-9 minute song through wav2vec2 at once needed over 10 GB of memory (the owner's redo of 'Tuesday's
    Gone' was killed by earlyoom at 10.3 GB; 20+ minute tracks always died). Each piece is run with `pad_seconds` of extra
    audio either side so its edge frames have context, the padding's frames are dropped, and the pieces are joined. Piece
    edges are multiples of the frame hop, so frame k of the result is frame k of a single pass: the count and the timing
    are unchanged. Measured on real audio (Night Moves, 150 s): with 75 s pieces and 4 s padding no word's timing moved by
    more than 0.26 s and 94% moved by under 40 ms; 30 s pieces moved a hummed 'Mm-mm' by 3 s, so keep pieces long. A song no
    longer than one padded piece is simply run in one pass, as before."""
    samples = waveform.shape[1]
    piece = int(piece_seconds * sample_rate) // _FRAME_HOP * _FRAME_HOP
    pad = int(pad_seconds * sample_rate) // _FRAME_HOP * _FRAME_HOP
    if samples <= piece + 2 * pad:
        return model(waveform)[0]
    kept = []
    for start in range(0, samples, piece):
        first_sample = max(0, start - pad)
        frames, _ = model(waveform[:, first_sample:min(samples, start + piece + pad)])
        skip = (start - first_sample) // _FRAME_HOP
        kept.append(frames[:, skip:skip + piece // _FRAME_HOP])
    return torch.cat(kept, dim=1)


def prepare_alignment(vocals_wav_path: Path, bundle=None) -> PreparedAlignment:
    bundle = bundle or torchaudio.pipelines.MMS_FA
    model = bundle.get_model()

    waveform, sample_rate = torchaudio.load(str(vocals_wav_path))
    if sample_rate != bundle.sample_rate:
        waveform = torchaudio.functional.resample(waveform, sample_rate, bundle.sample_rate)
    waveform = waveform.mean(dim=0, keepdim=True)

    with torch.inference_mode():
        emission = _emission_in_pieces(model, waveform, sample_rate=bundle.sample_rate)

    num_frames = emission.shape[1]
    seconds_per_frame = waveform.shape[1] / num_frames / bundle.sample_rate
    return PreparedAlignment(bundle, emission, num_frames, seconds_per_frame, waveform.shape[1] / bundle.sample_rate)


def _spans_for(prepared: PreparedAlignment, first_frame: int, last_frame: int, words: list[str]) -> list[tuple[int, int]]:
    """Force-align `words` inside frames [first_frame, last_frame) of the emission. Frame numbers in the result are
    relative to first_frame. Raises AlignmentError when they cannot be placed there (e.g. the window is too short)."""
    tokenizer = prepared.bundle.get_tokenizer()
    aligner = prepared.bundle.get_aligner()
    tokens = tokenizer([_normalize_word_for_alignment(w) for w in words])
    try:
        token_spans = aligner(prepared.emission[0][first_frame:last_frame], tokens)
    except (RuntimeError, ValueError) as e:
        raise AlignmentError(f"cannot fit {len(words)} words in frames {first_frame}-{last_frame}: {e}") from e
    if len(token_spans) != len(words):
        raise AlignmentError(f"aligner returned {len(token_spans)} spans for {len(words)} input words")
    return [(spans[0].start, spans[-1].end) for spans in token_spans]


def align_words(vocals_wav_path: Path, words: list[str], bundle=None, prepared: PreparedAlignment | None = None) -> list[tuple[float, float]]:
    """The whole song as ONE alignment pass (the original behavior; drifts when the audio holds material the
    text lacks -- see align_words_anchored)."""
    if not words:
        raise AlignmentError("no words to align")
    prepared = prepared or prepare_alignment(vocals_wav_path, bundle)
    spf = prepared.seconds_per_frame
    return [(start * spf, end * spf) for start, end in _spans_for(prepared, 0, prepared.num_frames, words)]


@dataclass(frozen=True)
class BlockInfo:
    block: Block
    widened: bool = False       # the first window was too small for the aligner and had to grow
    fell_back: bool = False     # the aligner could not place the words at all; they were spread evenly instead


_WIDEN_FACTORS = (1.0, 2.0, 4.0, 8.0)   # extra window, each side, as a multiple of the block's own width


def align_blocks(
    blocks: list[Block], words_per_block: list[int], total_frames: int, seconds_per_frame: float, align_window,
) -> tuple[list[tuple[float, float]], list[BlockInfo]]:
    """Align each block's words inside its own window of the song, so a wrong guess cannot drift more than the
    window allows. `align_window(block_index, first_frame, last_frame, n_words)` returns [(start, end)] frames relative to
    first_frame (raising AlignmentError when it cannot). A rejected window is widened up to three times, then the
    words are spread evenly across the block's ORIGINAL window. Results are made monotonic across blocks."""
    times: list[tuple[float, float]] = []
    infos: list[BlockInfo] = []
    for index, (block, n_words) in enumerate(zip(blocks, words_per_block)):
        if n_words <= 0:
            continue
        width = max(block.end - block.start, seconds_per_frame)
        placed = None
        widened = False
        for attempt, factor in enumerate(_WIDEN_FACTORS):
            start = max(0.0, block.start - width * (factor - 1.0 if attempt else 0.0))
            end = min(total_frames * seconds_per_frame, block.end + width * (factor - 1.0 if attempt else 0.0))
            f0, f1 = int(start / seconds_per_frame), min(total_frames, math.ceil(end / seconds_per_frame))
            if f1 <= f0:
                continue
            try:
                spans = align_window(index, f0, f1, n_words)
            except AlignmentError:
                widened = True
                continue
            placed = [((f0 + s) * seconds_per_frame, (f0 + e) * seconds_per_frame) for s, e in spans]
            break
        fell_back = placed is None
        if fell_back:
            step = (block.end - block.start) / n_words
            placed = [(block.start + k * step, block.start + k * step + step * 0.8) for k in range(n_words)]
        times.extend(placed)
        infos.append(BlockInfo(block, widened=widened, fell_back=fell_back))
    return _monotonic(times), infos


def _monotonic(times: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Words never start before the previous one ended (overlapping windows can otherwise cross)."""
    out: list[tuple[float, float]] = []
    previous_end = 0.0
    for start, end in times:
        start = max(start, previous_end)
        end = max(end, start + 0.02)
        out.append((start, end))
        previous_end = end
    return out


def align_words_anchored(
    vocals_wav_path: Path, lines_words: list[list[str]], anchors: dict, bundle=None, pad: float = 3.0,
    prepared: PreparedAlignment | None = None,
) -> tuple[list[tuple[float, float]], list[BlockInfo]]:
    """Per-word (start, end) for every word of every line, aligned one bounded window at a time: `anchors`
    (anchors.line_anchors, from Whisper's word times) say where each line roughly is, and each line's words are
    only allowed inside its anchor +/- `pad` seconds, so the alignment cannot drift the way one whole-song pass
    does when the audio holds repeats or ad-libs the text lacks. Lines Whisper did not hear share the gap between
    their neighbours' anchors. Returns the flat word times (line order) and one BlockInfo per block."""
    from .anchors import plan_windows

    if not any(lines_words):
        raise AlignmentError("no words to align")
    prepared = prepared or prepare_alignment(vocals_wav_path, bundle)
    blocks = plan_windows(len(lines_words), anchors, prepared.audio_seconds, pad)
    block_words = [[w for i in range(b.first, b.last + 1) for w in lines_words[i]] for b in blocks]

    def align_window(index, first_frame, last_frame, n_words):
        return _spans_for(prepared, first_frame, last_frame, block_words[index])

    return align_blocks(
        blocks, [len(w) for w in block_words], prepared.num_frames, prepared.seconds_per_frame, align_window,
    )


def vocal_loudness(vocals_wav_path: Path, hop: float = 0.5) -> list[float]:
    """RMS level of the vocal track for every `hop` seconds (0.0 = silence)."""
    waveform, sample_rate = torchaudio.load(str(vocals_wav_path))
    mono = waveform.mean(dim=0)
    step = max(1, int(hop * sample_rate))
    return [float(mono[i:i + step].pow(2).mean().sqrt()) for i in range(0, mono.shape[0], step)]
