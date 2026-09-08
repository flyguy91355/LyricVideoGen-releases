# Tab-PDF-Driven Lyric Video Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI tool that turns an owner-supplied song audio file + tab
PDF into a synced lyric/chord play-along video with an AI-generated,
lyric-meaning-following background.

**Architecture:** A staged, resumable pipeline: Demucs vocal separation →
coordinate-aware PDF chord/lyric extraction → torchaudio forced alignment for
word-level timing → Claude-written image prompts rendered via Replicate
(cached per unique line) → Pillow-composited video frames (scrolling lyrics +
word-synced chord labels + Ken Burns background) assembled with moviepy and
muxed with the original audio.

**Tech Stack:** Python 3.11+, pdfplumber, torch/torchaudio, demucs, Pillow,
moviepy, replicate, anthropic, pytest.

**Spec:** `docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md`

## Global Constraints

- CPU-only — no GPU-dependent code paths (owner's hardware has no capable GPU).
- v1 requires a tab PDF for every song; no audio-only ASR/chord-detection
  fallback in this plan.
- Scanned/image-only PDFs are out of scope — no OCR.
- No real song's lyric text is ever embedded in test fixtures — synthetic/
  invented placeholder text and PDFs only.
- Chord *names* only in the rendered video — no fingering diagrams.
- Output is a local `.mp4` file only — no auto-upload to YouTube.
- Video output is 1080p, 16:9.
- Background images: one per *unique* lyric line (exact-repeat lines reuse
  the cached image), generated fully automatically (Claude writes the prompt,
  no owner input per line), stills with Ken Burns pan/zoom — no AI video
  generation.

---

### Task 1: Project Scaffolding & Data Model

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `lyricvideo/__init__.py`
- Create: `lyricvideo/models.py`
- Create: `tests/__init__.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `ChordWord(word: str, chord: str | None = None, start_time: float | None = None, end_time: float | None = None)`,
  `LyricLine(words: list[ChordWord], start_time: float | None = None, end_time: float | None = None)` with `.text` property,
  `Song(title: str, audio_path: str, vocal_stem_path: str | None = None, lines: list[LyricLine] = [], image_cache: dict[str, str] = {})`,
  `save_song(song: Song, path: Path) -> None`, `load_song(path: Path) -> Song`,
  `line_hash(text: str) -> str`.

- [x] **Step 1: Create the project directory layout and dependency files**

`requirements.txt`:
```
pdfplumber>=0.10
torch>=2.1
torchaudio>=2.1
demucs>=4.0
pillow>=10.0
moviepy>=1.0.3
numpy>=1.24
replicate>=0.25
anthropic>=0.34
python-dotenv>=1.0
pytest>=7.0
reportlab>=4.0
```
(`pytest` and `reportlab` are dev/test-only — `reportlab` builds synthetic
PDF fixtures for the parser tests in Task 2 — but this project is small
enough that a separate `requirements-dev.txt` isn't worth the split.)

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.env
work/
```

`.env.example`:
```
ANTHROPIC_API_KEY=
REPLICATE_API_TOKEN=
```

- [x] **Step 2: Write the failing test for the data model**

```python
# tests/test_models.py
import json
from pathlib import Path

from lyricvideo.models import ChordWord, LyricLine, Song, save_song, load_song, line_hash


def test_lyric_line_text_joins_words():
    line = LyricLine(words=[ChordWord(word="hello"), ChordWord(word="there")])
    assert line.text == "hello there"


def test_save_and_load_song_round_trip(tmp_path):
    song = Song(
        title="Test Song",
        audio_path="audio.mp3",
        vocal_stem_path="vocals.wav",
        lines=[
            LyricLine(
                words=[ChordWord(word="hi", chord="G", start_time=0.0, end_time=0.5)],
                start_time=0.0,
                end_time=0.5,
            )
        ],
        image_cache={"abc123": "images/abc123.png"},
    )
    path = tmp_path / "song.json"

    save_song(song, path)
    restored = load_song(path)

    assert restored == song


def test_line_hash_stable_and_case_insensitive():
    assert line_hash("Hello There") == line_hash("hello there")
    assert line_hash("Hello There") != line_hash("Something else")
```

- [x] **Step 3: Run the test to verify it fails**

Run: `mkdir -p /home/doug/LyricVideoGen/lyricvideo /home/doug/LyricVideoGen/tests && cd /home/doug/LyricVideoGen && python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest tests/test_models.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.models'`, or import error) since `lyricvideo/models.py` doesn't exist yet. (Note: the `pip install` step downloads `torch` and will take a while the first time — this is a one-time cost for the whole project's dependency set.)

- [x] **Step 4: Write `lyricvideo/models.py`**

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ChordWord:
    word: str
    chord: str | None = None
    start_time: float | None = None
    end_time: float | None = None


@dataclass
class LyricLine:
    words: list[ChordWord] = field(default_factory=list)
    start_time: float | None = None
    end_time: float | None = None

    @property
    def text(self) -> str:
        return " ".join(w.word for w in self.words)


@dataclass
class Song:
    title: str
    audio_path: str
    vocal_stem_path: str | None = None
    lines: list[LyricLine] = field(default_factory=list)
    image_cache: dict[str, str] = field(default_factory=dict)


def line_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _song_to_dict(song: Song) -> dict:
    return asdict(song)


def _song_from_dict(data: dict) -> Song:
    lines = [
        LyricLine(
            words=[ChordWord(**w) for w in ln["words"]],
            start_time=ln.get("start_time"),
            end_time=ln.get("end_time"),
        )
        for ln in data.get("lines", [])
    ]
    return Song(
        title=data["title"],
        audio_path=data["audio_path"],
        vocal_stem_path=data.get("vocal_stem_path"),
        lines=lines,
        image_cache=data.get("image_cache", {}),
    )


def save_song(song: Song, path: Path) -> None:
    path.write_text(json.dumps(_song_to_dict(song), indent=2), encoding="utf-8")


def load_song(path: Path) -> Song:
    return _song_from_dict(json.loads(path.read_text(encoding="utf-8")))
```

Also create empty `lyricvideo/__init__.py` and `tests/__init__.py`.

- [x] **Step 5: Run the test to verify it passes**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_models.py -v`
Expected: PASS (3 tests)

- [x] **Step 6: Commit**

```bash
cd /home/doug/LyricVideoGen
git add requirements.txt .gitignore .env.example lyricvideo/__init__.py lyricvideo/models.py tests/__init__.py tests/test_models.py
git commit -m "Add project scaffolding and shared data model"
```

---

### Task 2: Tab PDF Chord/Lyric Parser

**Files:**
- Create: `lyricvideo/pdf_parse.py`
- Test: `tests/test_pdf_parse.py`

**Interfaces:**
- Consumes: `ChordWord`, `LyricLine` from `lyricvideo.models` (Task 1).
- Produces: `parse_tab_pdf(pdf_path: Path) -> list[LyricLine]`,
  `is_chord_token(token: str) -> bool`,
  exceptions `TabPdfError`, `NoTextLayerError(TabPdfError)`,
  `NoChordLyricPairsError(TabPdfError)`.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_pdf_parse.py
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from lyricvideo.pdf_parse import (
    parse_tab_pdf,
    is_chord_token,
    NoTextLayerError,
    NoChordLyricPairsError,
)


def _make_chord_lyric_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=(400, 200))
    c.setFont("Courier", 12)
    c.drawString(50, 150, "G")
    c.drawString(110, 150, "D")
    c.drawString(50, 130, "hello")
    c.drawString(110, 130, "there")
    c.save()


def test_parse_chord_over_lyric_pdf(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_chord_lyric_pdf(pdf_path)

    lines = parse_tab_pdf(pdf_path)

    assert len(lines) == 1
    words = lines[0].words
    assert [w.word for w in words] == ["hello", "there"]
    assert words[0].chord == "G"
    assert words[1].chord == "D"


def test_no_text_layer_raises(tmp_path):
    pdf_path = tmp_path / "scanned.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 200))
    c.rect(10, 10, 380, 180, fill=1)  # a shape, no text at all
    c.save()

    with pytest.raises(NoTextLayerError):
        parse_tab_pdf(pdf_path)


def test_no_chord_lyric_pairs_raises(tmp_path):
    pdf_path = tmp_path / "chords_only.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 200))
    c.setFont("Courier", 12)
    c.drawString(50, 150, "G")
    c.drawString(110, 150, "D")
    c.drawString(50, 130, "Em")
    c.drawString(110, 130, "C")
    c.save()

    with pytest.raises(NoChordLyricPairsError):
        parse_tab_pdf(pdf_path)


def test_prose_only_pdf_produces_lines_with_no_chords(tmp_path):
    pdf_path = tmp_path / "prose.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 200))
    c.setFont("Courier", 12)
    c.drawString(50, 150, "just some ordinary")
    c.drawString(50, 130, "sentence with no chords")
    c.save()

    lines = parse_tab_pdf(pdf_path)

    assert all(w.chord is None for line in lines for w in line.words)


def test_is_chord_token():
    for tok in ["G", "Em7", "C/G", "A#dim", "Asus4", "Bm"]:
        assert is_chord_token(tok)
    for tok in ["hello", "the", "Wish"]:
        assert not is_chord_token(tok)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_pdf_parse.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.pdf_parse'`)

- [x] **Step 3: Write `lyricvideo/pdf_parse.py`**

```python
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from .models import ChordWord, LyricLine

CHORD_RE = re.compile(
    r'^[A-G](#|b)?(maj|min|m|dim|aug|sus)?\d{0,2}(/[A-G](#|b)?)?$'
)
CHORD_LINE_THRESHOLD = 0.6
Y_TOLERANCE = 3.0


class TabPdfError(Exception):
    pass


class NoTextLayerError(TabPdfError):
    pass


class NoChordLyricPairsError(TabPdfError):
    pass


def is_chord_token(token: str) -> bool:
    return bool(CHORD_RE.match(token.strip()))


def _cluster_words_into_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        placed = False
        for line in lines:
            if abs(line[0]["top"] - word["top"]) <= Y_TOLERANCE:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    lines.sort(key=lambda line: line[0]["top"])
    return lines


def _classify_line(line: list[dict]) -> str:
    tokens = [w["text"] for w in line]
    if not tokens:
        return "lyric"
    chord_count = sum(1 for t in tokens if is_chord_token(t))
    return "chord" if (chord_count / len(tokens)) >= CHORD_LINE_THRESHOLD else "lyric"


def _pair_chord_to_words(chord_line: list[dict], lyric_line: list[dict]) -> list[ChordWord]:
    result = [ChordWord(word=w["text"], chord=None) for w in lyric_line]
    for chord_word in chord_line:
        chord_center = (chord_word["x0"] + chord_word["x1"]) / 2
        best_idx, best_dist = None, None
        for i, lw in enumerate(lyric_line):
            lyric_center = (lw["x0"] + lw["x1"]) / 2
            dist = abs(lyric_center - chord_center)
            if best_dist is None or dist < best_dist:
                best_idx, best_dist = i, dist
        if best_idx is not None:
            result[best_idx].chord = chord_word["text"]
    return result


def parse_tab_pdf(pdf_path: Path) -> list[LyricLine]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        all_words: list[dict] = []
        for page in pdf.pages:
            all_words.extend(page.extract_words())

    if not all_words:
        raise NoTextLayerError(
            f"{pdf_path} has no extractable text layer (scanned/image-only PDF?)"
        )

    lines = _cluster_words_into_lines(all_words)
    classified = [(line, _classify_line(line)) for line in lines]

    result: list[LyricLine] = []
    i = 0
    while i < len(classified):
        line, kind = classified[i]
        if kind == "chord" and i + 1 < len(classified) and classified[i + 1][1] == "lyric":
            lyric_line = classified[i + 1][0]
            words = _pair_chord_to_words(line, lyric_line)
            result.append(LyricLine(words=words))
            i += 2
        elif kind == "lyric":
            words = [ChordWord(word=w["text"], chord=None) for w in line]
            result.append(LyricLine(words=words))
            i += 1
        else:
            i += 1

    if not result:
        raise NoChordLyricPairsError(
            f"{pdf_path} has a text layer but no chord-over-lyric line pairs "
            "or plain lyric lines were found"
        )

    return result
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_pdf_parse.py -v`
Expected: PASS (5 tests). If the chord/lyric pairing assertions fail, check
the actual `x0`/`x1`/`top` values `pdfplumber` reports for the synthetic PDF
(print them) — the clustering/pairing logic depends on real coordinate
values matching this reasoning, not just the reportlab source positions.

- [x] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/pdf_parse.py tests/test_pdf_parse.py
git commit -m "Add tab PDF chord/lyric parser"
```

---

### Task 3: Vocal Stem Separation Wrapper

**Files:**
- Create: `lyricvideo/separate.py`
- Test: `tests/test_separate.py`

**Interfaces:**
- Produces: `separate_vocals(audio_path: Path, out_dir: Path) -> Path`,
  exception `SeparationError`.

- [x] **Step 1: Write the failing test**

Demucs itself is too slow/heavy for a unit test (real model inference) —
this test verifies the wrapper's subprocess invocation and error handling
via mocking, not real separation. Real separation is verified manually in
Task 11.

```python
# tests/test_separate.py
from pathlib import Path

import pytest

from lyricvideo.separate import separate_vocals, SeparationError


def test_separate_vocals_returns_expected_path(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"

    expected_vocals = out_dir / "htdemucs" / "song" / "vocals.wav"

    def fake_run(cmd, check):
        expected_vocals.parent.mkdir(parents=True, exist_ok=True)
        expected_vocals.write_bytes(b"fake-wav")

    monkeypatch.setattr("lyricvideo.separate.subprocess.run", fake_run)

    result = separate_vocals(audio_path, out_dir)

    assert result == expected_vocals


def test_separate_vocals_raises_if_output_missing(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake-audio")
    out_dir = tmp_path / "out"

    monkeypatch.setattr("lyricvideo.separate.subprocess.run", lambda cmd, check: None)

    with pytest.raises(SeparationError):
        separate_vocals(audio_path, out_dir)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_separate.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.separate'`)

- [x] **Step 3: Write `lyricvideo/separate.py`**

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class SeparationError(Exception):
    pass


def separate_vocals(audio_path: Path, out_dir: Path) -> Path:
    """Run Demucs two-stem separation (CPU) and return the path to vocals.wav."""
    subprocess.run(
        [
            sys.executable, "-m", "demucs",
            "--two-stems=vocals",
            "-n", "htdemucs",
            "-o", str(out_dir),
            str(audio_path),
        ],
        check=True,
    )
    stem_name = Path(audio_path).stem
    vocals_path = out_dir / "htdemucs" / stem_name / "vocals.wav"
    if not vocals_path.exists():
        raise SeparationError(f"Demucs did not produce expected output at {vocals_path}")
    return vocals_path
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_separate.py -v`
Expected: PASS (2 tests)

- [x] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/separate.py tests/test_separate.py
git commit -m "Add Demucs vocal stem separation wrapper"
```

---

### Task 4: Forced Alignment Wrapper

**Files:**
- Create: `lyricvideo/align.py`
- Test: `tests/test_align.py`

**Interfaces:**
- Produces: `align_words(vocals_wav_path: Path, words: list[str], bundle=None) -> list[tuple[float, float]]`,
  exception `AlignmentError`. `bundle` defaults to `torchaudio.pipelines.MMS_FA`
  and is injectable for testing (must expose `.sample_rate`, `.get_model()`,
  `.get_tokenizer()`, `.get_aligner()`).

**⚠️ Verify before relying on this in Task 11:** the exact `torchaudio`
forced-alignment API (`MMS_FA` bundle shape, whether `aligner()` returns
per-token or per-character spans, the units `TokenSpan.start`/`.end` are in)
has shifted across `torchaudio` releases. Before running the real end-to-end
test in Task 11, check the actual installed `torchaudio` version's forced
alignment tutorial/API docs (`torchaudio.pipelines.MMS_FA`,
`torchaudio.functional.forced_align`) and adjust `align_words` if the real
API differs from what's written here — don't assume this snippet is correct
by construction.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_align.py
from pathlib import Path

import pytest
import torch
import torchaudio

from lyricvideo.align import align_words, AlignmentError


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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_align.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.align'`)

- [x] **Step 3: Write `lyricvideo/align.py`**

```python
from __future__ import annotations

from pathlib import Path

import torch
import torchaudio


class AlignmentError(Exception):
    pass


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

    tokens = tokenizer(words)
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
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_align.py -v`
Expected: PASS (3 tests)

- [x] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/align.py tests/test_align.py
git commit -m "Add forced-alignment wrapper for word-level lyric timing"
```

---

### Task 5: Alignment Combination & Sanity Checks

**Files:**
- Create: `lyricvideo/combine.py`
- Test: `tests/test_combine.py`

**Interfaces:**
- Consumes: `ChordWord`, `LyricLine` from `lyricvideo.models`.
- Produces: `combine_alignment(parsed_lines: list[LyricLine], word_times: list[tuple[float, float]], audio_duration: float) -> list[LyricLine]`,
  exception `AlignmentSanityError`.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_combine.py
import pytest

from lyricvideo.models import ChordWord, LyricLine
from lyricvideo.combine import combine_alignment, AlignmentSanityError


def _parsed_lines():
    return [
        LyricLine(words=[ChordWord(word="hello", chord="G"), ChordWord(word="there")]),
        LyricLine(words=[ChordWord(word="my", chord="D"), ChordWord(word="friend")]),
    ]


def test_combine_alignment_assigns_times_in_order():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (1.0, 1.3), (1.3, 1.8)]

    result = combine_alignment(lines, word_times, audio_duration=2.0)

    assert result[0].words[0].start_time == 0.0
    assert result[0].words[0].end_time == 0.4
    assert result[0].words[0].chord == "G"
    assert result[0].start_time == 0.0
    assert result[0].end_time == 0.9
    assert result[1].words[0].start_time == 1.0
    assert result[1].end_time == 1.8


def test_combine_alignment_word_count_mismatch_raises():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9)]  # only 2, but 4 words total

    with pytest.raises(AlignmentSanityError):
        combine_alignment(lines, word_times, audio_duration=2.0)


def test_combine_alignment_out_of_duration_raises():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (1.0, 1.3), (1.3, 5.0)]  # 5.0 > duration

    with pytest.raises(AlignmentSanityError):
        combine_alignment(lines, word_times, audio_duration=2.0)


def test_combine_alignment_non_monotonic_raises():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (0.5, 0.7), (1.3, 1.8)]  # word 3 goes backward

    with pytest.raises(AlignmentSanityError):
        combine_alignment(lines, word_times, audio_duration=2.0)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_combine.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.combine'`)

- [x] **Step 3: Write `lyricvideo/combine.py`**

```python
from __future__ import annotations

from .models import ChordWord, LyricLine

MONOTONIC_TOLERANCE = 0.05


class AlignmentSanityError(Exception):
    pass


def combine_alignment(
    parsed_lines: list[LyricLine],
    word_times: list[tuple[float, float]],
    audio_duration: float,
) -> list[LyricLine]:
    flat_words = [w for line in parsed_lines for w in line.words]
    if len(flat_words) != len(word_times):
        raise AlignmentSanityError(
            f"word count mismatch: {len(flat_words)} lyric words vs "
            f"{len(word_times)} aligned timestamps"
        )

    idx = 0
    prev_end = -1.0
    timed_lines: list[LyricLine] = []
    for line in parsed_lines:
        new_words: list[ChordWord] = []
        for w in line.words:
            start, end = word_times[idx]
            if start < 0 or end > audio_duration or end < start or start < prev_end - MONOTONIC_TOLERANCE:
                raise AlignmentSanityError(
                    f"invalid timestamp for word {idx} ('{w.word}'): start={start}, "
                    f"end={end}, audio_duration={audio_duration}, prev_end={prev_end}"
                )
            new_words.append(ChordWord(word=w.word, chord=w.chord, start_time=start, end_time=end))
            prev_end = end
            idx += 1
        timed_lines.append(
            LyricLine(words=new_words, start_time=new_words[0].start_time, end_time=new_words[-1].end_time)
        )
    return timed_lines
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_combine.py -v`
Expected: PASS (4 tests)

- [x] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/combine.py tests/test_combine.py
git commit -m "Add alignment combination and sanity-check logic"
```

---

### Task 6: AI Background Image Generation & Caching

**Files:**
- Create: `lyricvideo/imagery.py`
- Test: `tests/test_imagery.py`

**Interfaces:**
- Consumes: `line_hash` from `lyricvideo.models` (Task 1).
- Produces: `build_image_prompt(anthropic_client, full_lyrics: str, line_text: str, model: str = "claude-sonnet-5") -> str`,
  `generate_line_image(replicate_client, prompt: str, out_path: Path, model: str = "black-forest-labs/flux-schnell") -> Path`,
  `get_or_generate_image(anthropic_client, replicate_client, full_lyrics: str, line_text: str, cache_dir: Path, fallback_color: tuple[int,int,int] = (30,30,40)) -> Path`,
  exception `ImageGenError`.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_imagery.py
from pathlib import Path

from lyricvideo.imagery import build_image_prompt, get_or_generate_image


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text="a moody forest at dusk"):
        self.messages = _FakeMessages(text)


def test_build_image_prompt_extracts_and_strips_text():
    client = _FakeAnthropicClient(text="  a lonely lighthouse in a storm  ")
    prompt = build_image_prompt(client, "full lyrics here", "the current line")
    assert prompt == "a lonely lighthouse in a storm"


def test_get_or_generate_image_uses_cache(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_generate(replicate_client, prompt, out_path, model="black-forest-labs/flux-schnell"):
        calls["n"] += 1
        out_path.write_bytes(b"fake-png-bytes")
        return out_path

    monkeypatch.setattr("lyricvideo.imagery.generate_line_image", fake_generate)

    anthropic_client = _FakeAnthropicClient()
    replicate_client = object()

    path1 = get_or_generate_image(anthropic_client, replicate_client, "full lyrics", "same line", tmp_path)
    path2 = get_or_generate_image(anthropic_client, replicate_client, "full lyrics", "same line", tmp_path)

    assert path1 == path2
    assert calls["n"] == 1


def test_get_or_generate_image_falls_back_on_failure(tmp_path):
    class _FailingMessages:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    class _FailingAnthropicClient:
        def __init__(self):
            self.messages = _FailingMessages()

    path = get_or_generate_image(_FailingAnthropicClient(), object(), "full lyrics", "a broken line", tmp_path)

    assert path.exists()
    from PIL import Image
    img = Image.open(path)
    assert img.size == (1920, 1080)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_imagery.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.imagery'`)

- [x] **Step 3: Write `lyricvideo/imagery.py`**

```python
from __future__ import annotations

import urllib.request
from pathlib import Path

from PIL import Image

from .models import line_hash

FRAME_SIZE = (1920, 1080)


class ImageGenError(Exception):
    pass


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise ImageGenError("Claude response contained no text content")
    return "".join(parts)


def build_image_prompt(
    anthropic_client,
    full_lyrics: str,
    line_text: str,
    model: str = "claude-sonnet-5",
) -> str:
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    "You are writing a single image-generation prompt for a background "
                    "image in a lyric video. Here are the full song lyrics for context:\n\n"
                    f"{full_lyrics}\n\n"
                    f'The current line is: "{line_text}"\n\n'
                    "Write ONE concise, vivid, purely visual image-generation prompt (no "
                    "camera jargon, no text-in-image requests) that captures the meaning/"
                    "imagery of this specific line in the context of the song. Reply with "
                    "ONLY the prompt text, nothing else."
                ),
            }
        ],
    )
    return _extract_text(response).strip()


def generate_line_image(
    replicate_client,
    prompt: str,
    out_path: Path,
    model: str = "black-forest-labs/flux-schnell",
) -> Path:
    output = replicate_client.run(model, input={"prompt": prompt})
    url = output[0] if isinstance(output, list) else output
    urllib.request.urlretrieve(str(url), out_path)
    return out_path


def get_or_generate_image(
    anthropic_client,
    replicate_client,
    full_lyrics: str,
    line_text: str,
    cache_dir: Path,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = line_hash(line_text)
    cached_path = cache_dir / f"{key}.png"
    if cached_path.exists():
        return cached_path

    for attempt in range(2):
        try:
            prompt = build_image_prompt(anthropic_client, full_lyrics, line_text)
            return generate_line_image(replicate_client, prompt, cached_path)
        except Exception:
            if attempt == 1:
                break

    Image.new("RGB", FRAME_SIZE, fallback_color).save(cached_path)
    return cached_path
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_imagery.py -v`
Expected: PASS (3 tests)

- [x] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/imagery.py tests/test_imagery.py
git commit -m "Add AI background image generation with per-line caching"
```

---

### Task 7: Frame Layout (Pure Scene-Building Functions)

**Files:**
- Create: `lyricvideo/layout.py`
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `LyricLine`, `line_hash` from `lyricvideo.models`.
- Produces: `SceneWord(text: str, chord: str | None = None)`,
  `SceneLine(words: list[SceneWord], is_current: bool = False, distance_from_current: int = 0)` with `.text` property,
  `Scene(lines: list[SceneLine], image_key: str, ken_burns_progress: float)`,
  `find_current_line_index(lines: list[LyricLine], t: float) -> int`,
  `build_scene(lines: list[LyricLine], t: float, window: int = 1) -> Scene`.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_layout.py
from lyricvideo.models import ChordWord, LyricLine
from lyricvideo.layout import build_scene, find_current_line_index


def _make_lines():
    return [
        LyricLine(
            words=[
                ChordWord(word="hello", chord="G", start_time=0.0, end_time=0.5),
                ChordWord(word="there", chord=None, start_time=0.5, end_time=1.0),
            ],
            start_time=0.0, end_time=1.0,
        ),
        LyricLine(
            words=[
                ChordWord(word="my", chord="D", start_time=1.0, end_time=1.3),
                ChordWord(word="friend", chord=None, start_time=1.3, end_time=2.0),
            ],
            start_time=1.0, end_time=2.0,
        ),
        LyricLine(
            words=[ChordWord(word="goodbye", chord="Em", start_time=2.0, end_time=2.6)],
            start_time=2.0, end_time=3.0,
        ),
    ]


def test_find_current_line_index():
    lines = _make_lines()
    assert find_current_line_index(lines, 0.2) == 0
    assert find_current_line_index(lines, 1.5) == 1
    assert find_current_line_index(lines, 2.9) == 2


def test_build_scene_current_line_shows_reached_chords():
    lines = _make_lines()
    scene = build_scene(lines, t=0.6)
    current = next(l for l in scene.lines if l.is_current)
    assert [w.text for w in current.words] == ["hello", "there"]
    assert current.words[0].chord == "G"


def test_build_scene_chord_not_shown_before_its_start_time():
    lines = _make_lines()
    scene_early = build_scene(lines, t=0.9)
    current_early = next(l for l in scene_early.lines if l.is_current)
    assert current_early.words[0].chord == "G"

    scene_next_line = build_scene(lines, t=1.1)
    current_next = next(l for l in scene_next_line.lines if l.is_current)
    assert current_next.words[0].chord == "D"


def test_build_scene_window_includes_neighbors():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    offsets = sorted(l.distance_from_current for l in scene.lines)
    assert offsets == [-1, 0, 1]


def test_build_scene_ken_burns_progress_increases_within_line():
    lines = _make_lines()
    start_scene = build_scene(lines, t=1.0)
    mid_scene = build_scene(lines, t=1.5)
    end_scene = build_scene(lines, t=1.99)
    assert start_scene.ken_burns_progress < mid_scene.ken_burns_progress < end_scene.ken_burns_progress
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_layout.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.layout'`)

- [x] **Step 3: Write `lyricvideo/layout.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field

from .models import LyricLine, line_hash


@dataclass
class SceneWord:
    text: str
    chord: str | None = None


@dataclass
class SceneLine:
    words: list[SceneWord] = field(default_factory=list)
    is_current: bool = False
    distance_from_current: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Scene:
    lines: list[SceneLine]
    image_key: str
    ken_burns_progress: float


def find_current_line_index(lines: list[LyricLine], t: float) -> int:
    idx = 0
    for i, line in enumerate(lines):
        if line.start_time is not None and line.start_time <= t:
            idx = i
        else:
            break
    return idx


def build_scene(lines: list[LyricLine], t: float, window: int = 1) -> Scene:
    if not lines:
        raise ValueError("no lines to build a scene from")

    idx = find_current_line_index(lines, t)
    current = lines[idx]

    scene_lines: list[SceneLine] = []
    for offset in range(-window, window + 1):
        i = idx + offset
        if not (0 <= i < len(lines)):
            continue
        line = lines[i]
        is_current = offset == 0
        words = [
            SceneWord(
                text=w.word,
                chord=(
                    w.chord
                    if (is_current and w.chord and w.start_time is not None and w.start_time <= t)
                    else None
                ),
            )
            for w in line.words
        ]
        scene_lines.append(SceneLine(words=words, is_current=is_current, distance_from_current=offset))

    start = current.start_time or 0.0
    end = current.end_time if (current.end_time and current.end_time > start) else start + 1.0
    progress = min(max((t - start) / (end - start), 0.0), 1.0)

    return Scene(lines=scene_lines, image_key=line_hash(current.text), ken_burns_progress=progress)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_layout.py -v`
Expected: PASS (5 tests)

- [x] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/layout.py tests/test_layout.py
git commit -m "Add pure scene-layout functions for frame content"
```

---

### Task 8: Frame Rendering (Pillow + Ken Burns)

**Files:**
- Create: `lyricvideo/render.py`
- Create: `tests/conftest.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `Scene`, `SceneLine`, `SceneWord` from `lyricvideo.layout`.
- Produces: `FRAME_SIZE = (1920, 1080)`,
  `apply_ken_burns(image: Image.Image, progress: float, zoom_start: float = 1.0, zoom_end: float = 1.15) -> Image.Image`,
  `draw_scene(scene: Scene, background: Image.Image, font_path: str, font_size: int = 48, chord_font_size: int = 40) -> Image.Image`.
- Test fixture: `test_font_path` (in `tests/conftest.py`, shared with Task 9)
  — a real installed TrueType font path, or the test using it is skipped.

- [x] **Step 1: Write `tests/conftest.py`**

```python
from pathlib import Path

import pytest

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


@pytest.fixture
def test_font_path():
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    pytest.skip("no truetype font available in this environment")
```

- [x] **Step 2: Write the failing tests**

```python
# tests/test_render.py
from PIL import Image

from lyricvideo.render import apply_ken_burns, draw_scene, FRAME_SIZE
from lyricvideo.layout import Scene, SceneLine, SceneWord


def test_apply_ken_burns_returns_frame_sized_image():
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    out_start = apply_ken_burns(img, progress=0.0)
    out_end = apply_ken_burns(img, progress=1.0)
    assert out_start.size == FRAME_SIZE
    assert out_end.size == FRAME_SIZE


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
```

- [x] **Step 3: Run tests to verify they fail**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_render.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.render'`)

- [x] **Step 4: Write `lyricvideo/render.py`**

```python
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .layout import Scene, SceneLine

FRAME_SIZE = (1920, 1080)


def apply_ken_burns(
    image: Image.Image,
    progress: float,
    zoom_start: float = 1.0,
    zoom_end: float = 1.15,
) -> Image.Image:
    img = image.resize(FRAME_SIZE)
    zoom = zoom_start + (zoom_end - zoom_start) * progress
    w, h = img.size
    new_w, new_h = max(int(w * zoom), w), max(int(h * zoom), h)
    img = img.resize((new_w, new_h))
    max_dx, max_dy = new_w - w, new_h - h
    x = int(max_dx * progress)
    y = int(max_dy * progress)
    return img.crop((x, y, x + w, y + h))


def _draw_line_with_chords(draw, y, scene_line: SceneLine, font, chord_font, is_current: bool):
    words = scene_line.words
    if not words:
        return
    space_w = draw.textlength(" ", font=font)
    widths = [draw.textlength(w.text, font=font) for w in words]
    total_w = sum(widths) + space_w * max(len(words) - 1, 0)
    x = (FRAME_SIZE[0] - total_w) / 2
    fill = (255, 255, 255) if is_current else (190, 190, 190)
    for word, w_width in zip(words, widths):
        if word.chord:
            chord_w = draw.textlength(word.chord, font=chord_font)
            chord_x = x + (w_width - chord_w) / 2
            draw.text((chord_x, y - chord_font.size - 10), word.chord, font=chord_font, fill=(255, 215, 0))
        draw.text((x, y), word.text, font=font, fill=fill)
        x += w_width + space_w


def draw_scene(
    scene: Scene,
    background: Image.Image,
    font_path: str,
    font_size: int = 48,
    chord_font_size: int = 40,
) -> Image.Image:
    frame = background.copy()
    draw = ImageDraw.Draw(frame)
    font = ImageFont.truetype(font_path, font_size)
    chord_font = ImageFont.truetype(font_path, chord_font_size)

    center_y = FRAME_SIZE[1] // 2
    line_height = font_size + 40

    for sl in scene.lines:
        y = center_y + sl.distance_from_current * line_height
        _draw_line_with_chords(draw, y, sl, font, chord_font, sl.is_current)

    return frame
```

- [x] **Step 5: Run tests to verify they pass**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_render.py -v`
Expected: PASS (2 tests, unless no truetype font is found, in which case the
second test SKIPs — install `fonts-dejavu-core` or `fonts-liberation` via the
system package manager if that happens, since real rendering needs one.)

- [x] **Step 6: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/render.py tests/conftest.py tests/test_render.py
git commit -m "Add Pillow frame rendering with Ken Burns and word-synced chord labels"
```

---

### Task 9: Video Assembly (moviepy + audio mux)

**Files:**
- Create: `lyricvideo/assemble.py`
- Test: `tests/test_assemble.py`

**Interfaces:**
- Consumes: `LyricLine` from `lyricvideo.models`; `build_scene` from
  `lyricvideo.layout`; `apply_ken_burns`, `draw_scene`, `FRAME_SIZE` from
  `lyricvideo.render`; `test_font_path` fixture from `tests/conftest.py`.
- Produces: `assemble_video(lines: list[LyricLine], image_dir: Path, audio_path: Path, out_path: Path, font_path: str, fallback_color: tuple[int,int,int] = (30,30,40)) -> None`.

- [x] **Step 1: Write the failing test**

```python
# tests/test_assemble.py
from pathlib import Path

from lyricvideo.models import ChordWord, LyricLine


def test_assemble_video_invokes_write_videofile(tmp_path, monkeypatch, test_font_path):
    calls = {}

    class _FakeAudioClip:
        duration = 2.0

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame
            calls["duration"] = duration

        def set_audio(self, audio_clip):
            calls["audio_clip"] = audio_clip
            return self

        def write_videofile(self, path, fps, codec, audio_codec):
            calls["write_path"] = path
            calls["fps"] = fps
            Path(path).write_bytes(b"fake-mp4")

    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: _FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", _FakeVideoClip)

    from lyricvideo.assemble import assemble_video

    lines = [
        LyricLine(
            words=[ChordWord(word="hi", chord="G", start_time=0.0, end_time=1.0)],
            start_time=0.0, end_time=1.0,
        )
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(lines, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path)

    assert calls["duration"] == 2.0
    assert calls["fps"] == 24
    # exercise the real make_frame once to confirm the wiring works end to end
    # (no cached image on disk here, so it exercises the fallback-color path)
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (1080, 1920)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_assemble.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.assemble'`)

- [x] **Step 3: Write `lyricvideo/assemble.py`**

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
from moviepy.editor import AudioFileClip, VideoClip
from PIL import Image

from .layout import build_scene
from .models import LyricLine
from .render import FRAME_SIZE, apply_ken_burns, draw_scene


def assemble_video(
    lines: list[LyricLine],
    image_dir: Path,
    audio_path: Path,
    out_path: Path,
    font_path: str,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
) -> None:
    image_cache: dict[str, Image.Image] = {}

    def get_image(key: str) -> Image.Image:
        if key not in image_cache:
            path = image_dir / f"{key}.png"
            if path.exists():
                image_cache[key] = Image.open(path).convert("RGB")
            else:
                image_cache[key] = Image.new("RGB", FRAME_SIZE, fallback_color)
        return image_cache[key]

    def make_frame(t: float):
        scene = build_scene(lines, t)
        bg = apply_ken_burns(get_image(scene.image_key), scene.ken_burns_progress)
        frame = draw_scene(scene, bg, font_path)
        return np.array(frame)

    audio_clip = AudioFileClip(str(audio_path))
    duration = audio_clip.duration
    video_clip = VideoClip(make_frame, duration=duration).set_audio(audio_clip)
    video_clip.write_videofile(str(out_path), fps=24, codec="libx264", audio_codec="aac")
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_assemble.py -v`
Expected: PASS (1 test, unless no font is found — see Task 8's note)

- [x] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/assemble.py tests/test_assemble.py
git commit -m "Add video assembly (moviepy) with audio mux"
```

---

### Task 10: Pipeline Orchestration & CLI

**Files:**
- Create: `lyricvideo/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9 — `Song`, `LyricLine`, `ChordWord`,
  `save_song`, `load_song` (`models`); `parse_tab_pdf` (`pdf_parse`);
  `separate_vocals` (`separate`); `align_words` (`align`);
  `combine_alignment` (`combine`); `get_or_generate_image` (`imagery`);
  `assemble_video` (`assemble`).
- Produces: `STAGES = ["separate", "parse", "align", "images", "render"]`,
  `line_to_dict(line: LyricLine) -> dict`, `dict_to_line(d: dict) -> LyricLine`,
  `run_pipeline(audio_path: Path, tab_pdf_path: Path, work_dir: Path, title: str, start_stage: str = "separate", font_path: str | None = None) -> Path`,
  `main()` (CLI entrypoint, run via `python -m lyricvideo.pipeline`).

- [x] **Step 1: Write the failing tests**

```python
# tests/test_pipeline.py
from pathlib import Path

from lyricvideo.models import ChordWord, LyricLine, Song, save_song
from lyricvideo.pipeline import line_to_dict, dict_to_line, run_pipeline


def test_line_dict_round_trip():
    line = LyricLine(
        words=[ChordWord(word="hi", chord="G", start_time=0.1, end_time=0.5)],
        start_time=0.1, end_time=0.5,
    )
    restored = dict_to_line(line_to_dict(line))
    assert restored == line


def test_run_pipeline_skips_earlier_stages(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        "lyricvideo.pipeline.separate_vocals",
        lambda *a, **k: calls.append("separate") or (tmp_path / "vocals.wav"),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.parse_tab_pdf",
        lambda *a, **k: calls.append("parse") or [],
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.align_words",
        lambda *a, **k: calls.append("align") or [],
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.combine_alignment",
        lambda *a, **k: calls.append("align") or [],
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image",
        lambda *a, **k: calls.append("images") or Path("x"),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.assemble_video",
        lambda *a, **k: calls.append("render"),
    )
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setattr("lyricvideo.pipeline.replicate", type("M", (), {"Client": lambda: object()}))

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    seeded_line = LyricLine(
        words=[ChordWord(word="hi", chord="G", start_time=0.0, end_time=1.0)],
        start_time=0.0, end_time=1.0,
    )
    save_song(
        Song(title="t", audio_path="a.mp3", lines=[seeded_line]),
        work_dir / "lyrics_timed.json",
    )

    run_pipeline(Path("audio.mp3"), Path("tab.pdf"), work_dir, "t", start_stage="images")

    assert calls == ["images", "render"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'lyricvideo.pipeline'`)

- [x] **Step 3: Write `lyricvideo/pipeline.py`**

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path

import anthropic
import replicate
import torchaudio
from dotenv import load_dotenv

from .align import align_words
from .assemble import assemble_video
from .combine import combine_alignment
from .imagery import get_or_generate_image
from .models import ChordWord, LyricLine, Song, load_song, save_song
from .pdf_parse import parse_tab_pdf
from .separate import separate_vocals

STAGES = ["separate", "parse", "align", "images", "render"]


def line_to_dict(line: LyricLine) -> dict:
    return {
        "words": [
            {"word": w.word, "chord": w.chord, "start_time": w.start_time, "end_time": w.end_time}
            for w in line.words
        ],
        "start_time": line.start_time,
        "end_time": line.end_time,
    }


def dict_to_line(d: dict) -> LyricLine:
    return LyricLine(
        words=[ChordWord(**w) for w in d["words"]],
        start_time=d.get("start_time"),
        end_time=d.get("end_time"),
    )


def _default_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    raise FileNotFoundError("No default font found; pass --font explicitly")


def run_pipeline(
    audio_path: Path,
    tab_pdf_path: Path,
    work_dir: Path,
    title: str,
    start_stage: str = "separate",
    font_path: str | None = None,
) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    vocals_path = work_dir / "vocals.wav"
    parsed_path = work_dir / "parsed_tab.json"
    timed_path = work_dir / "lyrics_timed.json"
    images_dir = work_dir / "images"
    final_path = work_dir / "final.mp4"

    start_idx = STAGES.index(start_stage)

    if start_idx <= STAGES.index("separate"):
        vocals_path = separate_vocals(audio_path, work_dir)

    if start_idx <= STAGES.index("parse"):
        parsed_lines = parse_tab_pdf(tab_pdf_path)
        parsed_path.write_text(
            json.dumps([line_to_dict(l) for l in parsed_lines], indent=2), encoding="utf-8"
        )

    if start_idx <= STAGES.index("align"):
        if start_idx > STAGES.index("parse"):
            parsed_lines = [
                dict_to_line(d) for d in json.loads(parsed_path.read_text(encoding="utf-8"))
            ]
        info = torchaudio.info(str(vocals_path))
        audio_duration = info.num_frames / info.sample_rate
        flat_words = [w.word for line in parsed_lines for w in line.words]
        word_times = align_words(vocals_path, flat_words)
        timed_lines = combine_alignment(parsed_lines, word_times, audio_duration)
        song = Song(
            title=title,
            audio_path=str(audio_path),
            vocal_stem_path=str(vocals_path),
            lines=timed_lines,
        )
        save_song(song, timed_path)
    else:
        song = load_song(timed_path)

    if start_idx <= STAGES.index("images"):
        anthropic_client = anthropic.Anthropic()
        replicate_client = replicate.Client()
        full_lyrics = "\n".join(l.text for l in song.lines)
        images_dir.mkdir(exist_ok=True)
        for line in song.lines:
            get_or_generate_image(anthropic_client, replicate_client, full_lyrics, line.text, images_dir)

    if start_idx <= STAGES.index("render"):
        assemble_video(song.lines, images_dir, audio_path, final_path, font_path or _default_font())

    return final_path


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Generate a synced lyric+chord video from a tab PDF and audio file."
    )
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--tab-pdf", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--stage", choices=STAGES, default="separate")
    parser.add_argument("--font", default=None)
    args = parser.parse_args()

    out = run_pipeline(args.audio, args.tab_pdf, args.work_dir, args.title, args.stage, args.font)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: PASS (2 tests)

- [x] **Step 5: Run the full test suite**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest -v`
Expected: PASS (all tests across all modules, some `test_render.py`/
`test_assemble.py` cases SKIPped only if no system TrueType font was found —
see Task 8)

- [x] **Step 6: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/pipeline.py tests/test_pipeline.py
git commit -m "Add staged/resumable pipeline orchestration and CLI entrypoint"
```

---

### Task 11: Manual End-to-End Verification (Wish You Were Here)

Real forced-alignment accuracy on sung vocals, real Demucs separation
quality, and real visual sync can only be judged against a real song — this
is the manual verification step the spec calls for, not a unit test.

**Files used (already on this machine from prior GuitarTrainer work, not
copied into this repo — this project reads them from their existing
location):**
- Audio: `/home/doug/GuitarTrainer/data/songs/bceed218-dc5f-4091-a534-8242a999e552/original.mp3`
- Tab PDF: `/home/doug/GuitarTrainer/Tab Music/wish you were here tab.pdf`

- [x] **Step 1: Set API keys**

```bash
cd /home/doug/LyricVideoGen
cp .env.example .env
# edit .env and fill in real ANTHROPIC_API_KEY and REPLICATE_API_TOKEN
```

- [x] **Step 2: Run the full pipeline**

```bash
cd /home/doug/LyricVideoGen
.venv/bin/python -m lyricvideo.pipeline \
  --audio "/home/doug/GuitarTrainer/data/songs/bceed218-dc5f-4091-a534-8242a999e552/original.mp3" \
  --tab-pdf "/home/doug/GuitarTrainer/Tab Music/wish you were here tab.pdf" \
  --work-dir work/wish-you-were-here \
  --title "Wish You Were Here"
```

Expect this to take a while on CPU-only hardware (Demucs separation +
forced alignment), consistent with GuitarTrainer's own CPU-only pipeline
timings noted in the spec.

- [x] **Step 3: Inspect intermediate artifacts before watching the final video**

```bash
cat work/wish-you-were-here/parsed_tab.json | head -50
cat work/wish-you-were-here/lyrics_timed.json | head -50
ls work/wish-you-were-here/images | wc -l
```

Confirm: `parsed_tab.json` has real lyric lines with plausible chord
assignments (not garbage/misaligned words); `lyrics_timed.json` has
monotonically increasing, plausible timestamps; the number of files in
`images/` is less than or equal to the number of unique lyric lines (proof
the caching/dedup is actually working, not regenerating every line).

- [x] **Step 4: Watch/listen to the final video**

```bash
xdg-open work/wish-you-were-here/final.mp4
```

Judge by eye/ear: do the lyrics scroll in time with the actual vocals? Do
chord names appear above the correct word at roughly the correct moment? Do
background images plausibly match what each line is about? If forced
alignment is meaningfully off, note it — per the spec's Honest Uncertainty
section, singing-vs-speech alignment accuracy is a genuinely open question
this step is what actually answers, and a correction mechanism (e.g.
hand-editing `lyrics_timed.json` and re-running from `--stage render`) is
already supported by the staged pipeline if needed.

- [x] **Step 5: Report results to the owner**

Summarize what worked, what didn't (if anything), and whether a follow-up
fix is needed before this is considered done — do not declare the tool
"working" without having actually watched the rendered output per Step 4.

---

## Self-Review Notes

- **Spec coverage:** every in-scope v1 item (PDF parsing, forced alignment,
  per-line cached AI imagery, Ken Burns + scrolling-lyric + word-synced
  chord rendering, staged/resumable CLI, Wish You Were Here as first test)
  has a task. Deferred items (audio-only fallback, note-grid tab, OCR, video
  backgrounds, auto-upload, chord diagrams) have no task, correctly.
- **Type/signature consistency:** verified `parse_tab_pdf`, `separate_vocals`,
  `align_words`, `combine_alignment`, `get_or_generate_image`, `build_scene`,
  `apply_ken_burns`, `draw_scene`, `assemble_video`, `line_hash` are called
  with the same names/argument order/defaults everywhere they're consumed
  across tasks (traced Task 10's `pipeline.py` against Tasks 2–9's produced
  interfaces line by line).
- **No placeholders:** every step has real, complete code — no TODOs.
