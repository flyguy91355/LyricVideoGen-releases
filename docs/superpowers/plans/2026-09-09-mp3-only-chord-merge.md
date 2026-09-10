# MP3-Only Chord/Lyric-ID Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace this program's tab-PDF/chords-text input and above-word chord
display with LyricChord's audio-only chord detection and MP3-only lyric-fetching,
while keeping this program's forced-alignment lyric sync, AI Ken-Burns images, and
Redo-an-Existing-Song feature.

**Architecture:** Two new pipeline stages (`identify`, `fetch_lyrics`) resolve song
identity and lyric text from nothing but the audio file, ported from LyricChord's
`metadata.py`/`lyrics.py`/`vocal.py`. A third new stage (`detect_chords`), ported from
LyricChord's `chords/local.py`/`theory.py`, produces a `ChordTrack` from Demucs's own
clean `no_vocals.wav` stem. The existing `align` stage now times `fetch_lyrics`'s text
instead of tab-parsed text (its own internals are unchanged). `render.py` gains a
LyricChord-style NOW/NEXT/timeline chord bar; the lyric block's background image now
follows the active chord during instrumental gaps instead of freezing. The tab-PDF/
vision/OCR parsing path and the old above-word chord display are deleted entirely.

**Tech Stack:** Python 3.12, librosa (chroma/beat/Viterbi), mutagen (tags), requests
(lrclib/MusicBrainz/syncedlyrics), Pillow (rendering), existing Demucs/torchaudio
(separation + forced alignment), pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md`

## Global Constraints

- Audio-detected chords always win over any human-supplied chord source — there is no
  sheet/Ultimate-Guitar cascade in this program; `detect_chords` is the only chord
  source.
- The only required input for a new song is the audio file. "Song title" is an
  optional override, not a required field.
- No song title or artist text is drawn into the video frame anywhere (owner-confirmed
  2026-09-09) — only the chord bar and a Key/BPM badge are added to the frame.
- Every new/ported module follows this codebase's existing style: `from __future__
  import annotations`, lowercase builtin generics (`list[str]`, `dict[str, int]`), `X |
  None` unions — not `typing.List`/`Optional`.
- Every `Path.read_text()`/`.write_text()` call must pass `encoding="utf-8"` explicitly
  (matches this codebase's existing convention throughout).
- Redo an Existing Song must keep working, resuming at the new `"fetch_lyrics"` stage
  instead of `"align"`, still reusing cached Demucs stems and images.
- AcoustID fingerprinting and Ultimate-Guitar online chord lookup are explicitly out of
  scope — do not port `identify_acoustid`, `chords/online.py`, or `chords/sheet.py`.
- Run `cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/ -v`
  after every task; all tests must pass before committing.

---

## Task 1: Add new dependencies

**Files:**
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `mutagen`, `requests`, `syncedlyrics` importable in the venv for later tasks.

- [ ] **Step 1: Add the three new packages to requirements.txt**

Open `requirements.txt` and add these lines under a new comment block (position doesn't
matter functionally; group them near the top for readability):

```
# Song identification / lyric fetching (ported from LyricChord)
mutagen>=1.47
requests>=2.31
syncedlyrics>=1.0
```

Leave every existing line untouched — `pdfplumber`/`pytesseract` are removed later
(Task 11), once nothing imports them anymore.

- [ ] **Step 2: Install into the venv**

Run: `cd /home/doug/PlayAlongVideoProduction && .venv/bin/pip install -r requirements.txt`
Expected: `mutagen`, `requests`, `syncedlyrics` install successfully (no errors). Confirm with:

```bash
.venv/bin/python -c "import mutagen, requests, syncedlyrics; print('ok')"
```

Expected output: `ok`

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all 183 tests still pass (pure dependency addition, no code changed yet).

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Add mutagen/requests/syncedlyrics for the LyricChord lyric/metadata port"
```

---

## Task 2: Port the FFmpeg audio-decoding helper

LyricChord's chord detection and vocal-onset disambiguation both decode audio via a
direct FFmpeg subprocess call (not `librosa.load`), because it handles MP3/M4A/OGG
without depending on libsndfile's codec support. Both later tasks need this.

**Files:**
- Create: `lyricvideo/audio_decode.py`
- Test: `tests/test_audio_decode.py`

**Interfaces:**
- Produces: `decode_audio(path: Path, sr: int = 22050, start: float | None = None, duration: float | None = None) -> np.ndarray` (mono float32), `find_ffmpeg() -> str`.
- Consumes: nothing from this codebase; the system's `ffmpeg` binary (already present —
  confirmed at `/usr/bin/ffmpeg`, and `imageio-ffmpeg` is already a dependency as a
  fallback).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_audio_decode.py`:

```python
import numpy as np
import pytest

from lyricvideo.audio_decode import decode_audio, find_ffmpeg, probe_duration


def test_find_ffmpeg_returns_an_existing_path():
    exe = find_ffmpeg()
    assert exe  # non-empty string; either "ffmpeg" resolved via PATH or an absolute path


def test_decode_audio_returns_mono_float32_array(tmp_path):
    import subprocess

    wav_path = tmp_path / "tone.wav"
    # Generate 1 second of a 440Hz sine tone with ffmpeg itself, so this test has
    # no dependency on a real song file.
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ar", "22050", "-ac", "1", str(wav_path)],
        check=True,
    )

    y = decode_audio(wav_path, sr=22050)

    assert y.dtype == np.float32
    assert y.ndim == 1
    assert 22000 < y.size < 22100  # ~1 second at 22050 Hz


def test_decode_audio_respects_start_and_duration(tmp_path):
    import subprocess

    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-ar", "22050", "-ac", "1", str(wav_path)],
        check=True,
    )

    full = decode_audio(wav_path, sr=22050)
    windowed = decode_audio(wav_path, sr=22050, start=1.0, duration=1.0)

    assert windowed.size < full.size
    assert 22000 < windowed.size < 22100


def test_probe_duration_reads_real_duration(tmp_path):
    import subprocess

    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         str(wav_path)],
        check=True,
    )

    duration = probe_duration(wav_path)

    assert 1.9 < duration < 2.1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_audio_decode.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.audio_decode'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/audio_decode.py`:

```python
"""Audio decoding via FFmpeg, ported from LyricChord's utils/audio.py + utils/ffmpeg.py.

FFmpeg (not librosa.load/soundfile) decodes so MP3/M4A/OGG all work without relying on
libsndfile's codec support -- the same reasoning LyricChord's own docstring gives.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class FFmpegNotFound(RuntimeError):
    pass


@lru_cache(maxsize=1)
def find_ffmpeg() -> str:
    """Resolution order: PLAYALONGVIDEOPRODUCTION_FFMPEG env var -> `ffmpeg` on PATH ->
    the binary bundled with the already-required `imageio-ffmpeg` package."""
    env = os.environ.get("PLAYALONGVIDEOPRODUCTION_FFMPEG")
    if env and os.path.isfile(env):
        return env

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # ImportError or download failure
        pass

    raise FFmpegNotFound(
        "FFmpeg was not found. Install it and add it to PATH, set "
        "PLAYALONGVIDEOPRODUCTION_FFMPEG to the executable, or `pip install imageio-ffmpeg`."
    )


def decode_audio(
    path: Path, sr: int = 22050, start: float | None = None, duration: float | None = None
) -> np.ndarray:
    """Decode an audio file (or a window of it) to mono float32 at sample rate `sr`.

    `start`/`duration` are seconds; FFmpeg seeks and stops itself, so a short window of
    a long file costs a fraction of a full decode.
    """
    cmd = [find_ffmpeg(), "-v", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{max(0.0, duration):.3f}"]
    cmd += ["-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(sr), "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="ignore")[:400]
        raise RuntimeError(f"FFmpeg failed to decode {path.name}: {err}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def probe_duration(path: Path) -> float:
    """Duration in seconds from FFmpeg's input banner (a header read, not a decode)."""
    cmd = [find_ffmpeg(), "-hide_banner", "-i", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = _DURATION_RE.search(proc.stderr or "")
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)
```

Note: `creationflags=no_window_flag()` was dropped from LyricChord's original — that
flag only matters on Windows to suppress a flashing console window, and this program
targets Linux only (see CLAUDE.md).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_audio_decode.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/audio_decode.py tests/test_audio_decode.py
git commit -m "Port LyricChord's FFmpeg audio-decoding helper"
```

---

## Task 3: Port chord music-theory helpers

**Files:**
- Create: `lyricvideo/chord_theory.py`
- Test: `tests/test_chord_theory.py`

**Interfaces:**
- Produces: `build_templates(qualities: list[str]) -> tuple[list[tuple[int, str]], np.ndarray]`,
  `estimate_key(chroma_mean: np.ndarray) -> tuple[int, str, float]`,
  `diatonic_chords(tonic: int, mode: str, include_sevenths: bool = False) -> set[tuple[int, str]]`,
  `key_uses_flats(tonic: int, mode: str) -> bool`, `key_name(tonic: int, mode: str, prefer_flats: bool = True) -> str`,
  `spell(root: int, quality: str, use_flats: bool) -> str`, `TRIADS: list[str]`, `SEVENTHS: list[str]`.
- Consumes: nothing from this codebase (pure math/music-theory, `numpy` only).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chord_theory.py`:

```python
import numpy as np

from lyricvideo.chord_theory import (
    KS_MAJOR,
    TRIADS,
    build_templates,
    diatonic_chords,
    estimate_key,
    key_name,
    key_uses_flats,
    spell,
)


def test_build_templates_returns_one_row_per_root_per_quality():
    chords, templates = build_templates(TRIADS)  # ["maj", "min"]
    assert len(chords) == 24  # 12 roots x 2 qualities
    assert templates.shape == (24, 12)


def test_build_templates_rows_are_unit_norm():
    _, templates = build_templates(TRIADS)
    norms = np.linalg.norm(templates, axis=1)
    assert np.allclose(norms, 1.0)


def test_estimate_key_recognizes_c_major_profile():
    # KS_MAJOR itself, rolled to tonic 0, is a perfect C major profile.
    tonic, mode, confidence = estimate_key(KS_MAJOR)
    assert tonic == 0
    assert mode == "major"
    assert confidence > 0.9


def test_estimate_key_handles_all_silence():
    tonic, mode, confidence = estimate_key(np.zeros(12))
    assert tonic == 0
    assert mode == "major"
    assert confidence == 0.0


def test_diatonic_chords_c_major_contains_expected_triads():
    dia = diatonic_chords(tonic=0, mode="major")
    assert (0, "maj") in dia   # C
    assert (7, "maj") in dia   # G
    assert (9, "min") in dia   # Am
    assert (1, "maj") not in dia  # Db, not diatonic to C major


def test_key_uses_flats_for_f_major():
    assert key_uses_flats(tonic=5, mode="major") is True  # F major


def test_key_uses_flats_false_for_g_major():
    assert key_uses_flats(tonic=7, mode="major") is False


def test_key_name_formats_readably():
    assert key_name(tonic=0, mode="major") == "C major"
    assert key_name(tonic=9, mode="minor") == "A minor"


def test_spell_major_and_minor():
    assert spell(root=0, quality="maj", use_flats=False) == "C"
    assert spell(root=9, quality="min", use_flats=False) == "Am"
    assert spell(root=10, quality="maj", use_flats=True) == "Bb"
    assert spell(root=10, quality="maj", use_flats=False) == "A#"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_chord_theory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.chord_theory'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/chord_theory.py` (ported from LyricChord's `chords/theory.py`,
trimmed of the `parse_label`/`canonical_label`/`is_chord_token` chord-sheet-parsing
helpers, which have no purpose once there is no sheet input — chord LABELS are only
ever produced here, never parsed from text):

```python
"""Music-theory helpers: pitch classes, chord templates, key estimation, spelling.

Ported from LyricChord's chords/theory.py. Chord "qualities" are kept deliberately
small (triads plus optional sevenths) so the on-screen chords stay playable for a
strumming guitarist/pianist.
"""

from __future__ import annotations

import numpy as np

NOTES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# quality -> semitone intervals from the root
QUALITY_INTERVALS: dict[str, tuple[int, ...]] = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "min7": (0, 3, 7, 10),
    "maj7": (0, 4, 7, 11),
}
QUALITY_SUFFIX: dict[str, str] = {"maj": "", "min": "m", "7": "7", "min7": "m7", "maj7": "maj7"}

TRIADS = ["maj", "min"]
SEVENTHS = ["7", "min7", "maj7"]

# Krumhansl-Schmuckler key profiles.
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Keys conventionally written with flats (major tonics; minors via relative major).
_FLAT_MAJOR_TONICS = {5, 10, 3, 8, 1, 6}  # F Bb Eb Ab Db Gb

Chord = tuple[int, str]  # (root pitch class, quality)


def build_templates(qualities: list[str]) -> tuple[list[Chord], np.ndarray]:
    """Return chord list and a (n_chords, 12) matrix of unit-norm binary templates."""
    chords: list[Chord] = []
    rows = []
    for q in qualities:
        for root in range(12):
            vec = np.zeros(12)
            for iv in QUALITY_INTERVALS[q]:
                vec[(root + iv) % 12] = 1.0
            vec[root] = 1.15  # slight emphasis on the root
            rows.append(vec / np.linalg.norm(vec))
            chords.append((root, q))
    return chords, np.vstack(rows)


def estimate_key(chroma_mean: np.ndarray) -> tuple[int, str, float]:
    """Krumhansl-Schmuckler key finding. Returns (tonic_pc, 'major'|'minor', confidence)."""
    c = np.asarray(chroma_mean, dtype=float)
    if c.sum() <= 0:
        return 0, "major", 0.0
    best = (0, "major", -2.0)
    for tonic in range(12):
        for mode, profile in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            score = float(np.corrcoef(c, np.roll(profile, tonic))[0, 1])
            if score > best[2]:
                best = (tonic, mode, score)
    return best


def diatonic_chords(tonic: int, mode: str, include_sevenths: bool = False) -> set[Chord]:
    """Chords built on the scale degrees of the key (harmonic-minor V included)."""
    if mode == "major":
        degrees = [(0, "maj"), (2, "min"), (4, "min"), (5, "maj"), (7, "maj"), (9, "min")]
        sevenths = [(0, "maj7"), (2, "min7"), (4, "min7"), (5, "maj7"), (7, "7"), (9, "min7")]
    else:
        degrees = [(0, "min"), (3, "maj"), (5, "min"), (7, "min"), (7, "maj"), (8, "maj"), (10, "maj")]
        sevenths = [(0, "min7"), (3, "maj7"), (5, "min7"), (7, "7"), (8, "maj7"), (10, "7")]
    out = {((tonic + d) % 12, q) for d, q in degrees}
    if include_sevenths:
        out |= {((tonic + d) % 12, q) for d, q in sevenths}
    return out


def key_uses_flats(tonic: int, mode: str) -> bool:
    rel_major = tonic if mode == "major" else (tonic + 3) % 12
    return rel_major in _FLAT_MAJOR_TONICS


def key_name(tonic: int, mode: str, prefer_flats: bool = True) -> str:
    names = NOTES_FLAT if (prefer_flats and key_uses_flats(tonic, mode)) else NOTES_SHARP
    return f"{names[tonic]} {mode}"


def spell(root: int, quality: str, use_flats: bool) -> str:
    names = NOTES_FLAT if use_flats else NOTES_SHARP
    return names[root % 12] + QUALITY_SUFFIX.get(quality, quality)
```

Dropped versus the original: the `dim`/`aug`/`sus2`/`sus4` qualities and the
diminished-vii scale degree (LyricChord's own `TRIADS`/`SEVENTHS` constants used here
never include them either — they only appear in LyricChord's chord-sheet *parsing*
vocabulary, which this program has no use for since it never parses chord text), and
every `parse_label`/`canonical_label`/`is_chord_token` sheet-parsing function.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_chord_theory.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/chord_theory.py tests/test_chord_theory.py
git commit -m "Port LyricChord's chord music-theory helpers"
```

---

## Task 4: Port the ChordEvent/ChordTrack models and chord-detection stage

**Files:**
- Create: `lyricvideo/detect_chords.py`
- Test: `tests/test_detect_chords.py`

**Interfaces:**
- Consumes: `lyricvideo.audio_decode.decode_audio` (Task 2), `lyricvideo.chord_theory.*` (Task 3).
- Produces: `ChordEvent(start: float, end: float, label: str)` dataclass (with a
  `.duration` property), `ChordTrack(events: list[ChordEvent], key: str = "", bpm: float
  = 0.0)` dataclass, `detect_chords(path: Path) -> ChordTrack`. **These dataclasses live
  in this file, not `models.py`** — Task 8 moves them into `models.py` alongside the
  rest of the song data model once the big cutover happens; defining them here first
  keeps this task fully self-contained and testable in isolation.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detect_chords.py`:

```python
import subprocess

from lyricvideo.audio_decode import find_ffmpeg
from lyricvideo.detect_chords import ChordEvent, ChordTrack, detect_chords


def _make_test_tone(tmp_path, freq=220, duration=6):
    """A simple, single-pitch tone -- not a real chord, but enough to exercise
    the full pipeline (harmonic separation -> chroma -> beat sync -> template
    match -> Viterbi -> silence detection) without needing a real song file."""
    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={duration}",
         "-ar", "22050", str(wav_path)],
        check=True,
    )
    return wav_path


def test_chord_event_duration():
    event = ChordEvent(start=1.0, end=3.5, label="Am")
    assert event.duration == 2.5


def test_chord_track_defaults_to_empty():
    track = ChordTrack()
    assert track.events == []
    assert track.key == ""
    assert track.bpm == 0.0


def test_detect_chords_returns_a_track_covering_the_whole_file(tmp_path):
    wav_path = _make_test_tone(tmp_path, duration=6)

    track = detect_chords(wav_path)

    assert isinstance(track, ChordTrack)
    assert track.events  # at least one segment
    assert track.events[0].start == 0.0
    assert track.events[-1].end >= 5.5  # covers (close to) the whole 6s file


def test_detect_chords_on_silence_returns_no_chord_label(tmp_path):
    wav_path = tmp_path / "silence.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "3", str(wav_path)],
        check=True,
    )

    track = detect_chords(wav_path)

    assert all(e.label == "N" for e in track.events)


def test_detect_chords_on_short_clip_returns_empty_track(tmp_path):
    wav_path = tmp_path / "tiny.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
         "-ar", "22050", str(wav_path)],
        check=True,
    )

    track = detect_chords(wav_path)

    assert track.events == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_detect_chords.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.detect_chords'`

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/detect_chords.py` (ported from LyricChord's `chords/local.py`,
adapted: `Settings`-object config fields become plain module constants at their
LyricChord defaults, `decode_audio`/theory imports point at this project's own
modules, and `ChordEvent`/`ChordTrack` are defined here directly):

```python
"""Local chord detection with librosa, ported from LyricChord's chords/local.py.

Pipeline:
  audio -> harmonic/percussive separation -> CQT chroma -> beat-synchronous chroma
        -> cosine similarity against chord templates -> key prior
        -> Viterbi decoding with a "sticky" transition matrix -> merged segments

Not as accurate as a trained model, but runs offline in ~10-30s per song and produces
clean, playable progressions for most pop/rock/folk material (LyricChord's own
characterization, unchanged by this port).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import chord_theory as theory
from .audio_decode import decode_audio

SR = 22050
HOP = 512
SOFTMAX_TEMPERATURE = 0.04     # lower = trust the template match more
STAY_PROBABILITY = 0.85        # Viterbi self-transition probability per beat
NON_DIATONIC_PENALTY = 0.75    # multiplies similarity for chords outside the key
SILENCE_RATIO = 0.03           # segments below this fraction of peak RMS become "N"

# LyricChord's own Settings defaults (config.py), fixed here since this program has
# no per-run chord-detection settings UI.
SNAP_CHORDS_TO_KEY = True
PREFER_FLATS = True
MIN_CHORD_SECONDS = 0.5
INCLUDE_SEVENTH_CHORDS = False


@dataclass
class ChordEvent:
    """A chord held from `start` to `end` (seconds). Label like 'Am', 'F#', 'N'."""

    start: float
    end: float
    label: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ChordTrack:
    """Timeline of chords plus global musical info for one song."""

    events: list[ChordEvent] = field(default_factory=list)
    key: str = ""
    bpm: float = 0.0


def _segments(boundaries: np.ndarray) -> list[slice]:
    return [slice(int(a), int(b)) for a, b in zip(boundaries[:-1], boundaries[1:]) if b > a]


def _fold_tempo(bpm: float) -> float:
    """Fold octave errors of the beat tracker into the common 60-190 BPM range."""
    if bpm <= 0:
        return 0.0
    while bpm < 60:
        bpm *= 2
    while bpm > 190:
        bpm /= 2
    return bpm


def _softmax(x: np.ndarray, axis: int = 0) -> np.ndarray:
    x = x - x.max(axis=axis, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=axis, keepdims=True)


def _merge_short_events(events: list[ChordEvent], min_seconds: float) -> list[ChordEvent]:
    """Absorb segments shorter than `min_seconds` into their neighbours."""
    if min_seconds <= 0 or len(events) < 2:
        return events
    out: list[ChordEvent] = []
    i = 0
    while i < len(events):
        ev = events[i]
        if ev.duration < min_seconds and (out or i + 1 < len(events)):
            if out:
                out[-1].end = ev.end
            else:
                events[i + 1].start = ev.start
            i += 1
            continue
        if out and out[-1].label == ev.label:
            out[-1].end = ev.end
        else:
            out.append(ChordEvent(ev.start, ev.end, ev.label))
        i += 1
    return out


def detect_chords(path: Path) -> ChordTrack:
    """Analyse an audio file and return its ChordTrack."""
    import librosa  # imported lazily: slow to import, only needed here

    y = decode_audio(path, SR)
    if y.size < SR:  # < 1 s of audio
        return ChordTrack()

    # 1) Tonal content only: percussive hits smear chroma.
    y_harm = librosa.effects.harmonic(y=y, margin=3.0)

    # 2) Chroma from a constant-Q transform (better low-frequency resolution than STFT).
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=SR, hop_length=HOP)
    n_frames = chroma.shape[1]

    # 3) Beat tracking on the full mix; chords are assumed to change on beats.
    try:
        tempo, beats = librosa.beat.beat_track(y=y, sr=SR, hop_length=HOP, units="frames")
        tempo = _fold_tempo(float(np.atleast_1d(tempo)[0]))
    except Exception:
        tempo, beats = 0.0, np.array([], dtype=int)
    if len(beats) < 8:
        beats = np.arange(0, n_frames, max(1, int(0.5 * SR / HOP)))
    boundaries = np.unique(np.concatenate([[0], beats, [n_frames]]).astype(int))
    segs = _segments(boundaries)
    if not segs:
        return ChordTrack()

    seg_chroma = librosa.util.sync(data=chroma, idx=segs, aggregate=np.median, pad=False)
    seg_chroma = seg_chroma / (np.linalg.norm(seg_chroma, axis=0, keepdims=True) + 1e-9)

    # 4) Template matching.
    qualities = theory.TRIADS + (theory.SEVENTHS if INCLUDE_SEVENTH_CHORDS else [])
    chords, templates = theory.build_templates(qualities)
    sim = templates @ seg_chroma

    tonic, mode, _key_conf = theory.estimate_key(chroma.mean(axis=1))
    if SNAP_CHORDS_TO_KEY:
        diatonic = theory.diatonic_chords(tonic, mode, include_sevenths=True)
        prior = np.array([1.0 if c in diatonic else NON_DIATONIC_PENALTY for c in chords])
        sim = sim * prior[:, None]

    # 5) Viterbi smoothing: chords tend to persist for several beats.
    prob = _softmax(sim / SOFTMAX_TEMPERATURE, axis=0)
    transition = librosa.sequence.transition_loop(n_states=len(chords), prob=STAY_PROBABILITY)
    states = librosa.sequence.viterbi(prob=prob, transition=transition)

    # 6) Silence detection -> "N" (no chord).
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    rms = rms[:n_frames] if rms.size >= n_frames else np.pad(rms, (0, n_frames - rms.size))
    seg_rms = np.array([rms[s].mean() if s.stop > s.start else 0.0 for s in segs])
    silent = seg_rms < max(1e-4, SILENCE_RATIO * seg_rms.max())

    use_flats = PREFER_FLATS and theory.key_uses_flats(tonic, mode)
    times = librosa.frames_to_time(frames=boundaries, sr=SR, hop_length=HOP)
    events: list[ChordEvent] = []
    for i, state in enumerate(states):
        label = "N" if silent[i] else theory.spell(chords[state][0], chords[state][1], use_flats)
        start, end = float(times[i]), float(times[i + 1])
        if events and events[-1].label == label:
            events[-1].end = end
        else:
            events.append(ChordEvent(start, end, label))

    events = _merge_short_events(events, MIN_CHORD_SECONDS)
    duration = y.size / SR
    if events:
        events[-1].end = max(events[-1].end, duration)

    return ChordTrack(events=events, key=theory.key_name(tonic, mode, PREFER_FLATS), bpm=round(tempo, 1))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_detect_chords.py -v`
Expected: 5 passed (this stage genuinely runs librosa's full analysis, so this test
file will take several seconds — that's expected, not a hang).

- [ ] **Step 5: Commit**

```bash
git add lyricvideo/detect_chords.py tests/test_detect_chords.py
git commit -m "Port LyricChord's librosa-based chord-detection stage"
```

---

## Task 5: Port shared text helpers, then song identification (identify.py)

**Files:**
- Create: `lyricvideo/text_clean.py`
- Create: `lyricvideo/identify.py`
- Test: `tests/test_text_clean.py`
- Test: `tests/test_identify.py`

**Interfaces:**
- Produces (`text_clean.py`): `clean_title(text: str) -> str`, `normalize(text: str) -> str`,
  `artist_key(name: str) -> str`, `smart_title_case(text: str) -> str`.
- Produces (`identify.py`): `SongInfo(path: Path, title: str, artist: str, duration: float = 0.0, source: str = "unknown", alt_titles: list[str] = [])` dataclass with a `search_titles` property; `extract_metadata(path: Path) -> SongInfo`.
- Consumes: `requests` (Task 1), `mutagen` (Task 1), `lyricvideo.audio_decode.probe_duration` (Task 2),
  `lyricvideo.text_clean.*` (this task). Task 6 (`fetch_lyrics.py`) will also consume
  `normalize`/`artist_key` from `text_clean.py` — porting these once here, shared,
  avoids duplicating the same fuzzy-matching logic in two files.

AcoustID fingerprinting is explicitly out of scope (per spec) — this port stops at the
tags → filename → lrclib/MusicBrainz consensus tier.

- [ ] **Step 1: Write the failing tests for text_clean.py**

Create `tests/test_text_clean.py`:

```python
from lyricvideo.text_clean import artist_key, clean_title, normalize, smart_title_case


def test_clean_title_strips_official_video_noise():
    assert clean_title("Eye in the Sky (Official Video)") == "Eye in the Sky"


def test_clean_title_strips_track_number_prefix():
    assert clean_title("01. Eye in the Sky") == "Eye in the Sky"
    assert clean_title("01 - Eye in the Sky") == "Eye in the Sky"


def test_clean_title_leaves_numbers_that_are_part_of_the_title():
    assert clean_title("99 Luftballons") == "99 Luftballons"
    assert clean_title("21 Guns") == "21 Guns"


def test_normalize_strips_accents_and_punctuation():
    assert normalize("Café del Mar!") == "cafe del mar"


def test_normalize_collapses_whitespace():
    assert normalize("  Hello   World  ") == "hello world"


def test_artist_key_collapses_the_and_ampersand_variants():
    assert artist_key("The Alan Parsons Project") == artist_key("Alan Parsons Project")
    assert artist_key("Simon & Garfunkel") == artist_key("Simon and Garfunkel")


def test_smart_title_case_capitalizes_lowercase_input():
    assert smart_title_case("eye in the sky") == "Eye in the Sky"


def test_smart_title_case_leaves_mixed_case_untouched():
    assert smart_title_case("Eye IN the Sky") == "Eye IN the Sky"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_text_clean.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.text_clean'`

- [ ] **Step 3: Write text_clean.py**

Create `lyricvideo/text_clean.py` (ported from LyricChord's `utils/text.py`, keeping
only the four functions this program needs — `safe_filename`/`format_time` have no
caller here):

```python
"""Small text helpers, ported from LyricChord's utils/text.py, used for fuzzy
title/artist matching during song identification and lyric lookup."""

from __future__ import annotations

import re
import unicodedata

_PAREN_NOISE = re.compile(
    r"[\(\[\{]\s*(official|lyrics?|lyric video|audio|video|hd|hq|remaster(ed)?( \d{4})?|"
    r"live|explicit|clean|mono|stereo|visuali[sz]er|4k|1080p|from .*)[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
# A track number needs a separator ("01 - ", "01. ", "1) ") or a leading zero ("01 Song");
# a bare number followed by a space is part of the title ("99 Luftballons", "21 Guns").
_TRACK_PREFIX = re.compile(r"^\s*(?:\d{1,3}\s*[-._)\]]\s*|0\d{1,2}\s+)")
_ARTIST_STOP_WORDS = {"the", "and", "n", "feat", "featuring", "ft", "with"}
_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}


def clean_title(text: str) -> str:
    """Remove '(Official Video)'-style noise and leading track numbers."""
    text = _PAREN_NOISE.sub("", text)
    text = _TRACK_PREFIX.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -_.")
    return text


def normalize(text: str) -> str:
    """Lower-case, strip accents and punctuation. Used for fuzzy matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def artist_key(name: str) -> str:
    """Comparison key for artist names: 'The Alan Parsons Project', 'Alan Parsons
    Project', 'Simon & Garfunkel' vs 'Simon and Garfunkel' all collapse to the same
    words."""
    return " ".join(w for w in normalize(name).split() if w not in _ARTIST_STOP_WORDS)


def smart_title_case(text: str) -> str:
    """'eye in the sky' -> 'Eye in the Sky'. Leaves mixed-case input untouched."""
    if text != text.lower():
        return text
    words = text.split()
    out = []
    for i, w in enumerate(words):
        if 0 < i < len(words) - 1 and w in _SMALL_WORDS:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_text_clean.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit text_clean.py**

```bash
git add lyricvideo/text_clean.py tests/test_text_clean.py
git commit -m "Port LyricChord's shared text-cleaning/fuzzy-matching helpers"
```

- [ ] **Step 6: Write the failing tests for identify.py**

Create `tests/test_identify.py`:

```python
from pathlib import Path

import pytest

from lyricvideo.identify import (
    SongInfo,
    artist_consensus,
    extract_metadata,
    parse_filename,
    rank_musicbrainz,
)


def test_song_info_search_titles_dedupes_case_insensitively():
    info = SongInfo(path=Path("x.mp3"), title="Eye In The Sky", artist="APP",
                     alt_titles=["eye in the sky", "Sirius / Eye in the Sky"])
    assert info.search_titles == ["Eye In The Sky", "Sirius / Eye in the Sky"]


def test_song_info_search_titles_skips_blank_entries():
    info = SongInfo(path=Path("x.mp3"), title="Angie", artist="", alt_titles=["", "  "])
    assert info.search_titles == ["Angie"]


def test_parse_filename_splits_artist_and_title():
    artist, title = parse_filename("Rolling Stones - Angie")
    assert artist == "Rolling Stones"
    assert title == "Angie"


def test_parse_filename_no_separator_returns_empty_artist():
    artist, title = parse_filename("angie")
    assert artist == ""
    assert title == "angie"


def test_parse_filename_handles_track_number_prefix_style():
    artist, title = parse_filename("01. Alan Parsons Project - Eye in the Sky (Official)")
    assert "Alan Parsons Project" in artist
    assert "Eye in the Sky" in title


def test_artist_consensus_picks_the_majority_artist():
    results = [
        {"artistName": "The Alan Parsons Project", "syncedLyrics": "..."},
        {"artistName": "The Alan Parsons Project", "syncedLyrics": "..."},
        {"artistName": "Some Cover Band", "plainLyrics": "..."},
    ]
    assert artist_consensus(results) == "The Alan Parsons Project"


def test_artist_consensus_returns_none_with_no_agreement():
    # A single plain-lyrics (unsynced) record only carries weight 1 -- below the
    # min_votes=2 threshold. (A single SYNCED record carries weight 2 and would
    # legitimately pass on its own -- that's a different, correctly-answered case.)
    results = [{"artistName": "Band A", "plainLyrics": "x"}]
    assert artist_consensus(results, min_votes=2) is None


def test_rank_musicbrainz_prefers_closer_duration():
    recordings = [
        {"score": 90, "length": 300000, "artist-credit": [{"name": "Band"}], "releases": [1]},
        {"score": 90, "length": 391000, "artist-credit": [{"name": "Band"}], "releases": [1]},
    ]
    result = rank_musicbrainz(recordings, duration=390.0)
    assert result is not None
    artist, title = result
    assert artist == "Band"


def test_rank_musicbrainz_returns_none_on_empty_input():
    assert rank_musicbrainz([], duration=200.0) is None


def test_extract_metadata_falls_back_to_filename_when_no_tags(tmp_path, monkeypatch):
    # A real (silent) audio file so mutagen/probe_duration don't error out, but with
    # no ID3 tags at all, so this exercises the filename-parsing fallback path.
    import subprocess

    from lyricvideo.audio_decode import find_ffmpeg

    audio_path = tmp_path / "Rolling Stones - Angie.mp3"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "2", str(audio_path)],
        check=True,
    )
    # No network lookups should be needed since the filename already has an artist.
    monkeypatch.setattr("lyricvideo.identify.lrclib_artist_for_title", lambda title: None)
    monkeypatch.setattr("lyricvideo.identify.musicbrainz_lookup", lambda *a, **k: None)

    info = extract_metadata(audio_path)

    assert info.artist == "Rolling Stones"
    assert info.title == "Angie"
    assert info.source == "filename"
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_identify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.identify'`

- [ ] **Step 8: Write the implementation**

Create `lyricvideo/identify.py` (ported from LyricChord's `pipeline/metadata.py`,
adapted: drops the `identify_acoustid` fingerprinting tier and its `acoustid_api_key`
parameter entirely — out of scope per spec; uses `text_clean.py`'s `clean_title`/
`smart_title_case` from Step 3 above instead of a local reimplementation):

```python
"""Song identification, ported from LyricChord's pipeline/metadata.py.

Resolution order:
  1. Embedded tags (ID3 for MP3, Vorbis comments for FLAC/OGG, iTunes atoms for M4A)
  2. Filename parsing ("Artist - Title.mp3", "01. Artist - Title.mp3", ...)
  3. Title-only lookups when the artist is still unknown:
       a. lrclib search: the artist most of the lyric entries for this title agree on
       b. MusicBrainz recording search filtered by the file's duration, which also
          yields the exact recording title (e.g. "Sirius / Eye in the Sky" for the
          album edit that has an instrumental intro)

AcoustID audio-fingerprint identification (LyricChord's optional 4th tier) is
deliberately not ported -- out of scope per this program's merge design spec.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .audio_decode import probe_duration
from .text_clean import clean_title, smart_title_case

log = logging.getLogger("playalongvideoproduction")

_SEPARATORS = [" - ", " – ", " — ", "_-_", " -- ", " _ "]

LRCLIB_SEARCH = "https://lrclib.net/api/search"
MUSICBRAINZ_RECORDING = "https://musicbrainz.org/ws/2/recording/"
MB_TIMEOUT = 12
HTTP_HEADERS = {"User-Agent": "PlayAlongVideoProduction/1.0"}


@dataclass
class SongInfo:
    """Identity of a song, resolved from tags / filename / online consensus."""

    path: Path
    title: str
    artist: str
    duration: float = 0.0
    source: str = "unknown"  # "id3" | "filename" | "musicbrainz" | "lrclib"
    alt_titles: list[str] = field(default_factory=list)

    @property
    def search_titles(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in [self.title, *self.alt_titles]:
            key = t.strip().lower()
            if t.strip() and key not in seen:
                seen.add(key)
                out.append(t.strip())
        return out


def parse_filename(stem: str) -> tuple[str, str]:
    """Guess (artist, title) from a filename stem. Artist is '' when unknown."""
    text = clean_title(stem)
    for sep in _SEPARATORS:
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            if len(parts) >= 2:
                artist = parts[0]
                title = clean_title(" - ".join(parts[1:]))
                return artist, title
    return "", text.replace("_", " ").strip()


def read_tags(path: Path) -> tuple[str, str, str, float]:
    """Return (title, artist, album, duration_seconds) from embedded tags."""
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except ImportError:  # pragma: no cover
        log.warning("mutagen is not installed; tag reading disabled")
        return "", "", "", 0.0

    try:
        audio = MutagenFile(str(path), easy=True)
    except Exception as exc:
        log.debug("mutagen could not open %s: %s", path.name, exc)
        return "", "", "", 0.0
    if audio is None:
        return "", "", "", 0.0

    def first(key: str) -> str:
        if not audio.tags:
            return ""
        value = audio.tags.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "").strip()

    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    return first("title"), first("artist"), first("album"), duration


def artist_consensus(results: list[dict], min_votes: int = 2, min_share: float = 0.4) -> str | None:
    """Pick the artist most lrclib entries for a title agree on."""
    votes: Counter = Counter()
    display: dict[str, Counter] = defaultdict(Counter)
    for item in results:
        if not isinstance(item, dict):
            continue
        name = str(item.get("artistName") or "").strip()
        if not name or not (item.get("syncedLyrics") or item.get("plainLyrics")):
            continue
        key = name.strip().lower()
        weight = 2 if item.get("syncedLyrics") else 1
        votes[key] += weight
        display[key][name] += 1
    if not votes:
        return None
    key, best = votes.most_common(1)[0]
    if best < min_votes or best / sum(votes.values()) < min_share:
        return None
    return display[key].most_common(1)[0][0]


def lrclib_artist_for_title(title: str) -> str | None:
    try:
        r = requests.get(LRCLIB_SEARCH, params={"track_name": title}, headers=HTTP_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        return artist_consensus(data) if isinstance(data, list) else None
    except (requests.RequestException, ValueError) as exc:
        log.debug("lrclib artist lookup failed: %s", exc)
        return None


def _artist_credit(rec: dict) -> str:
    parts = []
    for credit in rec.get("artist-credit", []) or []:
        if isinstance(credit, dict):
            parts.append(str(credit.get("name", "")) + str(credit.get("joinphrase", "") or ""))
        elif isinstance(credit, str):
            parts.append(credit)
    return "".join(parts).strip()


def rank_musicbrainz(recordings: list[dict], duration: float, artist_hint: str = "") -> tuple[str, str] | None:
    """Choose (artist, title) from MusicBrainz search results."""
    hint = artist_hint.strip().lower() if artist_hint else ""
    best, best_score = None, float("-inf")
    for rec in recordings:
        length = (rec.get("length") or 0) / 1000.0
        if not length:
            continue
        delta = abs(length - duration)
        artist = _artist_credit(rec)
        score = (0.5 * float(rec.get("score") or 0)
                 - 4.0 * max(0.0, delta - 2.0)
                 + 6.0 * min(len(rec.get("releases") or []), 8))
        if hint and hint in artist.lower():
            score += 40.0
        if score > best_score and artist:
            best, best_score = (artist, str(rec.get("title") or "")), score
    return best


def musicbrainz_lookup(title: str, duration: float, artist_hint: str = "") -> tuple[str, str] | None:
    """Find the recording matching `title` and `duration` (within a small window)."""
    if not title or duration <= 0:
        return None
    window = max(6.0, duration * 0.02)
    lo, hi = int((duration - window) * 1000), int((duration + window) * 1000)
    q = f'recording:"{title.replace(chr(34), " ")}" AND dur:[{lo} TO {hi}]'
    try:
        r = requests.get(MUSICBRAINZ_RECORDING, params={"query": q, "fmt": "json", "limit": 15},
                         headers=HTTP_HEADERS, timeout=MB_TIMEOUT)
        if r.status_code != 200:
            log.info("MusicBrainz answered HTTP %s; skipping", r.status_code)
            return None
        return rank_musicbrainz(r.json().get("recordings", []) or [], duration, artist_hint)
    except (requests.RequestException, ValueError) as exc:
        log.info("MusicBrainz lookup failed: %s", exc)
        return None


def extract_metadata(path: Path) -> SongInfo:
    """Resolve title/artist/duration for one audio file."""
    title, artist, _album, duration = read_tags(path)
    source = "id3" if (title and artist) else ""
    alt_titles: list[str] = []

    if not title or not artist:
        f_artist, f_title = parse_filename(path.stem)
        title = title or f_title
        artist = artist or f_artist
        source = source or "filename"
    if duration <= 0:
        duration = probe_duration(path)
    title = clean_title(title) or path.stem

    if not artist and title:
        log.info("No artist in tags or filename; looking '%s' up by title and length", title)
        hint = lrclib_artist_for_title(title)
        hit = musicbrainz_lookup(title, duration, artist_hint=hint or "")
        if hit:
            artist, mb_title = hit
            if mb_title and mb_title.strip().lower() != title.strip().lower():
                alt_titles.append(title)
                title = mb_title
            source = "musicbrainz"
        elif hint:
            artist, source = hint, "lrclib"

    title = clean_title(title) or path.stem
    if source == "filename":
        title = smart_title_case(title)
    info = SongInfo(path=path, title=title, artist=artist.strip(), duration=duration,
                    source=source or "filename", alt_titles=alt_titles)
    log.info("Identified '%s' by '%s' (%s, %.0fs)", info.title, info.artist or "Unknown Artist",
             info.source, info.duration)
    return info
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_identify.py -v`
Expected: 10 passed

- [ ] **Step 10: Commit**

```bash
git add lyricvideo/identify.py tests/test_identify.py
git commit -m "Port LyricChord's song-identification module (identify.py)"
```

---

## Task 6: Port vocal-onset disambiguation and lyric fetching

**Files:**
- Create: `lyricvideo/vocal_onset.py`
- Create: `lyricvideo/fetch_lyrics.py`
- Test: `tests/test_vocal_onset.py`
- Test: `tests/test_fetch_lyrics.py`

**Interfaces:**
- Produces (`vocal_onset.py`): `vocal_onset_rise(path: Path, times: list[float], before: float = 1.5, after: float = 1.5) -> list[float]`.
- Produces (`fetch_lyrics.py`): `fetch_lyric_lines(audio_path: Path, title: str, artist: str, duration: float, alt_titles: list[str] | None = None) -> list[str]` — the only function `pipeline.py` (Task 8) calls. Also exports `choose_lyrics_candidate`, `parse_lrc`, `plain_to_lines`, `title_variants`, `artist_matches` for direct unit testing.
- Consumes: `lyricvideo.audio_decode.decode_audio` (Task 2), `lyricvideo.text_clean.normalize`/`artist_key` (Task 5), `requests`/`syncedlyrics` (Task 1).

**Simplification versus LyricChord's original `lyrics.py`:** enhanced-LRC word-level
timestamps (`<mm:ss.xx>word` tags, the `LyricWord` class) are dropped entirely — this
program never uses them, since real per-word timing always comes from its own forced
alignment (`align.py`) regardless of what a lyrics provider's LRC carries. The internal
line representation only needs `(start, end, text)`.

- [ ] **Step 1: Write the failing tests for vocal_onset.py**

Create `tests/test_vocal_onset.py`:

```python
import subprocess

from lyricvideo.audio_decode import find_ffmpeg
from lyricvideo.vocal_onset import vocal_onset_rise


def test_vocal_onset_rise_returns_one_score_per_candidate(tmp_path):
    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=10", "-ar", "22050", str(wav_path)],
        check=True,
    )

    rises = vocal_onset_rise(wav_path, times=[2.0, 5.0, 8.0])

    assert len(rises) == 3
    assert all(isinstance(r, float) for r in rises)


def test_vocal_onset_rise_empty_times_returns_empty_list(tmp_path):
    wav_path = tmp_path / "tone.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=3", str(wav_path)],
        check=True,
    )

    assert vocal_onset_rise(wav_path, times=[]) == []


def test_vocal_onset_rise_never_returns_nan(tmp_path):
    import math

    wav_path = tmp_path / "silence.wav"
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "5", str(wav_path)],
        check=True,
    )

    rises = vocal_onset_rise(wav_path, times=[1.0, 3.0])

    assert all(math.isfinite(r) for r in rises)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vocal_onset.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.vocal_onset'`

- [ ] **Step 3: Write vocal_onset.py**

Create `lyricvideo/vocal_onset.py` (ported from LyricChord's `pipeline/vocal.py`):

```python
"""Narrow-window vocal-entry check, ported from LyricChord's pipeline/vocal.py.

A global "where do the vocals start" detector is unreliable on a full mix, but a much
smaller question is tractable: given a few candidate times a few seconds apart (lyric
records that disagree about the first line), at which one does singing actually begin?
The true entry shows a sustained rise in harmonic energy in the vocal band; guitar
fills and drum hits at the wrong candidates usually do not.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .audio_decode import decode_audio

SR = 22050
HOP = 256
N_FFT = 2048
VOCAL_BAND = (300.0, 3500.0)   # Hz; fundamentals and lower formants of sung voice
WINDOW_PAD = 2.0               # seconds decoded beyond the candidate range on each side


def vocal_onset_rise(path: Path, times: list[float], before: float = 1.5, after: float = 1.5) -> list[float]:
    """For each candidate time, return (mean vocal-band energy just after) - (just before).

    Larger is more consistent with a voice entering at that moment. Energies are
    normalised to the window's peak so the values are comparable between candidates.
    Candidates outside the decoded audio score 0.0 (never NaN).
    """
    if not times:
        return []
    import librosa

    lo = max(0.0, min(times) - before - WINDOW_PAD)
    hi = max(times) + after + WINDOW_PAD
    seg = decode_audio(path, SR, start=lo, duration=hi - lo)
    if seg.size < SR:
        return [0.0] * len(times)

    harmonic = librosa.effects.harmonic(y=seg, margin=3.0)
    spec = np.abs(librosa.stft(y=harmonic, n_fft=N_FFT, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=SR, n_fft=N_FFT)
    energy = spec[(freqs >= VOCAL_BAND[0]) & (freqs <= VOCAL_BAND[1])].sum(axis=0)
    energy = energy / (energy.max() + 1e-9)
    fps = SR / HOP
    n = energy.size

    def mean(a: float, b: float) -> float:
        i = min(n, max(0, int((a - lo) * fps)))
        j = min(n, max(0, int((b - lo) * fps)))
        return float(energy[i:j].mean()) if j > i else 0.0

    rises = [mean(t, t + after) - mean(t - before, t) for t in times]
    return [r if np.isfinite(r) else 0.0 for r in rises]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vocal_onset.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit vocal_onset.py**

```bash
git add lyricvideo/vocal_onset.py tests/test_vocal_onset.py
git commit -m "Port LyricChord's vocal-onset first-line disambiguation"
```

- [ ] **Step 6: Write the failing tests for fetch_lyrics.py**

Create `tests/test_fetch_lyrics.py`:

```python
from pathlib import Path

from lyricvideo.fetch_lyrics import (
    _LyricLine,
    artist_matches,
    choose_lyrics_candidate,
    fetch_lyric_lines,
    parse_lrc,
    plain_to_lines,
    title_variants,
)


def test_parse_lrc_extracts_timed_lines():
    text = "[00:01.00]Hello darkness\n[00:03.50]My old friend\n"
    lines = parse_lrc(text, duration=10.0)
    assert [l.text for l in lines] == ["Hello darkness", "My old friend"]
    assert lines[0].start == 1.0
    assert lines[1].start == 3.5


def test_parse_lrc_multiple_timestamps_per_line():
    text = "[00:01.00][00:05.00]Chorus line\n"
    lines = parse_lrc(text, duration=10.0)
    assert [l.start for l in lines] == [1.0, 5.0]


def test_parse_lrc_last_line_ends_at_duration_when_within_max_hold():
    text = "[00:01.00]Only line\n"
    lines = parse_lrc(text, duration=8.0)
    assert lines[0].end == 8.0


def test_parse_lrc_last_line_capped_at_max_line_hold_even_with_more_duration_left():
    # A real design choice (unchanged from LyricChord): no single line lingers on
    # screen forever just because it's the last one and the song still has a long
    # tail -- it's capped at MAX_LINE_HOLD (10.0s) seconds after its own start.
    text = "[00:01.00]Only line\n"
    lines = parse_lrc(text, duration=20.0)
    assert lines[0].end == 11.0


def test_plain_to_lines_spreads_evenly_between_intro_and_outro():
    text = "one\ntwo\nthree\n"
    lines = plain_to_lines(text, duration=100.0)
    assert [l.text for l in lines] == ["one", "two", "three"]
    assert lines[0].start == 8.0        # duration * 0.08
    assert lines[-1].end == 94.0        # duration * 0.94


def test_plain_to_lines_empty_text_returns_empty():
    assert plain_to_lines("", duration=100.0) == []


def test_title_variants_splits_on_slash():
    variants = title_variants("Sirius / Eye in the Sky")
    assert "Sirius / Eye in the Sky" in variants
    assert "Sirius Eye in the Sky" in variants


def test_title_variants_dedupes_case_insensitively():
    variants = title_variants("Angie")
    assert variants.count("Angie") == 1


def test_artist_matches_handles_the_prefix():
    assert artist_matches("The Alan Parsons Project", "Alan Parsons Project") is True


def test_artist_matches_false_for_unrelated_artists():
    assert artist_matches("Rolling Stones", "The Beatles") is False


def test_artist_matches_empty_wanted_always_matches():
    assert artist_matches("Anyone", "") is True


def test_choose_lyrics_candidate_prefers_the_larger_matching_cluster():
    items = [
        {"id": 1, "syncedLyrics": "[00:00.00]a\n[00:05.00]b\n", "duration": 100},
        {"id": 2, "syncedLyrics": "[00:00.00]a\n[00:05.00]b\n", "duration": 100},
        {"id": 3, "syncedLyrics": "[00:10.00]a\n[00:15.00]b\n", "duration": 100},
    ]
    chosen, note = choose_lyrics_candidate(items, file_duration=100.0)
    assert chosen is not None
    assert chosen["id"] in (1, 2)  # the 2-record cluster outvotes the lone record
    assert "2 of 3" in note


def test_choose_lyrics_candidate_returns_none_note_when_nothing_usable():
    chosen, note = choose_lyrics_candidate([], file_duration=100.0)
    assert chosen is None
    assert note == "no usable records"


def test_fetch_lyric_lines_uses_sidecar_lrc_before_any_network_call(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")  # never actually decoded; sidecar wins first
    (tmp_path / "song.lrc").write_text(
        "[00:00.00]Hello darkness\n[00:03.00]My old friend\n", encoding="utf-8",
    )

    def fail_if_called(*a, **k):
        raise AssertionError("network lookup should not run when a sidecar exists")

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", fail_if_called)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", fail_if_called)

    lines = fetch_lyric_lines(audio_path, title="Anything", artist="Anyone", duration=10.0)

    assert lines == ["Hello darkness", "My old friend"]


def test_fetch_lyric_lines_returns_empty_when_nothing_found(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")

    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", lambda *a, **k: None)

    assert fetch_lyric_lines(audio_path, title="Unknown", artist="Unknown", duration=10.0) == []
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fetch_lyrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lyricvideo.fetch_lyrics'`

- [ ] **Step 8: Write the implementation**

Create `lyricvideo/fetch_lyrics.py` (ported from LyricChord's `pipeline/lyrics.py`,
simplified as noted above — no enhanced-LRC word tags, no `LyricWord`, and the public
entry point returns plain `list[str]` rather than a `Lyrics` dataclass):

```python
"""Lyrics retrieval and LRC parsing, ported from LyricChord's pipeline/lyrics.py.

Provider order:
  1. Sidecar file next to the audio: "<name>.lrc" (synced) or "<name>.txt" (plain)
  2. lrclib.net - free, no API key, returns synced + plain lyrics
  3. syncedlyrics - aggregates Musixmatch / NetEase / Megalobiz / lrclib

Synced lyrics are only correct for the *edition* they were timed against, so lrclib
results are gathered for every known title variant and ranked by how closely their
reference duration matches this file. Real per-word/per-line TIMING always comes from
this program's own forced alignment (align.py) -- only line TEXT is ever kept from
whatever provider answers here.
"""

from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .text_clean import artist_key, normalize
from .vocal_onset import vocal_onset_rise

log = logging.getLogger("playalongvideoproduction")

LRC_TAG = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
META_TAG = re.compile(r"^\[([a-zA-Z]+):([^\]]*)\]$")

MAX_LINE_HOLD = 10.0
LRCLIB_BASE = "https://lrclib.net/api"
LRCLIB_PARALLELISM = 6
DURATION_TOLERANCE = 8.0
HTTP_HEADERS = {"User-Agent": "PlayAlongVideoProduction/1.0"}

# A provider hit: (lyrics text, synced?, reference duration in seconds or 0).
# Empty text with synced=True means "the provider says this track is instrumental".
Hit = tuple[str, bool, float]


@dataclass
class _LyricLine:
    start: float
    end: float
    text: str


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _tag_seconds(m: "re.Match[str]") -> float:
    mins, secs, frac = m.group(1), m.group(2), m.group(3) or "0"
    return int(mins) * 60 + int(secs) + int(frac) / (10 ** len(frac))


def parse_lrc(text: str, duration: float = 0.0) -> list[_LyricLine]:
    """Parse LRC text into timed lines. Supports several timestamps per line and the
    standard `[offset:+/-ms]` header (positive values make the lyrics appear earlier)."""
    entries: list[tuple[float, str]] = []
    offset = 0.0
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        meta = META_TAG.match(raw)
        if meta:
            if meta.group(1).lower() == "offset":
                try:
                    offset = int(meta.group(2).strip().replace("+", "")) / 1000.0
                except ValueError:
                    pass
            continue
        tags = []
        pos = 0
        while True:
            m = LRC_TAG.match(raw, pos)
            if not m:
                break
            tags.append(m)
            pos = m.end()
        if not tags:
            continue
        body = raw[pos:].strip()
        for tag in tags:
            entries.append((_tag_seconds(tag), body))

    entries.sort(key=lambda e: e[0])
    lines: list[_LyricLine] = []
    for i, (start, text_) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else (duration if duration > start else start + MAX_LINE_HOLD)
        if text_:
            end = min(end, start + MAX_LINE_HOLD)
        lines.append(_LyricLine(start=max(0.0, start - offset), end=max(start, end) - offset, text=text_))
    return lines


def plain_to_lines(text: str, duration: float) -> list[_LyricLine]:
    """Spread untimed lyric lines evenly between an assumed intro and outro."""
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    if not rows or duration <= 0:
        return []
    start, end = duration * 0.08, duration * 0.94
    step = (end - start) / len(rows)
    return [_LyricLine(start + i * step, start + (i + 1) * step, row) for i, row in enumerate(rows)]


def has_lyrics_sidecar(path: Path) -> bool:
    return path.with_suffix(".lrc").exists() or path.with_suffix(".txt").exists()


def _sidecar(path: Path) -> Hit | None:
    lrc = path.with_suffix(".lrc")
    if lrc.exists():
        content = lrc.read_text(encoding="utf-8", errors="ignore")
        return (content, True, 0.0) if content.strip() else None
    txt = path.with_suffix(".txt")
    if txt.exists():
        content = txt.read_text(encoding="utf-8", errors="ignore")
        if content.strip():
            return content, bool(LRC_TAG.search(content)), 0.0
    return None


def score_lyrics_candidate(item: dict, file_duration: float) -> float:
    """Rank an lrclib record: synced beats plain, closer reference duration beats farther."""
    if item.get("syncedLyrics"):
        base = 2.0
    elif item.get("plainLyrics"):
        base = 1.0
    else:
        return -1.0
    ref = _num(item.get("duration"))
    if file_duration and ref:
        delta = abs(ref - file_duration)
        base -= min(0.9, delta / 120.0) if delta > 2.0 else 0.0
    return base


CLUSTER_TOLERANCE = 6.0
FOREIGN_EDITION_GAP = 20.0


@dataclass
class _Timeline:
    item: dict
    ref: float
    first: float
    last: float
    n_lines: int


def _timeline(item: dict) -> _Timeline | None:
    if not isinstance(item.get("syncedLyrics"), str) or not item["syncedLyrics"]:
        return None
    ref = _num(item.get("duration"))
    lines = [l for l in parse_lrc(item["syncedLyrics"], ref) if l.text.strip()]
    if not lines:
        return None
    return _Timeline(item, ref, lines[0].start, lines[-1].start, len(lines))


def _same_start(a: _Timeline, b: _Timeline) -> bool:
    return abs(a.first - b.first) <= CLUSTER_TOLERANCE


def _same_timeline(a: _Timeline, b: _Timeline) -> bool:
    return abs(a.first - b.first) <= CLUSTER_TOLERANCE and abs(a.last - b.last) <= 2 * CLUSTER_TOLERANCE


def _copied_from_other_edition(t: _Timeline, matching: list[_Timeline], foreign: list[_Timeline]) -> bool:
    foreign_sharers = sum(1 for f in foreign if _same_timeline(f, t))
    matching_sharers = sum(1 for m in matching if _same_timeline(m, t))
    return foreign_sharers >= max(2, matching_sharers)


OnsetScorer = Callable[[list[float]], list[float]]
ONSET_DISAGREEMENT = 0.5


def choose_lyrics_candidate(items: list[dict], file_duration: float,
                            tolerance: float = DURATION_TOLERANCE,
                            onset_scorer: OnsetScorer | None = None) -> tuple[dict | None, str]:
    """Pick the lrclib record whose timeline most plausibly belongs to this file's
    edition. See module docstring; logic ported unchanged from LyricChord."""
    timelines = [t for t in (_timeline(it) for it in items if isinstance(it, dict)) if t]
    if file_duration > 0:
        matching = [t for t in timelines if abs(t.ref - file_duration) <= tolerance]
        foreign = [t for t in timelines if abs(t.ref - file_duration) > FOREIGN_EDITION_GAP]
    else:
        matching, foreign = timelines, []

    if matching:
        copied_flags = [_copied_from_other_edition(t, matching, foreign) for t in matching]
        if all(copied_flags):
            copied_flags = [False] * len(matching)
        best, best_score, best_note = None, float("-inf"), ""
        scored: list[tuple[float, _Timeline]] = []
        for t, copied in zip(matching, copied_flags):
            cluster = [o for o in matching if _same_start(o, t)]
            median_first = sorted(o.first for o in cluster)[len(cluster) // 2]
            score = float(len(cluster))
            if copied:
                score -= 10.0
            tail = (file_duration - t.last) / file_duration if file_duration > 0 else 0.0
            score -= 3.0 * max(0.0, tail - 0.3)
            score += 0.02 * t.n_lines
            score -= 0.1 * abs(t.first - median_first)
            scored.append((score, t))
            if score > best_score:
                best, best_score = t, score
                best_note = (f"{len(cluster)} of {len(matching)} matching-length records agree on this start"
                             + ("; timing copied from another edition" if copied else ""))
        assert best is not None

        if onset_scorer is not None:
            peers = sorted(((s, t) for s, t in scored if _same_start(t, best) and s >= best_score - 1.5),
                           key=lambda st: st[1].first)
            groups: list[list[tuple[float, _Timeline]]] = []
            for s, t in peers:
                if groups and t.first - groups[-1][0][1].first <= ONSET_DISAGREEMENT:
                    groups[-1].append((s, t))
                else:
                    groups.append([(s, t)])
            if len(groups) > 1:
                rep_times = [sorted(t.first for _, t in g)[len(g) // 2] for g in groups]
                try:
                    rises = onset_scorer(rep_times)
                except Exception as exc:
                    log.debug("vocal onset check failed: %s", exc)
                    rises = []
                if len(rises) == len(groups) and all(math.isfinite(r) for r in rises):
                    own = next(i for i, g in enumerate(groups) if any(t is best for _, t in g))
                    idx = max(range(len(groups)), key=lambda i: (round(rises[i], 3), i == own))
                    chosen = max(groups[idx], key=lambda st: st[0])[1]
                    if chosen is not best:
                        best_note += f"; audio places the first line at {chosen.first:.1f}s rather than {best.first:.1f}s"
                        best = chosen
        return best.item, best_note

    ranked = sorted((it for it in items if isinstance(it, dict)),
                    key=lambda it: score_lyrics_candidate(it, file_duration), reverse=True)
    if ranked and score_lyrics_candidate(ranked[0], file_duration) >= 0:
        return ranked[0], "no record matches this file's length; using the closest edition"
    return None, "no usable records"


def _pick_lrclib(item: dict) -> Hit | None:
    ref = _num(item.get("duration"))
    if item.get("syncedLyrics"):
        return item["syncedLyrics"], True, ref
    if item.get("plainLyrics"):
        return item["plainLyrics"], False, ref
    if item.get("instrumental"):
        return "", True, ref
    return None


_TITLE_SEPARATOR = re.compile(r"\s*[/|&+,]\s*|\s+-\s+")


def title_variants(title: str, limit: int = 4) -> list[str]:
    """Spellings uploaders use for the same track."""
    out: list[str] = []

    def add(t: str) -> None:
        t = t.strip()
        if t and t.lower() not in {o.lower() for o in out}:
            out.append(t)

    add(title)
    parts = [p for p in _TITLE_SEPARATOR.split(title) if p.strip()]
    if len(parts) > 1:
        add(" ".join(parts))
        add("/".join(parts))
        add(" - ".join(parts))
    add(normalize(title))
    return out[:limit]


def artist_matches(record_artist: str, wanted: str) -> bool:
    if not wanted:
        return True
    a, b = artist_key(record_artist or ""), artist_key(wanted)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _lrclib_get(params: dict) -> dict | None:
    r = requests.get(f"{LRCLIB_BASE}/get", params=params, headers=HTTP_HEADERS, timeout=15)
    return r.json() if r.status_code == 200 and isinstance(r.json(), dict) else None


def _lrclib_search(params: dict) -> list[dict]:
    r = requests.get(f"{LRCLIB_BASE}/search", params=params, headers=HTTP_HEADERS, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _fetch_lrclib_hit(audio_path: Path, title: str, artist: str, duration: float,
                      alt_titles: list[str]) -> Hit | None:
    """Query lrclib.net and choose among all records by edition consensus."""
    titles = [t for t in [title, *alt_titles] if t.strip()]
    if not titles:
        return None

    jobs: list[tuple[Callable[[dict], object], dict]] = []
    if artist and duration > 0:
        for t in titles:
            jobs.append((_lrclib_get, {"artist_name": artist, "track_name": t, "duration": int(round(duration))}))
    queries: list[dict] = []
    for t in titles:
        for k, variant in enumerate(title_variants(t)):
            if k == 0:
                queries.append({"track_name": variant})
            queries.append({"q": variant})
            if artist:
                if k == 0:
                    queries.append({"track_name": variant, "artist_name": artist})
                queries.append({"q": f"{artist} {variant}"})
    seen_queries = set()
    for params in queries[:16]:
        key = tuple(sorted(params.items()))
        if key not in seen_queries:
            seen_queries.add(key)
            jobs.append((_lrclib_search, params))

    def run(job: tuple[Callable[[dict], object], dict]) -> tuple[dict, object, Exception | None]:
        fn, params = job
        try:
            return params, fn(params), None
        except (requests.RequestException, ValueError) as exc:
            return params, None, exc

    candidates: dict[object, dict] = {}
    with ThreadPoolExecutor(max_workers=LRCLIB_PARALLELISM) as pool:
        for params, result, exc in pool.map(run, jobs):
            if exc is not None:
                log.warning("lrclib request failed (%s): %s", params, exc)
                continue
            for item in (result if isinstance(result, list) else [result] if result else []):
                if isinstance(item, dict):
                    candidates.setdefault(item.get("id", id(item)), item)

    if artist:
        by_artist = {k: v for k, v in candidates.items()
                     if artist_matches(str(v.get("artistName") or ""), artist)}
        if by_artist:
            candidates = by_artist
    if not candidates:
        return None

    scorer: OnsetScorer | None = None
    if audio_path.is_file():
        scorer = lambda times: vocal_onset_rise(audio_path, times)  # noqa: E731
    chosen, note = choose_lyrics_candidate(list(candidates.values()), duration, onset_scorer=scorer)
    if chosen is not None:
        log.info("lrclib: record #%s chosen (%s)", chosen.get("id", "?"), note)
        return _pick_lrclib(chosen)
    for item in candidates.values():
        ref = _num(item.get("duration"))
        if item.get("instrumental") and (not duration or abs(ref - duration) <= DURATION_TOLERANCE):
            return "", True, ref
    return None


def _fetch_syncedlyrics_hit(title: str, artist: str) -> Hit | None:
    try:
        import syncedlyrics  # type: ignore
    except ImportError:
        return None
    term = f"{artist} {title}".strip()
    try:
        try:
            lrc = syncedlyrics.search(term, allow_plain_format=True)
        except TypeError:
            lrc = syncedlyrics.search(term)
    except Exception as exc:
        log.warning("syncedlyrics failed: %s", exc)
        return None
    if not lrc:
        return None
    return lrc, bool(LRC_TAG.search(lrc)), 0.0


def fetch_lyric_lines(audio_path: Path, title: str, artist: str, duration: float,
                      alt_titles: list[str] | None = None) -> list[str]:
    """Best-effort plain lyric-line text for a song: sidecar .lrc/.txt -> lrclib ->
    syncedlyrics. Returns [] if nothing usable was found anywhere -- never fabricates
    lyrics. Only line TEXT is returned; timing always comes from this program's own
    forced alignment, never from whatever timestamps a provider's LRC carries."""
    hit = _sidecar(audio_path)
    if hit is None:
        hit = _fetch_lrclib_hit(audio_path, title, artist, duration, alt_titles or [])
    if hit is None:
        hit = _fetch_syncedlyrics_hit(title, artist)
    if hit is None:
        log.warning("No lyrics found for '%s' - '%s'", artist, title)
        return []

    text, synced, _ref_duration = hit
    if synced and not text.strip():
        log.info("Track flagged instrumental by lyrics provider")
        return []
    lines = parse_lrc(text, duration) if synced else plain_to_lines(text, duration)
    return [l.text for l in lines if l.text.strip()]
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fetch_lyrics.py -v`
Expected: 14 passed

- [ ] **Step 10: Commit fetch_lyrics.py**

```bash
git add lyricvideo/fetch_lyrics.py tests/test_fetch_lyrics.py
git commit -m "Port LyricChord's lyric-fetching module (fetch_lyrics.py)"
```

---

## Task 7: The cutover — models, combine, layout, render, assemble, pipeline

This is one coherent, interdependent change: `ChordWord`/`InstrumentalChord`/
`InstrumentalBlock` are retired everywhere at once, `ChordEvent`/`ChordTrack` (Task 4's
standalone versions move here into `models.py`, their permanent home) become the one
source of chord data, and every consumer (`layout.py`, `render.py`, `assemble.py`,
`pipeline.py`) is updated together. Splitting this across separate commits would leave
the codebase failing its own tests in between, so this task makes several focused
edits and commits, but does not consider itself done — and does not merge into a
clean state other tasks can build on — until the full suite passes at the end.

**Files:**
- Modify: `lyricvideo/models.py`
- Modify: `tests/test_models.py`
- Modify: `lyricvideo/combine.py`
- Modify: `tests/test_combine.py`
- Modify: `lyricvideo/layout.py`
- Modify: `tests/test_layout.py`
- Modify: `lyricvideo/render.py`
- Modify: `tests/test_render.py`
- Modify: `lyricvideo/assemble.py`
- Modify: `tests/test_assemble.py`
- Modify: `lyricvideo/pipeline.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `lyricvideo.detect_chords.detect_chords` (Task 4, though its `ChordEvent`/
  `ChordTrack` classes are superseded by the ones this task adds to `models.py` — see
  Step 3), `lyricvideo.identify.extract_metadata` (Task 5), `lyricvideo.fetch_lyrics.fetch_lyric_lines` (Task 6).
- Produces: `Word(word, start_time, end_time)` (replaces `ChordWord`, drops the `.chord`
  field), `ChordEvent`/`ChordTrack`/`current_chord_at`/`next_chord_after` now live in
  `models.py`, `Song.chord_track: ChordTrack` (replaces `Song.instrumental_chords`),
  `build_scene(lines, t, chord_track, window=1, audio_duration=None)` (drops
  `instrumental_chords` param), `draw_scene`/`draw_chord_bar` in `render.py`,
  `assemble_video(lines, chord_track, image_dir, audio_path, out_path, font_path, ...)`,
  `pipeline.STAGES = ["identify", "separate", "fetch_lyrics", "align", "detect_chords",
  "images", "render"]`, `run_pipeline(audio_path, work_dir, title=None, start_stage="identify", font_path=None, progress_callback=None) -> Path`.

### Step 1: models.py — write the failing tests

Modify `tests/test_models.py` to its complete new content:

```python
import json
from pathlib import Path

from lyricvideo.models import (
    ChordEvent,
    ChordTrack,
    LyricLine,
    Song,
    Word,
    current_chord_at,
    line_hash,
    load_song,
    next_chord_after,
    save_song,
)


def test_lyric_line_text_joins_words():
    line = LyricLine(words=[Word(word="hello"), Word(word="there")])
    assert line.text == "hello there"


def test_save_and_load_song_round_trip(tmp_path):
    song = Song(
        title="Test Song",
        audio_path="audio.mp3",
        vocal_stem_path="vocals.wav",
        lines=[
            LyricLine(
                words=[Word(word="hi", start_time=0.0, end_time=0.5)],
                start_time=0.0,
                end_time=0.5,
            )
        ],
        instrumental_stem_path="no_vocals.wav",
        chord_track=ChordTrack(events=[ChordEvent(start=0.0, end=2.5, label="Em7")], key="E minor", bpm=90.0),
        image_cache={"abc123": "images/abc123.png"},
    )
    path = tmp_path / "song.json"

    save_song(song, path)
    restored = load_song(path)

    assert restored == song


def test_save_and_load_song_with_no_chord_track_defaults_empty(tmp_path):
    song = Song(title="No Chords Yet", audio_path="audio.mp3")
    path = tmp_path / "song.json"

    save_song(song, path)
    restored = load_song(path)

    assert restored.chord_track == ChordTrack()


def test_line_hash_stable_and_case_insensitive():
    assert line_hash("Hello There") == line_hash("hello there")
    assert line_hash("Hello There") != line_hash("Something else")


def test_current_chord_at_finds_covering_event():
    track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 5.0, "G")])
    assert current_chord_at(track, 1.0).label == "C"
    assert current_chord_at(track, 2.5).label == "G"


def test_current_chord_at_returns_none_outside_track():
    track = ChordTrack(events=[ChordEvent(1.0, 2.0, "C")])
    assert current_chord_at(track, 0.5) is None
    assert current_chord_at(track, 2.5) is None


def test_current_chord_at_empty_track_returns_none():
    assert current_chord_at(ChordTrack(), 1.0) is None


def test_next_chord_after_finds_next_different_label():
    track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "C"), ChordEvent(4.0, 6.0, "G")])
    # A merged-boundary duplicate "C" segment must not be reported as "next".
    result = next_chord_after(track, 0.5)
    assert result.label == "G"
    assert result.start == 4.0


def test_next_chord_after_returns_none_at_end_of_track():
    track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")])
    assert next_chord_after(track, 1.0) is None
```

### Step 2: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'Word' from 'lyricvideo.models'`
(and similar for `ChordEvent`/`current_chord_at`/`next_chord_after`).

### Step 3: Write models.py's new content

Replace `lyricvideo/models.py` in full:

```python
from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Word:
    word: str
    start_time: float | None = None
    end_time: float | None = None


@dataclass
class LyricLine:
    words: list[Word] = field(default_factory=list)
    start_time: float | None = None
    end_time: float | None = None

    @property
    def text(self) -> str:
        return " ".join(w.word for w in self.words)


@dataclass
class ChordEvent:
    """A chord held from `start` to `end` (seconds). Label like 'Am', 'F#', 'N'."""

    start: float
    end: float
    label: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ChordTrack:
    """Timeline of chords plus global musical info for one song, produced by
    detect_chords() and otherwise independent of the lyric lines entirely."""

    events: list[ChordEvent] = field(default_factory=list)
    key: str = ""
    bpm: float = 0.0


def current_chord_at(track: ChordTrack, t: float) -> ChordEvent | None:
    """The chord event covering time t, or None (before the first event, after the
    last, or an empty track). O(log n) via bisect against events sorted by start
    time (detect_chords always emits them in order)."""
    starts = [e.start for e in track.events]
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        return None
    event = track.events[i]
    return event if t < event.end else None


def next_chord_after(track: ChordTrack, t: float) -> ChordEvent | None:
    """The first chord event starting after t whose label differs from whatever is
    current at t, so a merged-boundary duplicate segment is never reported as "next"."""
    current = current_chord_at(track, t)
    for event in track.events:
        if event.start > t and (current is None or event.label != current.label):
            return event
    return None


@dataclass
class Song:
    title: str
    audio_path: str
    vocal_stem_path: str | None = None
    instrumental_stem_path: str | None = None
    lines: list[LyricLine] = field(default_factory=list)
    chord_track: ChordTrack = field(default_factory=ChordTrack)
    image_cache: dict[str, str] = field(default_factory=dict)


def line_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _song_to_dict(song: Song) -> dict:
    return asdict(song)


def _song_from_dict(data: dict) -> Song:
    lines = [
        LyricLine(
            words=[Word(**w) for w in ln["words"]],
            start_time=ln.get("start_time"),
            end_time=ln.get("end_time"),
        )
        for ln in data.get("lines", [])
    ]
    chord_data = data.get("chord_track") or {}
    chord_track = ChordTrack(
        events=[ChordEvent(**e) for e in chord_data.get("events", [])],
        key=chord_data.get("key", ""),
        bpm=chord_data.get("bpm", 0.0),
    )
    return Song(
        title=data["title"],
        audio_path=data["audio_path"],
        vocal_stem_path=data.get("vocal_stem_path"),
        instrumental_stem_path=data.get("instrumental_stem_path"),
        lines=lines,
        chord_track=chord_track,
        image_cache=data.get("image_cache", {}),
    )


def save_song(song: Song, path: Path) -> None:
    path.write_text(json.dumps(_song_to_dict(song), indent=2), encoding="utf-8")


def load_song(path: Path) -> Song:
    return _song_from_dict(json.loads(path.read_text(encoding="utf-8")))
```

Note: `Song.chord_track` defaults to an empty `ChordTrack()`, unlike the old
`instrumental_chords: list = []` — a fresh `Song()` with no chords is still exactly as
falsy/empty-checkable (`song.chord_track.events` is `[]`), just structured.

### Step 4: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: 9 passed

### Step 5: Commit models.py

```bash
git add lyricvideo/models.py tests/test_models.py
git commit -m "Replace ChordWord/InstrumentalChord with Word/ChordEvent/ChordTrack"
```

### Step 6: combine.py — write the failing tests

`combine_alignment`'s own logic is unaffected by the rename (it never read `.chord`
for anything except copying it through) — only the `ChordWord` construction calls in
its tests change to `Word`, dropping the `chord=` kwarg. Replace `tests/test_combine.py`
in full:

```python
import pytest

from lyricvideo.models import Word, LyricLine
from lyricvideo.combine import combine_alignment, AlignmentSanityError


def _parsed_lines():
    return [
        LyricLine(words=[Word(word="hello"), Word(word="there")]),
        LyricLine(words=[Word(word="my"), Word(word="friend")]),
    ]


def test_combine_alignment_assigns_times_in_order():
    lines = _parsed_lines()
    word_times = [(0.0, 0.4), (0.4, 0.9), (1.0, 1.3), (1.3, 1.8)]

    result = combine_alignment(lines, word_times, audio_duration=2.0)

    assert result[0].words[0].start_time == 0.0
    assert result[0].words[0].end_time == 0.4
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

### Step 7: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/test_combine.py -v`
Expected: FAIL — `ImportError: cannot import name 'Word' from 'lyricvideo.models'` is
already fixed by Step 3, so this actually fails differently: `combine.py` still
imports `ChordWord` (now gone from `models.py`), so the failure is
`ImportError: cannot import name 'ChordWord' from 'lyricvideo.models'` when
`combine.py` itself is imported.

### Step 8: Update combine.py

Replace `lyricvideo/combine.py` in full (only the import and the one construction
line change — the alignment-sanity logic itself is untouched):

```python
from __future__ import annotations

from .models import Word, LyricLine

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
        new_words: list[Word] = []
        for w in line.words:
            start, end = word_times[idx]
            if start < 0 or end > audio_duration or end < start or start < prev_end - MONOTONIC_TOLERANCE:
                raise AlignmentSanityError(
                    f"invalid timestamp for word {idx} ('{w.word}'): start={start}, "
                    f"end={end}, audio_duration={audio_duration}, prev_end={prev_end}"
                )
            new_words.append(Word(word=w.word, start_time=start, end_time=end))
            prev_end = end
            idx += 1
        timed_lines.append(
            LyricLine(words=new_words, start_time=new_words[0].start_time, end_time=new_words[-1].end_time)
        )
    return timed_lines
```

### Step 9: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/test_combine.py -v`
Expected: 4 passed

### Step 10: Commit combine.py

```bash
git add lyricvideo/combine.py tests/test_combine.py
git commit -m "Update combine.py for the Word rename"
```

### Step 11: layout.py — write the failing tests

This drops every chord-related field (`SceneWord.chord`/`chord_active`/`chord_flash`,
`Scene.instrumental_chord`/`instrumental_chord_flash`) and adds the instrumental-gap
image-key behavior (owner-requested 2026-09-09). Replace `tests/test_layout.py` in
full:

```python
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word, line_hash
from lyricvideo.layout import build_scene, find_current_line_index


def _make_lines():
    return [
        LyricLine(
            words=[
                Word(word="hello", start_time=0.0, end_time=0.5),
                Word(word="there", start_time=0.5, end_time=1.0),
            ],
            start_time=0.0, end_time=1.0,
        ),
        LyricLine(
            words=[
                Word(word="my", start_time=1.0, end_time=1.3),
                Word(word="friend", start_time=1.3, end_time=2.0),
            ],
            start_time=1.0, end_time=2.0,
        ),
        LyricLine(
            words=[Word(word="goodbye", start_time=2.0, end_time=2.6)],
            start_time=2.0, end_time=3.0,
        ),
    ]


def test_find_current_line_index():
    lines = _make_lines()
    assert find_current_line_index(lines, 0.2) == 0
    assert find_current_line_index(lines, 1.5) == 1
    assert find_current_line_index(lines, 2.9) == 2


def test_build_scene_shows_only_current_and_next_line_never_previous():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    offsets = sorted(l.distance_from_current for l in scene.lines)
    assert offsets == [0, 1]


def test_build_scene_word_active_sweeps_left_to_right_on_current_line():
    lines = _make_lines()
    scene_at_start = build_scene(lines, t=0.0)
    current_start = next(l for l in scene_at_start.lines if l.is_current)
    assert current_start.words[0].word_active is True
    assert current_start.words[1].word_active is False

    scene_mid = build_scene(lines, t=0.6)
    current_mid = next(l for l in scene_mid.lines if l.is_current)
    assert current_mid.words[0].word_active is True
    assert current_mid.words[1].word_active is True


def test_build_scene_next_line_words_never_marked_active():
    lines = _make_lines()
    scene = build_scene(lines, t=1.5, window=1)
    next_line = next(l for l in scene.lines if l.distance_from_current == 1)
    assert all(w.word_active is False for w in next_line.words)


def test_build_scene_ken_burns_progress_increases_within_line():
    lines = _make_lines()
    start_scene = build_scene(lines, t=1.0)
    mid_scene = build_scene(lines, t=1.5)
    end_scene = build_scene(lines, t=1.99)
    assert start_scene.ken_burns_progress < mid_scene.ken_burns_progress < end_scene.ken_burns_progress


def test_build_scene_ken_burns_progress_spans_gap_until_next_line_not_just_singing_end():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
        LyricLine(words=[Word(word="friend", start_time=3.0, end_time=3.5)], start_time=3.0, end_time=3.5),
    ]

    at_singing_end = build_scene(lines, t=1.0)
    mid_gap = build_scene(lines, t=2.0)
    just_before_next = build_scene(lines, t=2.99)

    assert at_singing_end.ken_burns_progress < 1.0
    assert at_singing_end.ken_burns_progress < mid_gap.ken_burns_progress < just_before_next.ken_burns_progress
    assert just_before_next.ken_burns_progress < 1.0
    assert at_singing_end.scroll_progress == 1.0


def test_build_scene_ken_burns_progress_uses_audio_duration_for_last_line():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
    ]

    mid = build_scene(lines, t=3.0, audio_duration=10.0)
    late = build_scene(lines, t=9.0, audio_duration=10.0)

    assert mid.ken_burns_progress < late.ken_burns_progress
    assert late.ken_burns_progress < 1.0


def test_build_scene_scroll_progress_tracks_time_through_current_line():
    lines = _make_lines()
    start_scene = build_scene(lines, t=1.0)
    mid_scene = build_scene(lines, t=1.5)
    end_scene = build_scene(lines, t=1.99)
    assert start_scene.scroll_progress == 0.0
    assert 0.0 < mid_scene.scroll_progress < 1.0
    assert start_scene.scroll_progress < mid_scene.scroll_progress < end_scene.scroll_progress


def test_build_scene_image_key_follows_lyric_line_text_while_singing():
    lines = _make_lines()
    scene = build_scene(lines, t=0.2)
    assert scene.image_key == line_hash("hello there")


def test_build_scene_image_key_follows_active_chord_during_instrumental_gap():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
        LyricLine(words=[Word(word="friend", start_time=5.0, end_time=5.5)], start_time=5.0, end_time=5.5),
    ]
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "Am"), ChordEvent(4.0, 6.0, "F"),
    ])

    scene_first_chord = build_scene(lines, t=1.5, chord_track=chord_track)
    scene_second_chord = build_scene(lines, t=2.5, chord_track=chord_track)

    assert scene_first_chord.image_key == line_hash("[Instrumental — chord: C]")
    assert scene_second_chord.image_key == line_hash("[Instrumental — chord: Am]")
    assert scene_first_chord.image_key != scene_second_chord.image_key


def test_build_scene_image_key_before_first_line_uses_instrumental_chord_too():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=5.0, end_time=5.5)], start_time=5.0, end_time=5.5),
    ]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 5.0, "G")])

    scene = build_scene(lines, t=1.0, chord_track=chord_track)

    assert scene.image_key == line_hash("[Instrumental — chord: G]")


def test_build_scene_image_key_gap_with_no_chord_track_falls_back_to_generic_key():
    lines = [
        LyricLine(words=[Word(word="hello", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0),
        LyricLine(words=[Word(word="friend", start_time=5.0, end_time=5.5)], start_time=5.0, end_time=5.5),
    ]

    scene = build_scene(lines, t=2.0)  # no chord_track passed at all

    assert scene.image_key == line_hash("[Instrumental]")
```

### Step 12: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/test_layout.py -v`
Expected: FAIL — `ImportError: cannot import name 'ChordEvent' from 'lyricvideo.models'`
resolves fine (Step 3 already added it), so the real failure here is `layout.py`
itself still importing `InstrumentalChord`/`ChordWord` from `models.py`
(`ImportError`), plus `build_scene()`'s current signature has no `chord_track`
parameter (`TypeError: unexpected keyword argument`).

### Step 13: Write layout.py's new content

Replace `lyricvideo/layout.py` in full:

```python
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ChordTrack, LyricLine, current_chord_at, line_hash


@dataclass
class SceneWord:
    text: str
    word_active: bool = False  # karaoke-style: true once this word has actually been sung


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
    scroll_progress: float = 0.0


def find_current_line_index(lines: list[LyricLine], t: float) -> int:
    idx = 0
    for i, line in enumerate(lines):
        if line.start_time is not None and line.start_time <= t:
            idx = i
        else:
            break
    return idx


def _in_a_line(lines: list[LyricLine], t: float) -> bool:
    """True if t falls within some line's own [start_time, end_time) singing
    window -- False during an intro, an instrumental gap between two lines, or
    after the last line has finished. Used to decide whether the background image
    should follow the lyric text or the active chord (2026-09-09 owner request:
    the image used to freeze on the last-sung line for the whole instrumental gap)."""
    return any(
        l.start_time is not None and l.end_time is not None and l.start_time <= t < l.end_time
        for l in lines
    )


def _instrumental_image_key(chord_track: ChordTrack, t: float) -> str:
    chord = current_chord_at(chord_track, t)
    caption = f"[Instrumental — chord: {chord.label}]" if chord else "[Instrumental]"
    return line_hash(caption)


def build_scene(
    lines: list[LyricLine],
    t: float,
    chord_track: ChordTrack | None = None,
    window: int = 1,
    audio_duration: float | None = None,
) -> Scene:
    if not lines:
        raise ValueError("no lines to build a scene from")

    idx = find_current_line_index(lines, t)
    current = lines[idx]

    scene_lines: list[SceneLine] = []
    # Only current (0) and upcoming (+1..+window) lines are shown -- no previous line.
    for offset in range(0, window + 1):
        i = idx + offset
        if not (0 <= i < len(lines)):
            continue
        line = lines[i]
        is_current = offset == 0
        words = []
        for w in line.words:
            word_sung = bool(is_current and w.start_time is not None and w.start_time <= t)
            words.append(SceneWord(text=w.word, word_active=word_sung))
        scene_lines.append(SceneLine(words=words, is_current=is_current, distance_from_current=offset))

    # Ken Burns progress spans the image's REAL on-screen duration -- until the
    # next line begins, or the song ends for the last line -- not just the
    # current line's own singing window (unchanged from before this merge).
    kb_start = current.start_time or 0.0
    if idx + 1 < len(lines) and lines[idx + 1].start_time is not None:
        kb_end = lines[idx + 1].start_time
    elif audio_duration is not None:
        kb_end = audio_duration
    else:
        kb_end = None
    if kb_end is None or kb_end <= kb_start:
        kb_end = current.end_time if (current.end_time and current.end_time > kb_start) else kb_start + 1.0
    ken_burns_progress = min(max((t - kb_start) / (kb_end - kb_start), 0.0), 1.0)

    scroll_start = current.start_time or 0.0
    scroll_end = (
        current.end_time if (current.end_time and current.end_time > scroll_start) else scroll_start + 1.0
    )
    scroll_progress = min(max((t - scroll_start) / (scroll_end - scroll_start), 0.0), 1.0)

    if _in_a_line(lines, t):
        image_key = line_hash(current.text)
    else:
        image_key = _instrumental_image_key(chord_track or ChordTrack(), t)

    return Scene(
        lines=scene_lines,
        image_key=image_key,
        ken_burns_progress=ken_burns_progress,
        scroll_progress=scroll_progress,
    )
```

### Step 14: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/test_layout.py -v`
Expected: 12 passed

### Step 15: Commit layout.py

```bash
git add lyricvideo/layout.py tests/test_layout.py
git commit -m "Drop chord fields from Scene; image key follows chord during gaps"
```

### Step 16: render.py — write the failing tests

This removes `_draw_line_with_chords`'s chord-label half and `_draw_instrumental_chord`
entirely, and adds `draw_chord_bar` (ported from LyricChord's `_draw_chords` +
`compute_layout`'s landscape geometry — this program only ever renders 1920×1080, so
the geometry constants are fixed, not parameterized). Replace `tests/test_render.py`
in full:

```python
import numpy as np
from PIL import Image

from lyricvideo.render import (
    CHORD_BOX, FRAME_SIZE, KEN_BURNS_PRESETS,
    apply_ken_burns, draw_chord_bar, draw_scene, ken_burns_preset_for_key,
)
from lyricvideo.layout import Scene, SceneLine, SceneWord
from lyricvideo.models import ChordEvent, ChordTrack


def test_apply_ken_burns_returns_frame_sized_image():
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    out_start = apply_ken_burns(img, progress=0.0)
    out_end = apply_ken_burns(img, progress=1.0)
    assert out_start.size == FRAME_SIZE
    assert out_end.size == FRAME_SIZE


def test_apply_ken_burns_pans_toward_end_position():
    img = Image.new("RGB", (800, 600))
    for x in range(800):
        for y in range(0, 600, 50):
            img.paste((x % 256, 0, 0), (x, y, x + 1, y + 50))

    start = np.array(apply_ken_burns(img, progress=0.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    end = np.array(apply_ken_burns(img, progress=1.0, start_x=0.0, end_x=1.0, zoom_start=1.3, zoom_end=1.3))
    assert not np.array_equal(start, end)


def test_apply_ken_burns_can_zoom_out():
    img = Image.new("RGB", (800, 600), (50, 50, 50))
    tight = apply_ken_burns(img, progress=0.0, zoom_start=1.2, zoom_end=1.0)
    wide = apply_ken_burns(img, progress=1.0, zoom_start=1.2, zoom_end=1.0)
    assert tight.size == wide.size == FRAME_SIZE


def test_ken_burns_preset_for_key_is_deterministic():
    assert ken_burns_preset_for_key("same-key") == ken_burns_preset_for_key("same-key")


def test_ken_burns_preset_for_key_varies_across_keys():
    presets_seen = {ken_burns_preset_for_key(f"key-{i}") for i in range(len(KEN_BURNS_PRESETS) * 3)}
    assert len(presets_seen) > 1


def test_draw_scene_renders_without_error_and_draws_text(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    scene = Scene(
        lines=[
            SceneLine(
                words=[SceneWord(text="hello"), SceneWord(text="there")],
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


def test_draw_scene_scroll_progress_shifts_line_position(test_font_path):
    def make_scene(scroll_progress):
        return Scene(
            lines=[
                SceneLine(words=[SceneWord(text="hello")], is_current=True, distance_from_current=0),
            ],
            image_key="abc123",
            ken_burns_progress=0.0,
            scroll_progress=scroll_progress,
        )

    bg = Image.new("RGB", FRAME_SIZE, (0, 0, 0))
    frame_at_start = np.array(draw_scene(make_scene(0.0), bg, test_font_path))
    frame_at_mid = np.array(draw_scene(make_scene(0.5), bg, test_font_path))

    assert not np.array_equal(frame_at_start, frame_at_mid)


def test_draw_chord_bar_renders_without_error(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G")], key="C major", bpm=120.0)

    frame = draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path)

    assert frame.size == FRAME_SIZE
    # The chord-box panel area must actually have been drawn into (not left as
    # the plain background color).
    assert frame.crop(CHORD_BOX).getextrema() != ((20, 20), (20, 20), (20, 20))


def test_draw_chord_bar_shows_dash_when_no_current_chord(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(5.0, 7.0, "C")])  # nothing covers t=0.0

    # Must not raise even though there's no current chord at this time.
    frame = draw_chord_bar(bg, chord_track, t=0.0, font_path=test_font_path)

    assert frame.size == FRAME_SIZE


def test_draw_chord_bar_does_not_mutate_input_frame(test_font_path):
    bg = Image.new("RGB", FRAME_SIZE, (20, 20, 20))
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C")])

    draw_chord_bar(bg, chord_track, t=0.5, font_path=test_font_path)

    assert bg.getextrema() == ((20, 20), (20, 20), (20, 20))
```

### Step 17: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/test_render.py -v`
Expected: FAIL with `ImportError: cannot import name 'draw_chord_bar' from 'lyricvideo.render'`

### Step 18: Write render.py's new content

Replace `lyricvideo/render.py` in full:

```python
from __future__ import annotations

import hashlib

from PIL import Image, ImageDraw, ImageFont

from .layout import Scene, SceneLine
from .models import ChordTrack, current_chord_at, next_chord_after

FRAME_SIZE = (1920, 1080)

CURRENT_LINE_UNSUNG_COLOR = (255, 255, 255)  # white -- current line, not sung yet
WORD_HIGHLIGHT_BG_COLOR = (46, 204, 113)     # bright green -- box behind already-sung words
WORD_HIGHLIGHT_TEXT_COLOR = (255, 255, 255)  # white text on top of the highlight box
NEXT_LINE_COLOR = (255, 255, 255)
TEXT_STROKE_COLOR = (0, 0, 0)                # black outline so text reads over any background
TEXT_STROKE_WIDTH = 3

# Chord bar geometry (landscape-only -- this program always renders 1920x1080),
# ported from LyricChord's compute_layout()'s landscape branch (frames.py).
_CHORD_BAR_MARGIN = 120
_CHORD_BAR_PAD = 20
CHORD_BOX = (
    _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.665),
    FRAME_SIZE[0] - _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.925),
)
_inner_h = CHORD_BOX[3] - CHORD_BOX[1] - 2 * _CHORD_BAR_PAD
NOW_BOX = (
    CHORD_BOX[0] + _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD,
    CHORD_BOX[0] + _CHORD_BAR_PAD + int(FRAME_SIZE[0] * 0.19), CHORD_BOX[3] - _CHORD_BAR_PAD,
)
NEXT_BOX = (
    NOW_BOX[2] + _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD + int(_inner_h * 0.15),
    NOW_BOX[2] + _CHORD_BAR_PAD + int(FRAME_SIZE[0] * 0.13), CHORD_BOX[3] - _CHORD_BAR_PAD - int(_inner_h * 0.15),
)
LANE_BOX = (
    NEXT_BOX[2] + 2 * _CHORD_BAR_PAD, CHORD_BOX[1] + _CHORD_BAR_PAD + int(_inner_h * 0.2),
    CHORD_BOX[2] - _CHORD_BAR_PAD, CHORD_BOX[3] - _CHORD_BAR_PAD - int(_inner_h * 0.2),
)
KEY_BPM_BADGE_XY = (FRAME_SIZE[0] - _CHORD_BAR_MARGIN, int(FRAME_SIZE[1] * 0.06))

PANEL_FILL = (11, 18, 32, 150)     # translucent panel behind the whole chord bar
BOX_FILL = (30, 41, 59, 235)       # NOW/NEXT/lane box fill
ACCENT_COLOR = (56, 189, 248)      # current-chord highlight
DIM_TEXT_COLOR = (148, 163, 184)
LANE_BLOCK_COLOR = (51, 65, 85, 235)
TIMELINE_WINDOW_SECONDS = 12.0

# (start_x, start_y, end_x, end_y, zoom_start, zoom_end) -- x/y are 0..1
# fractions of the available pan range (0.5 = centered).
KEN_BURNS_PRESETS: list[tuple[float, float, float, float, float, float]] = [
    (0.0, 0.0, 1.0, 1.0, 1.0, 1.15),
    (1.0, 1.0, 0.0, 0.0, 1.0, 1.15),
    (0.0, 1.0, 1.0, 0.0, 1.0, 1.15),
    (1.0, 0.0, 0.0, 1.0, 1.0, 1.15),
    (0.0, 0.5, 1.0, 0.5, 1.0, 1.15),
    (1.0, 0.5, 0.0, 0.5, 1.0, 1.15),
    (0.5, 0.0, 0.5, 1.0, 1.0, 1.15),
    (0.5, 1.0, 0.5, 0.0, 1.0, 1.15),
    (0.5, 0.5, 0.5, 0.5, 1.18, 1.0),
]


def ken_burns_preset_for_key(image_key: str) -> tuple[float, float, float, float, float, float]:
    idx = int(hashlib.sha256(image_key.encode("utf-8")).hexdigest(), 16) % len(KEN_BURNS_PRESETS)
    return KEN_BURNS_PRESETS[idx]


def apply_ken_burns(
    image: Image.Image,
    progress: float,
    start_x: float = 0.0,
    start_y: float = 0.0,
    end_x: float = 1.0,
    end_y: float = 1.0,
    zoom_start: float = 1.0,
    zoom_end: float = 1.15,
) -> Image.Image:
    img = image.resize(FRAME_SIZE)
    zoom = zoom_start + (zoom_end - zoom_start) * progress
    w, h = img.size
    new_w, new_h = max(int(w * zoom), w), max(int(h * zoom), h)
    img = img.resize((new_w, new_h))
    max_dx, max_dy = new_w - w, new_h - h
    frac_x = min(max(start_x + (end_x - start_x) * progress, 0.0), 1.0)
    frac_y = min(max(start_y + (end_y - start_y) * progress, 0.0), 1.0)
    x = int(max_dx * frac_x)
    y = int(max_dy * frac_y)
    return img.crop((x, y, x + w, y + h))


def _draw_line_words(draw, y, scene_line: SceneLine, font, is_current: bool) -> None:
    words = scene_line.words
    if not words:
        return
    space_w = draw.textlength(" ", font=font)
    widths = [draw.textlength(w.text, font=font) for w in words]
    total_w = sum(widths) + space_w * max(len(words) - 1, 0)
    x = (FRAME_SIZE[0] - total_w) / 2
    ascent, descent = font.getmetrics()
    text_height = ascent + descent

    for word, w_width in zip(words, widths):
        # Karaoke-style sweep: a highlight box appears behind each word the
        # instant it's actually sung (real per-word timing from forced
        # alignment), moving left to right through the line in sync with the
        # vocals -- not yet-sung words stay plain.
        if is_current and word.word_active:
            pad_x, pad_y = 6, 4
            draw.rectangle(
                [x - pad_x, y - pad_y, x + w_width + pad_x, y + text_height + pad_y],
                fill=WORD_HIGHLIGHT_BG_COLOR,
            )
            text_fill = WORD_HIGHLIGHT_TEXT_COLOR
        else:
            text_fill = CURRENT_LINE_UNSUNG_COLOR if is_current else NEXT_LINE_COLOR

        draw.text(
            (x, y), word.text, font=font, fill=text_fill,
            stroke_width=TEXT_STROKE_WIDTH, stroke_fill=TEXT_STROKE_COLOR,
        )
        x += w_width + space_w


def draw_scene(scene: Scene, background: Image.Image, font_path: str, font_size: int = 48) -> Image.Image:
    frame = background.copy()
    draw = ImageDraw.Draw(frame)
    font = ImageFont.truetype(font_path, font_size)

    center_y = FRAME_SIZE[1] // 2
    ascent, descent = font.getmetrics()
    line_height = (ascent + descent) + 20

    for sl in scene.lines:
        # Continuous position, not a fixed per-line step: every line drifts
        # upward by scroll_progress (0->1 across the current line's own real
        # start->end window) so the transition to the next line is a smooth
        # scroll instead of a snap.
        y = center_y + (sl.distance_from_current - scene.scroll_progress) * line_height
        _draw_line_words(draw, y, sl, font, sl.is_current)

    return frame


def _display_chord_label(label: str) -> str:
    return "N.C." if label == "N" else label


def draw_chord_bar(frame: Image.Image, chord_track: ChordTrack, t: float, font_path: str) -> Image.Image:
    """Composites the NOW/NEXT/timeline chord bar and the Key/BPM badge onto
    `frame`, ported from LyricChord's FrameComposer._draw_chords + its header
    badge. Independent of which lyric line/word is on screen -- looked up
    directly from chord_track by playback time t. Returns a new image; `frame`
    is not mutated (matches draw_scene's own copy-on-write style)."""
    label_font = ImageFont.truetype(font_path, 20)
    now_font = ImageFont.truetype(font_path, 64)
    next_font = ImageFont.truetype(font_path, 32)
    small_font = ImageFont.truetype(font_path, 24)
    lane_font = ImageFont.truetype(font_path, 30)

    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    draw.rounded_rectangle(CHORD_BOX, radius=24, fill=PANEL_FILL)
    draw.rounded_rectangle(NOW_BOX, radius=18, fill=BOX_FILL)
    draw.rounded_rectangle(NEXT_BOX, radius=16, fill=BOX_FILL)
    draw.rounded_rectangle(LANE_BOX, radius=12, fill=BOX_FILL)

    draw.text((NOW_BOX[0] + 16, NOW_BOX[1] + 10), "NOW", font=label_font, fill=DIM_TEXT_COLOR)
    draw.text((NEXT_BOX[0] + 14, NEXT_BOX[1] + 8), "NEXT", font=label_font, fill=DIM_TEXT_COLOR)

    current = current_chord_at(chord_track, t)
    next_event = next_chord_after(chord_track, t)

    now_label = _display_chord_label(current.label) if current else "—"
    now_color = ACCENT_COLOR if current else DIM_TEXT_COLOR
    now_w = draw.textlength(now_label, font=now_font)
    draw.text(
        ((NOW_BOX[0] + NOW_BOX[2]) / 2 - now_w / 2, (NOW_BOX[1] + NOW_BOX[3]) / 2 - 32),
        now_label, font=now_font, fill=now_color,
    )

    if next_event is not None:
        next_label = _display_chord_label(next_event.label)
        next_w = draw.textlength(next_label, font=next_font)
        draw.text(
            ((NEXT_BOX[0] + NEXT_BOX[2]) / 2 - next_w / 2, NEXT_BOX[1] + 34),
            next_label, font=next_font, fill=(248, 250, 252, 255),
        )
        eta = f"in {max(0.0, next_event.start - t):.1f}s"
        eta_w = draw.textlength(eta, font=small_font)
        draw.text(
            ((NEXT_BOX[0] + NEXT_BOX[2]) / 2 - eta_w / 2, NEXT_BOX[3] - 30),
            eta, font=small_font, fill=DIM_TEXT_COLOR,
        )

    lx0, ly0, lx1, ly1 = LANE_BOX
    pps = (lx1 - lx0) / TIMELINE_WINDOW_SECONDS
    for event in chord_track.events:
        if event.start >= t + TIMELINE_WINDOW_SECONDS:
            break
        if event.end <= t:
            continue
        bx0 = lx0 + max(0.0, event.start - t) * pps
        bx1 = lx0 + min(TIMELINE_WINDOW_SECONDS, event.end - t) * pps
        if bx1 - bx0 < 2:
            continue
        is_current = current is not None and event is current
        fill = (*ACCENT_COLOR, 255) if is_current else LANE_BLOCK_COLOR
        draw.rounded_rectangle((int(bx0) + 1, ly0 + 6, int(bx1) - 1, ly1 - 6), radius=10, fill=fill)
        label = _display_chord_label(event.label)
        text_color = (11, 18, 32, 255) if is_current else (248, 250, 252, 255)
        label_w = draw.textlength(label, font=lane_font)
        if label_w + 16 <= (bx1 - bx0):
            draw.text(((bx0 + bx1) / 2 - label_w / 2, (ly0 + ly1) / 2 - 15), label, font=lane_font, fill=text_color)

    if chord_track.key or chord_track.bpm:
        parts = []
        if chord_track.key:
            parts.append(f"Key: {chord_track.key}")
        if chord_track.bpm:
            parts.append(f"{int(round(chord_track.bpm))} BPM")
        badge = "   ·   ".join(parts)
        bx, by = KEY_BPM_BADGE_XY
        badge_w = draw.textlength(badge, font=small_font)
        draw.text((bx - badge_w, by), badge, font=small_font, fill=(*ACCENT_COLOR, 255))

    composited = Image.alpha_composite(frame.convert("RGBA"), overlay)
    return composited.convert("RGB")
```

Note: no song title or artist text is drawn anywhere in this file (owner-confirmed
2026-09-09) — only the chord bar and the Key/BPM badge were added.

### Step 19: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/test_render.py -v`
Expected: 10 passed

### Step 20: Commit render.py

```bash
git add lyricvideo/render.py tests/test_render.py
git commit -m "Add LyricChord-style NOW/NEXT/timeline chord bar; remove above-word chords"
```

### Step 21: assemble.py — write the failing tests

Replace `tests/test_assemble.py` in full:

```python
from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Word


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
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_video(lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path)

    assert calls["duration"] == 2.0
    assert calls["fps"] == 24
    frame = calls["make_frame"](0.3)
    assert frame.shape[:2] == (1080, 1920)


def test_assemble_video_threads_chord_track_into_scene(tmp_path, monkeypatch, test_font_path):
    class _FakeAudioClip:
        duration = 2.0

    calls = {}

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame

        def set_audio(self, audio_clip):
            return self

        def write_videofile(self, path, fps, codec, audio_codec):
            Path(path).write_bytes(b"fake-mp4")

    captured = {}
    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: _FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", _FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    real_build_scene = assemble_module.build_scene

    def spying_build_scene(lines, t, chord_track=None, **kwargs):
        captured["chord_track"] = chord_track
        return real_build_scene(lines, t, chord_track=chord_track, **kwargs)

    monkeypatch.setattr(assemble_module, "build_scene", spying_build_scene)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    chord_track = ChordTrack(events=[ChordEvent(1.0, 2.0, "Em7")])
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, chord_track, tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](1.5)

    assert captured["chord_track"] == chord_track


def test_assemble_video_draws_chord_bar_on_every_frame(tmp_path, monkeypatch, test_font_path):
    class _FakeAudioClip:
        duration = 2.0

    calls = {}

    class _FakeVideoClip:
        def __init__(self, make_frame, duration):
            calls["make_frame"] = make_frame

        def set_audio(self, audio_clip):
            return self

        def write_videofile(self, path, fps, codec, audio_codec):
            Path(path).write_bytes(b"fake-mp4")

    monkeypatch.setattr("lyricvideo.assemble.AudioFileClip", lambda path: _FakeAudioClip())
    monkeypatch.setattr("lyricvideo.assemble.VideoClip", _FakeVideoClip)

    from lyricvideo import assemble as assemble_module

    draw_calls = []
    real_draw_chord_bar = assemble_module.draw_chord_bar

    def spying_draw_chord_bar(frame, chord_track, t, font_path):
        draw_calls.append(t)
        return real_draw_chord_bar(frame, chord_track, t, font_path)

    monkeypatch.setattr(assemble_module, "draw_chord_bar", spying_draw_chord_bar)

    lines = [
        LyricLine(words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0)
    ]
    out_path = tmp_path / "final.mp4"

    assemble_module.assemble_video(
        lines, ChordTrack(), tmp_path, tmp_path / "audio.wav", out_path, font_path=test_font_path,
    )
    calls["make_frame"](0.5)

    assert draw_calls == [0.5]
```

### Step 22: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/test_assemble.py -v`
Expected: FAIL — `assemble_video()`'s current signature doesn't accept a `chord_track`
positional argument (`TypeError`).

### Step 23: Update assemble.py

Replace `lyricvideo/assemble.py` in full:

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

try:
    from moviepy.editor import AudioFileClip, VideoClip  # moviepy < 2.0
except ImportError:
    from moviepy import AudioFileClip, VideoClip  # moviepy >= 2.0 dropped .editor

from .layout import build_scene
from .models import ChordTrack, LyricLine
from .render import FRAME_SIZE, apply_ken_burns, draw_chord_bar, draw_scene, ken_burns_preset_for_key

FPS = 24


def assemble_video(
    lines: list[LyricLine],
    chord_track: ChordTrack,
    image_dir: Path,
    audio_path: Path,
    out_path: Path,
    font_path: str,
    fallback_color: tuple[int, int, int] = (30, 30, 40),
) -> None:
    image_cache: dict[str, Image.Image] = {}
    audio_clip = AudioFileClip(str(audio_path))
    duration = audio_clip.duration

    def get_image(key: str) -> Image.Image:
        if key not in image_cache:
            path = image_dir / f"{key}.png"
            if path.exists():
                image_cache[key] = Image.open(path).convert("RGB")
            else:
                image_cache[key] = Image.new("RGB", FRAME_SIZE, fallback_color)
        return image_cache[key]

    def make_frame(t: float):
        scene = build_scene(lines, t, chord_track=chord_track, audio_duration=duration)
        start_x, start_y, end_x, end_y, zoom_start, zoom_end = ken_burns_preset_for_key(scene.image_key)
        bg = apply_ken_burns(
            get_image(scene.image_key), scene.ken_burns_progress,
            start_x, start_y, end_x, end_y, zoom_start, zoom_end,
        )
        frame = draw_scene(scene, bg, font_path)
        frame = draw_chord_bar(frame, chord_track, t, font_path)
        return np.array(frame)

    video_clip = VideoClip(make_frame, duration=duration).set_audio(audio_clip)
    video_clip.write_videofile(str(out_path), fps=FPS, codec="libx264", audio_codec="aac")
```

### Step 24: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/test_assemble.py -v`
Expected: 3 passed

### Step 25: Commit assemble.py

```bash
git add lyricvideo/assemble.py tests/test_assemble.py
git commit -m "Wire chord_track and the chord-bar draw into assemble_video"
```

### Step 26: Point detect_chords.py at models.py's ChordEvent/ChordTrack

Task 4 defined `ChordEvent`/`ChordTrack` directly inside `detect_chords.py` so that
task was self-contained and testable in isolation. Now that Step 3 has added the
permanent versions to `models.py` (with the `current_chord_at`/`next_chord_after`
query helpers `layout.py`/`render.py` depend on), `detect_chords.py` must use those
same classes — otherwise `pipeline.py` would end up with two incompatible
`ChordTrack` types. Edit `lyricvideo/detect_chords.py`:

Replace:

```python
from . import chord_theory as theory
from .audio_decode import decode_audio

SR = 22050
HOP = 512
SOFTMAX_TEMPERATURE = 0.04     # lower = trust the template match more
STAY_PROBABILITY = 0.85        # Viterbi self-transition probability per beat
NON_DIATONIC_PENALTY = 0.75    # multiplies similarity for chords outside the key
SILENCE_RATIO = 0.03           # segments below this fraction of peak RMS become "N"

# LyricChord's own Settings defaults (config.py), fixed here since this program has
# no per-run chord-detection settings UI.
SNAP_CHORDS_TO_KEY = True
PREFER_FLATS = True
MIN_CHORD_SECONDS = 0.5
INCLUDE_SEVENTH_CHORDS = False


@dataclass
class ChordEvent:
    """A chord held from `start` to `end` (seconds). Label like 'Am', 'F#', 'N'."""

    start: float
    end: float
    label: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ChordTrack:
    """Timeline of chords plus global musical info for one song."""

    events: list[ChordEvent] = field(default_factory=list)
    key: str = ""
    bpm: float = 0.0
```

with:

```python
from . import chord_theory as theory
from .audio_decode import decode_audio
from .models import ChordEvent, ChordTrack

SR = 22050
HOP = 512
SOFTMAX_TEMPERATURE = 0.04     # lower = trust the template match more
STAY_PROBABILITY = 0.85        # Viterbi self-transition probability per beat
NON_DIATONIC_PENALTY = 0.75    # multiplies similarity for chords outside the key
SILENCE_RATIO = 0.03           # segments below this fraction of peak RMS become "N"

# LyricChord's own Settings defaults (config.py), fixed here since this program has
# no per-run chord-detection settings UI.
SNAP_CHORDS_TO_KEY = True
PREFER_FLATS = True
MIN_CHORD_SECONDS = 0.5
INCLUDE_SEVENTH_CHORDS = False
```

Also remove the now-unused `from dataclasses import dataclass, field` import line at
the top of the file (nothing in this file defines a dataclass anymore).

Run: `.venv/bin/python -m pytest tests/test_detect_chords.py -v`
Expected: 5 passed (the test file's own `from lyricvideo.detect_chords import
ChordEvent, ChordTrack, detect_chords` import keeps working unchanged — Python
resolves it to the names now imported into that module's namespace from `models.py`).

Commit:

```bash
git add lyricvideo/detect_chords.py
git commit -m "Point detect_chords.py at models.py's ChordEvent/ChordTrack"
```

### Step 27: pipeline.py — write the failing tests

This is the full rewrite: new `STAGES`, the `identify`/`fetch_lyrics`/`detect_chords`
stages, `align` now consuming `fetch_lyrics`'s text, the `images` stage generating
instrumental-chord images too, and `run_pipeline`'s simplified signature (audio +
work_dir are the only required arguments; `title` is an optional override). Replace
`tests/test_pipeline.py` in full:

```python
import json
from datetime import datetime
from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, save_song
from lyricvideo.pipeline import (
    run_pipeline,
    list_redoable_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
    _instrumental_chord_labels,
)


def _patch_common(monkeypatch, tmp_path):
    """Shared stubs for every run_pipeline test below: no real Demucs, alignment,
    Claude, or Replicate calls -- exercising the stage wiring, not the ported
    modules themselves (those have their own dedicated tests)."""
    import torch

    monkeypatch.setattr(
        "lyricvideo.pipeline.extract_metadata",
        lambda audio_path: type(
            "Info", (), {"title": "Test Song", "artist": "Test Artist", "duration": 10.0, "alt_titles": []}
        )(),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines", lambda *a, **k: ["hello there", "my friend"]
    )
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: tmp_path / "vocals.wav")
    monkeypatch.setattr("lyricvideo.pipeline.torchaudio.load", lambda path: (torch.zeros(1, 16000 * 10), 16000))
    monkeypatch.setattr("lyricvideo.pipeline.align_words", lambda vocals_path, words: [
        (float(i), float(i) + 0.4) for i in range(len(words))
    ])
    monkeypatch.setattr(
        "lyricvideo.pipeline.detect_chords",
        lambda instrumental_stem_path: ChordTrack(events=[ChordEvent(0.0, 10.0, "C")], key="C major", bpm=100.0),
    )
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: "a fake song gist")
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: Path("x"))
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")


def test_run_pipeline_reports_progress_per_stage(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    reported = []
    run_pipeline(Path("audio.mp3"), work_dir, progress_callback=reported.append)

    assert reported == [
        "identify", "separate", "fetch_lyrics", "align", "detect_chords", "images", "render", "done",
    ]


def test_run_pipeline_uses_auto_identified_title_when_none_given(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    out_path = run_pipeline(Path("audio.mp3"), work_dir)

    assert out_path.name == "test-song.mp4"


def test_run_pipeline_title_override_replaces_identified_title(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    out_path = run_pipeline(Path("audio.mp3"), work_dir, title="My Custom Title")

    assert out_path.name == "my-custom-title.mp4"


def test_run_pipeline_only_needs_the_audio_file_no_pdf_or_chords_text_argument(tmp_path, monkeypatch):
    """The whole point of this merge: run_pipeline() takes no tab/chords-text
    argument at all anymore -- calling it with just (audio_path, work_dir) must
    complete without raising, producing a path inside work_dir."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    out_path = run_pipeline(Path("audio.mp3"), work_dir)

    assert out_path.parent == work_dir


def test_run_pipeline_skips_earlier_stages(tmp_path, monkeypatch):
    calls = []
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image",
        lambda *a, **k: calls.append("images") or Path("x"),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.assemble_video",
        lambda *a, **k: calls.append("render"),
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "t", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    seeded_line = LyricLine(
        words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0,
    )
    save_song(
        Song(title="t", audio_path="a.mp3", lines=[seeded_line],
             chord_track=ChordTrack(events=[ChordEvent(0.0, 1.0, "C")])),
        work_dir / "lyrics_timed.json",
    )

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")

    assert calls == ["images", "render"]


def test_run_pipeline_resuming_past_identify_uses_saved_song_info(tmp_path, monkeypatch):
    """A redo resuming at 'fetch_lyrics' or later never re-runs identify -- it must
    read the original run's title/artist back from song_info.json instead."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "Original Title", "artist": "Original Artist", "duration": 10.0, "alt_titles": []}),
        encoding="utf-8",
    )

    out_path = run_pipeline(Path("songs/original.mp3"), work_dir, start_stage="fetch_lyrics")

    assert out_path.name == "original-title.mp4"


def test_run_pipeline_detect_chords_writes_chord_track_onto_song(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    from lyricvideo.models import load_song

    saved = load_song(work_dir / "lyrics_timed.json")
    assert saved.chord_track.key == "C major"
    assert saved.chord_track.events[0].label == "C"


def test_instrumental_chord_labels_skips_chords_covered_by_a_sung_line():
    lines = [LyricLine(start_time=0.0, end_time=2.0)]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G")])

    assert _instrumental_chord_labels(lines, chord_track) == ["G"]


def test_instrumental_chord_labels_dedupes_repeated_labels():
    lines = []
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "Am"), ChordEvent(2.0, 4.0, "F"), ChordEvent(4.0, 6.0, "Am"),
    ])

    assert _instrumental_chord_labels(lines, chord_track) == ["Am", "F"]


def test_list_redoable_songs_finds_folders_with_a_completed_run(tmp_path):
    work_root = tmp_path / "work"
    (work_root / "angie-rolling-stones").mkdir(parents=True)
    save_song(
        Song(title="Angie", audio_path="a.mp3"),
        work_root / "angie-rolling-stones" / "lyrics_timed.json",
    )
    (work_root / "still-in-progress").mkdir(parents=True)

    assert list_redoable_songs(work_root) == ["angie-rolling-stones"]


def test_list_redoable_songs_returns_empty_list_when_work_dir_missing(tmp_path):
    assert list_redoable_songs(tmp_path / "does-not-exist") == []


def test_list_redoable_songs_sorted_alphabetically(tmp_path):
    work_root = tmp_path / "work"
    for slug in ["wish-you-were-here", "angie-rolling-stones", "eye-in-the-sky"]:
        (work_root / slug).mkdir(parents=True)
        save_song(Song(title=slug, audio_path="a.mp3"), work_root / slug / "lyrics_timed.json")

    assert list_redoable_songs(work_root) == [
        "angie-rolling-stones", "eye-in-the-sky", "wish-you-were-here",
    ]


def test_load_redo_inputs_reads_audio_path_and_title(tmp_path):
    song_dir = tmp_path / "angie-rolling-stones"
    song_dir.mkdir()
    save_song(
        Song(title="Angie", audio_path="/home/doug/songs/angie.mp3"),
        song_dir / "lyrics_timed.json",
    )

    audio_path, title = load_redo_inputs(song_dir)

    assert audio_path == Path("/home/doug/songs/angie.mp3")
    assert title == "Angie"


def test_backup_song_outputs_copies_video_and_timed_json(tmp_path):
    work_dir = tmp_path / "angie-rolling-stones"
    work_dir.mkdir()
    (work_dir / "angie.mp4").write_bytes(b"old video bytes")
    save_song(Song(title="Angie", audio_path="a.mp3"), work_dir / "lyrics_timed.json")

    backup_dir = backup_song_outputs(work_dir, "angie", now=datetime(2026, 9, 8, 12, 0, 0))

    assert backup_dir == work_dir / "redo_backup_20260908-120000"
    assert (backup_dir / "angie.mp4").read_bytes() == b"old video bytes"
    assert (backup_dir / "lyrics_timed.json").exists()
    assert (work_dir / "angie.mp4").exists()
    assert (work_dir / "lyrics_timed.json").exists()


def test_backup_song_outputs_returns_none_when_nothing_to_back_up(tmp_path):
    work_dir = tmp_path / "brand-new-song"
    work_dir.mkdir()

    assert backup_song_outputs(work_dir, "brand-new-song", now=datetime(2026, 9, 8, 12, 0, 0)) is None


def test_backup_song_outputs_backs_up_whichever_of_the_two_exists(tmp_path):
    work_dir = tmp_path / "angie-rolling-stones"
    work_dir.mkdir()
    (work_dir / "angie.mp4").write_bytes(b"old video bytes")

    backup_dir = backup_song_outputs(work_dir, "angie", now=datetime(2026, 9, 8, 12, 0, 0))

    assert (backup_dir / "angie.mp4").exists()
    assert not (backup_dir / "lyrics_timed.json").exists()


def test_prepare_images_for_fresh_regeneration_moves_existing_dir_aside(tmp_path):
    work_dir = tmp_path / "angie-rolling-stones"
    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "abc123.png").write_bytes(b"old image bytes")

    moved_to = prepare_images_for_fresh_regeneration(images_dir, now=datetime(2026, 9, 8, 12, 0, 0))

    assert moved_to == work_dir / "images_prior_20260908-120000"
    assert (moved_to / "abc123.png").read_bytes() == b"old image bytes"
    assert not images_dir.exists()
    assert not moved_to.name.startswith("images_backup_")


def test_prepare_images_for_fresh_regeneration_returns_none_when_no_images_dir(tmp_path):
    images_dir = tmp_path / "angie-rolling-stones" / "images"

    assert prepare_images_for_fresh_regeneration(images_dir, now=datetime(2026, 9, 8, 12, 0, 0)) is None
```

### Step 28: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `pipeline.py` still imports `parse_tab_pdf`/`map_chords_with_vision`/
`parse_plaintext_chords`/`detect_chord_change_times`/`ChordWord`/`InstrumentalChord`,
none of which exist anymore, so the module fails to import at all (`ImportError` at
collection time for the whole file).

### Step 29: Write pipeline.py's new content

Replace `lyricvideo/pipeline.py` in full:

```python
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

import anthropic
import torchaudio
from dotenv import load_dotenv

from .align import align_words
from .assemble import assemble_video
from .combine import combine_alignment
from .detect_chords import detect_chords
from .fetch_lyrics import fetch_lyric_lines
from .identify import extract_metadata
from .imagery import get_or_generate_image, summarize_song_gist
from .layout import _in_a_line
from .models import ChordTrack, LyricLine, Song, Word, load_song, save_song
from .separate import separate_vocals

STAGES = ["identify", "separate", "fetch_lyrics", "align", "detect_chords", "images", "render"]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled-song"


def _instrumental_chord_labels(lines: list[LyricLine], chord_track: ChordTrack) -> list[str]:
    """Distinct chord labels that need their own instrumental-gap background image:
    every ChordEvent whose midpoint doesn't fall inside any sung line's window,
    in first-seen order (so a repeated chord anywhere in the song reuses one
    image, the same dedup-by-content philosophy the per-line images already use)."""
    labels: list[str] = []
    for event in chord_track.events:
        mid = (event.start + event.end) / 2
        if not _in_a_line(lines, mid) and event.label not in labels:
            labels.append(event.label)
    return labels


def _default_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    raise FileNotFoundError("No default font found; pass --font explicitly")


def list_redoable_songs(work_root: Path) -> list[str]:
    """Names of work_root's immediate subdirectories that hold a completed
    run (a saved lyrics_timed.json), sorted alphabetically. Backs the GUI's
    Redo dropdown -- a song is only offered once it has real timing data to
    resume from."""
    if not work_root.exists():
        return []
    return sorted(
        entry.name
        for entry in work_root.iterdir()
        if entry.is_dir() and (entry / "lyrics_timed.json").exists()
    )


def load_redo_inputs(song_dir: Path) -> tuple[Path, str]:
    """Reads back the (audio_path, title) a prior run saved onto its own
    Song, so a redo never needs the owner to re-browse for the original
    audio file."""
    song = load_song(song_dir / "lyrics_timed.json")
    return Path(song.audio_path), song.title


def backup_song_outputs(work_dir: Path, slug: str, now: datetime | None = None) -> Path | None:
    """Copies (never moves -- the originals must still be there for the
    redo run itself to overwrite) work_dir/{slug}.mp4 and
    work_dir/lyrics_timed.json into a fresh work_dir/redo_backup_<timestamp>/
    directory, so the exact pre-redo video and chord/lyric timing are always
    recoverable. Returns the backup directory, or None if neither file
    existed yet (nothing to protect)."""
    video_path = work_dir / f"{slug}.mp4"
    timed_path = work_dir / "lyrics_timed.json"
    if not video_path.exists() and not timed_path.exists():
        return None

    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup_dir = work_dir / f"redo_backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if video_path.exists():
        shutil.copy2(video_path, backup_dir / video_path.name)
    if timed_path.exists():
        shutil.copy2(timed_path, backup_dir / timed_path.name)
    return backup_dir


def prepare_images_for_fresh_regeneration(images_dir: Path, now: datetime | None = None) -> Path | None:
    """Moves an existing images/ directory aside to images_prior_<timestamp>/
    so the pipeline's images stage (which creates a fresh, empty images_dir
    via mkdir) generates every line's image anew instead of reusing what's
    cached there. Deliberately named "images_prior_", NOT "images_backup_" --
    the images stage auto-searches every images_backup_*/ directory for a
    reusable cached image (see get_or_generate_image's extra_cache_dirs),
    which would silently defeat "generate new images" if this used that
    name. Returns the new path, or None if there was no images_dir to move."""
    if not images_dir.exists():
        return None

    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    moved_to = images_dir.parent / f"images_prior_{timestamp}"
    images_dir.rename(moved_to)
    return moved_to


def run_pipeline(
    audio_path: Path,
    work_dir: Path,
    title: str | None = None,
    start_stage: str = "identify",
    font_path: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    start_idx = STAGES.index(start_stage)

    def report(stage: str) -> None:
        if progress_callback is not None:
            progress_callback(stage)

    work_dir.mkdir(parents=True, exist_ok=True)
    # Matches separate_vocals()'s own output path convention, so resuming from a
    # later stage (skipping separation) still finds the file it already wrote.
    demucs_dir = work_dir / "htdemucs" / Path(audio_path).stem
    vocals_path = demucs_dir / "vocals.wav"
    instrumental_stem_path = demucs_dir / "no_vocals.wav"
    info_path = work_dir / "song_info.json"
    lyrics_path = work_dir / "lyric_lines.json"
    timed_path = work_dir / "lyrics_timed.json"
    images_dir = work_dir / "images"

    if start_idx <= STAGES.index("identify"):
        report("identify")
        info = extract_metadata(audio_path)
        # A caller-supplied title overrides identify's own guess for DISPLAY/
        # filename purposes; artist/duration always come from identify -- there is
        # no separate artist-override field. Only relevant on a fresh run: a redo
        # resuming past this stage ignores this parameter entirely and reads the
        # ORIGINAL run's resolved title back from song_info.json below, so the
        # video's filename never changes between the original run and a redo.
        resolved_title = title.strip() if title and title.strip() else info.title
        info_path.write_text(
            json.dumps({
                "title": resolved_title, "artist": info.artist,
                "duration": info.duration, "alt_titles": info.alt_titles,
            }),
            encoding="utf-8",
        )
    else:
        info_data = json.loads(info_path.read_text(encoding="utf-8"))
        resolved_title = info_data["title"]

    final_path = work_dir / f"{slugify(resolved_title)}.mp4"

    if start_idx <= STAGES.index("separate"):
        report("separate")
        vocals_path = separate_vocals(audio_path, work_dir)

    if start_idx <= STAGES.index("fetch_lyrics"):
        report("fetch_lyrics")
        info_data = json.loads(info_path.read_text(encoding="utf-8"))
        lines_text = fetch_lyric_lines(
            audio_path, info_data["title"], info_data["artist"], info_data["duration"],
            info_data.get("alt_titles"),
        )
        lyrics_path.write_text(json.dumps(lines_text), encoding="utf-8")

    if start_idx <= STAGES.index("align"):
        report("align")
        lines_text = json.loads(lyrics_path.read_text(encoding="utf-8"))
        parsed_lines = [LyricLine(words=[Word(word=w) for w in text.split()]) for text in lines_text]
        waveform, sample_rate = torchaudio.load(str(vocals_path))
        audio_duration = waveform.shape[1] / sample_rate
        flat_words = [w.word for line in parsed_lines for w in line.words]
        word_times = align_words(vocals_path, flat_words)
        timed_lines = combine_alignment(parsed_lines, word_times, audio_duration)
        song = Song(
            title=resolved_title,
            audio_path=str(audio_path),
            vocal_stem_path=str(vocals_path),
            instrumental_stem_path=str(instrumental_stem_path),
            lines=timed_lines,
        )
        save_song(song, timed_path)
    else:
        song = load_song(timed_path)

    if start_idx <= STAGES.index("detect_chords"):
        report("detect_chords")
        song.chord_track = detect_chords(instrumental_stem_path)
        save_song(song, timed_path)

    if start_idx <= STAGES.index("images"):
        report("images")
        anthropic_client = anthropic.Anthropic()
        replicate_token = os.environ["REPLICATE_API_TOKEN"]
        full_lyrics = "\n".join(l.text for l in song.lines)
        song_gist = summarize_song_gist(anthropic_client, full_lyrics)
        images_dir.mkdir(exist_ok=True)
        # Reuse already-paid-for images from any prior images_backup_*/ archive
        # before spending on a new one (unchanged convention).
        backup_dirs = sorted(work_dir.glob("images_backup_*"))
        for line in song.lines:
            get_or_generate_image(
                anthropic_client, replicate_token, song_gist, line.text, images_dir,
                extra_cache_dirs=backup_dirs,
            )
        # Instrumental-gap images (2026-09-09 owner request): one per distinct
        # chord label that actually occurs during a gap, so the background
        # follows the chord instead of freezing on the last-sung line's image.
        for label in _instrumental_chord_labels(song.lines, song.chord_track):
            caption = f"[Instrumental — chord: {label}]"
            get_or_generate_image(
                anthropic_client, replicate_token, song_gist, caption, images_dir,
                extra_cache_dirs=backup_dirs,
            )

    if start_idx <= STAGES.index("render"):
        report("render")
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            font_path or _default_font(),
        )

    report("done")
    return final_path


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Generate a synced lyric+chord video from just an audio file."
    )
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument(
        "--title", default=None,
        help="Override the auto-identified song title (artist/lyrics search are unaffected).",
    )
    parser.add_argument("--stage", choices=STAGES, default="identify")
    parser.add_argument("--font", default=None)
    args = parser.parse_args()

    out = run_pipeline(args.audio, args.work_dir, args.title, args.stage, args.font)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
```

Deleted from the old `pipeline.py` (superseded, not carried forward): `line_to_dict`/
`dict_to_line`/`_parsed_to_dict`/`_dict_to_parsed` (existed solely to serialize
`parsed_tab.json`, which no longer exists) and `_compute_instrumental_chords`
(superseded by the `detect_chords` stage).

### Step 30: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: 17 passed

### Step 31: Run the FULL test suite

This is the point where Task 7's coordinated change must be fully consistent again.

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: every test in `tests/test_models.py`, `test_combine.py`, `test_layout.py`,
`test_render.py`, `test_assemble.py`, `test_detect_chords.py`, `test_pipeline.py`
passes. Tests in `test_pdf_parse.py`, `test_vision_parse.py`, `test_plaintext_chords.py`,
`test_instrumental_chords.py`, `test_ocr_parse.py`, `test_ocr_clean.py` will FAIL at
this point (those modules still exist but are now fully disconnected from the rest of
the codebase) — that's expected and is resolved by Task 9, which deletes them along
with their tests. If anything in the list of files this task touched fails, fix it now
before proceeding; do not carry a broken cutover into Task 8.

### Step 32: Commit pipeline.py

```bash
git add lyricvideo/pipeline.py tests/test_pipeline.py
git commit -m "Rewire pipeline.py: identify/fetch_lyrics/detect_chords replace tab parsing"
```

---

## Task 8: Simplify the GUI — audio-only input, auto-identified title

`test_gui.py` only tests `_slugify`/`_split_log_text` (pure functions untouched by
this task), so it needs no changes — this task has no new automated tests of its own;
correctness is verified by the full suite still passing (nothing else imports from
`gui.py`) plus the manual smoke check in Task 11.

**Files:**
- Modify: `lyricvideo/gui.py`

**Interfaces:**
- Consumes: `lyricvideo.identify.extract_metadata` (Task 5), `lyricvideo.pipeline.run_pipeline`'s new signature (Task 7).

- [ ] **Step 1: Add the identify import**

In `lyricvideo/gui.py`, find:

```python
from .pipeline import (
    run_pipeline,
    slugify as _slugify,
    list_redoable_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
)
```

Add right after it:

```python
from .identify import extract_metadata
```

- [ ] **Step 2: Drop the three removed input fields**

Find:

```python
        self.title_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.lyrics_var = tk.StringVar()
        self.tab_pdf_var = tk.StringVar()
        self.chords_text_var = tk.StringVar()
        self.work_dir_var = tk.StringVar()
```

Replace with:

```python
        self.title_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.work_dir_var = tk.StringVar()
```

- [ ] **Step 3: Simplify the form rows**

Find:

```python
        self._add_row(frame, 0, "Song title:", self.title_var)
        self._add_file_row(
            frame, 1, "Audio file (mp3/wav):", self.audio_var,
            [("Audio files", "*.mp3 *.wav *.m4a *.flac"), ("All files", "*.*")],
        )
        self._add_file_row(
            frame, 2, "Lyrics text file:", self.lyrics_var,
            [("Text files", "*.txt"), ("All files", "*.*")],
        )
        self._add_file_row(
            frame, 3, "Tab/chords PDF:", self.tab_pdf_var,
            [("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        self._add_file_row(
            frame, 4, "Or: chord-over-lyric text file:", self.chords_text_var,
            [("Text files", "*.txt"), ("All files", "*.*")],
        )
        self._add_row(frame, 5, "Work directory:", self.work_dir_var)

        note = ttk.Label(
            frame,
            text="Supply either the tab PDF, or the chord-over-lyric text file (skips\n"
            "Claude/vision entirely for chord placement -- use this if the PDF path fails\n"
            "with a content-filtering error).",
            foreground="#666",
            justify="left",
        )
        note.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 8))

        self.generate_button = ttk.Button(frame, text="Generate Video", command=self._on_generate)
        self.generate_button.grid(row=7, column=0, columnspan=3, pady=8)
```

Replace with:

```python
        self._add_row(frame, 0, "Song title (auto-filled, editable):", self.title_var)
        self._add_file_row(
            frame, 1, "Audio file (mp3/wav):", self.audio_var,
            [("Audio files", "*.mp3 *.wav *.m4a *.flac"), ("All files", "*.*")],
            on_selected=self._on_audio_selected,
        )
        self._add_row(frame, 2, "Work directory:", self.work_dir_var)

        note = ttk.Label(
            frame,
            text="Title, artist, and lyrics are identified automatically from the audio\n"
            "file's tags and online lookup -- edit the title above if it's wrong. Chords\n"
            "are detected directly from the audio; no tab or chord sheet is needed.",
            foreground="#666",
            justify="left",
        )
        note.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))

        self.generate_button = ttk.Button(frame, text="Generate Video", command=self._on_generate)
        self.generate_button.grid(row=4, column=0, columnspan=3, pady=8)
```

- [ ] **Step 4: Let `_add_file_row` take an optional selection callback**

Find:

```python
    def _add_file_row(
        self, frame: ttk.Frame, row: int, label: str, var: tk.StringVar, filetypes: list
    ) -> None:
        self._add_row(frame, row, label, var)

        def browse() -> None:
            path = filedialog.askopenfilename(filetypes=filetypes)
            if path:
                var.set(path)

        ttk.Button(frame, text="Browse...", command=browse).grid(row=row, column=2, padx=4)
```

Replace with:

```python
    def _add_file_row(
        self, frame: ttk.Frame, row: int, label: str, var: tk.StringVar, filetypes: list,
        on_selected=None,
    ) -> None:
        self._add_row(frame, row, label, var)

        def browse() -> None:
            path = filedialog.askopenfilename(filetypes=filetypes)
            if path:
                var.set(path)
                if on_selected is not None:
                    on_selected(path)

        ttk.Button(frame, text="Browse...", command=browse).grid(row=row, column=2, padx=4)
```

- [ ] **Step 5: Add the auto-identify-on-audio-selection methods**

Find (the `_on_title_changed` method, which stays unchanged and is a good anchor):

```python
    def _on_title_changed(self, *_args) -> None:
        if not self._running:
            slug = _slugify(self.title_var.get())
            self.work_dir_var.set(str(PROJECT_ROOT / "work" / slug))
```

Add right after it:

```python
    def _on_audio_selected(self, path: str) -> None:
        """Best-effort auto-fill of the title once an audio file is picked --
        never overwrites a title the owner already typed, and any failure
        (offline, unreadable file) is silently ignored: Generate still works
        with an auto-identified title computed fresh inside run_pipeline's own
        identify stage regardless of whether this GUI-side preview succeeds."""
        if self.title_var.get().strip():
            return
        thread = threading.Thread(target=self._identify_worker, args=(path,), daemon=True)
        thread.start()

    def _identify_worker(self, path: str) -> None:
        try:
            info = extract_metadata(Path(path))
        except Exception:
            return
        self.root.after(0, lambda: self._apply_identified_title(info.title))

    def _apply_identified_title(self, title: str) -> None:
        if not self.title_var.get().strip():  # still empty -- no manual edit arrived meanwhile
            self.title_var.set(title)
```

- [ ] **Step 6: Simplify Generate's validation and worker call**

Find:

```python
    def _on_generate(self) -> None:
        if self._running:
            return

        title = self.title_var.get().strip()
        audio = self.audio_var.get().strip()
        lyrics = self.lyrics_var.get().strip()
        tab_pdf = self.tab_pdf_var.get().strip()
        chords_text = self.chords_text_var.get().strip()
        work_dir = self.work_dir_var.get().strip()

        if not title:
            messagebox.showerror("Missing input", "Song title is required.")
            return
        if not audio:
            messagebox.showerror("Missing input", "Audio file is required.")
            return
        if not tab_pdf and not chords_text:
            messagebox.showerror(
                "Missing input", "Supply either a tab PDF or a chord-over-lyric text file."
            )
            return
        if not work_dir:
            messagebox.showerror("Missing input", "Work directory is required.")
            return

        self._running = True
        self.generate_button.state(["disabled"])
        self.redo_button.state(["disabled"])
        self.status_var.set("Starting...")
        self._clear_log()

        thread = threading.Thread(
            target=self._run_worker,
            args=(
                Path(audio),
                Path(tab_pdf) if tab_pdf else None,
                Path(work_dir),
                title,
                Path(lyrics) if lyrics else None,
                Path(chords_text) if chords_text else None,
            ),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)
```

Replace with:

```python
    def _on_generate(self) -> None:
        if self._running:
            return

        title = self.title_var.get().strip()  # optional -- run_pipeline's own
        audio = self.audio_var.get().strip()   # identify stage falls back if blank
        work_dir = self.work_dir_var.get().strip()

        if not audio:
            messagebox.showerror("Missing input", "Audio file is required.")
            return
        if not work_dir:
            messagebox.showerror("Missing input", "Work directory is required.")
            return

        self._running = True
        self.generate_button.state(["disabled"])
        self.redo_button.state(["disabled"])
        self.status_var.set("Starting...")
        self._clear_log()

        thread = threading.Thread(
            target=self._run_worker,
            args=(Path(audio), Path(work_dir), title or None),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)
```

- [ ] **Step 7: Retarget Redo to resume at fetch_lyrics**

Find:

```python
        thread = threading.Thread(
            target=self._run_worker,
            args=(audio_path, None, song_dir, title, None, None, "align"),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)
```

Replace with:

```python
        thread = threading.Thread(
            target=self._run_worker,
            args=(audio_path, song_dir, title, "fetch_lyrics"),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self._poll_queue)
```

- [ ] **Step 8: Simplify `_run_worker`'s signature**

Find:

```python
    def _run_worker(
        self,
        audio_path: Path,
        tab_pdf_path: Path | None,
        work_dir: Path,
        title: str,
        lyrics_file: Path | None,
        chords_text_file: Path | None,
        start_stage: str = "separate",
    ) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        try:
            out_path = run_pipeline(
                audio_path,
                tab_pdf_path,
                work_dir,
                title,
                start_stage=start_stage,
                lyrics_file=lyrics_file,
                chords_text_file=chords_text_file,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
            )
            self._queue.put(("done", str(out_path)))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
```

Replace with:

```python
    def _run_worker(
        self,
        audio_path: Path,
        work_dir: Path,
        title: str | None = None,
        start_stage: str = "identify",
    ) -> None:
        writer = _QueueWriter(self._queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        try:
            out_path = run_pipeline(
                audio_path,
                work_dir,
                title,
                start_stage=start_stage,
                progress_callback=lambda stage: self._queue.put(("stage", stage)),
            )
            self._queue.put(("done", str(out_path)))
        except Exception as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
```

- [ ] **Step 9: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/test_gui.py -v`
Expected: 10 passed (unchanged — this task didn't touch `_slugify`/`_split_log_text`).

- [ ] **Step 10: Manually smoke-test the GUI launches without error**

Run: `.venv/bin/python -m lyricvideo.gui &` then check the window opens with three
rows (Song title, Audio file, Work directory) and no Tab/chords-PDF or lyrics-file
rows, then close it. (A full end-to-end Generate run needs real API keys and takes
several minutes — that's covered by Task 11's manual smoke check, not here.)

- [ ] **Step 11: Commit**

```bash
git add lyricvideo/gui.py
git commit -m "Simplify GUI to audio-only input with auto-identified title"
```

---

## Task 9: Delete the superseded tab-PDF/vision/OCR modules

Everything here was already confirmed disconnected by Task 7's Step 31 full-suite run
(their own tests fail because nothing imports these modules anymore). This task
removes the dead code and its tests, and drops their now-unused dependencies.

**Files:**
- Delete: `lyricvideo/pdf_parse.py`, `lyricvideo/vision_parse.py`,
  `lyricvideo/plaintext_chords.py`, `lyricvideo/ocr_parse.py`, `lyricvideo/ocr_clean.py`,
  `lyricvideo/instrumental_chords.py`
- Delete: `tests/test_pdf_parse.py`, `tests/test_vision_parse.py`,
  `tests/test_plaintext_chords.py`, `tests/test_ocr_parse.py`, `tests/test_ocr_clean.py`,
  `tests/test_instrumental_chords.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Confirm nothing still references these modules**

Run:

```bash
grep -rln "pdf_parse\|vision_parse\|plaintext_chords\|ocr_parse\|ocr_clean\|instrumental_chords" \
    lyricvideo/*.py
```

Expected: no output (the six files about to be deleted may match their own filenames
if grepping filenames too, but this greps file *contents* under `lyricvideo/*.py` —
confirm the only hits, if any, are inside the files being deleted themselves, not in
`pipeline.py`/`gui.py`/`layout.py`/`render.py`/`assemble.py`/`models.py`).

- [ ] **Step 2: Delete the modules and their tests**

```bash
git rm lyricvideo/pdf_parse.py lyricvideo/vision_parse.py lyricvideo/plaintext_chords.py \
       lyricvideo/ocr_parse.py lyricvideo/ocr_clean.py lyricvideo/instrumental_chords.py
git rm tests/test_pdf_parse.py tests/test_vision_parse.py tests/test_plaintext_chords.py \
       tests/test_ocr_parse.py tests/test_ocr_clean.py tests/test_instrumental_chords.py
```

- [ ] **Step 3: Remove their now-unused dependencies**

In `requirements.txt`, remove these two lines (both were only ever used by
`pdf_parse.py`/`ocr_parse.py`, now deleted):

```
pdfplumber>=0.10
pytesseract>=0.3
```

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: every remaining test passes; no collection errors from the deleted files.

- [ ] **Step 5: Reinstall to confirm requirements.txt is still internally consistent**

Run: `.venv/bin/pip install -r requirements.txt`
Expected: completes without error (this doesn't uninstall `pdfplumber`/`pytesseract`
from the venv — that's harmless; it only confirms the trimmed file itself still
installs cleanly).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt
git commit -m "Delete superseded tab-PDF/vision/OCR modules and their tests"
```

---

## Task 10: Update CLAUDE.md and CLAUDE_HISTORY.md for the merged pipeline

CLAUDE.md still describes the pre-merge tab-PDF pipeline (verified by reading it in
full before writing this task) — it must be updated to genuinely current-state facts
now that the merge is implemented. The narrative (why this changed) goes in
CLAUDE_HISTORY.md.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/CLAUDE_HISTORY.md`

- [ ] **Step 1: Update the opening description**

Find:

```
# PlayAlongVideoProduction

Generates synced lyric+chord "play along" videos from a tab PDF and an audio
file: scrolling lyrics with chord names timed over the words they change on,
composited over an AI-generated, Ken-Burns-panned background image that
changes per lyric line to follow the song's meaning. Full design in
`docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md`; the
build plan (all steps checked off) is in
`docs/superpowers/plans/2026-09-06-tab-pdf-video-generator.md`.
```

Replace with:

```
# PlayAlongVideoProduction

Generates synced lyric+chord "play along" videos from nothing but an audio file:
karaoke-style scrolling lyrics (forced-aligned to the real vocal stem) and a
NOW/NEXT/timeline chord bar (chords detected directly from the audio, never from a
tab or chord sheet) composited over an AI-generated, Ken-Burns-panned background
image that changes per lyric line — and per active chord during instrumental
gaps — to follow the song. Original tab-PDF-input design in
`docs/superpowers/specs/2026-09-06-tab-pdf-video-generator-design.md` (superseded —
see the MP3-only merge design below); that build plan (all steps checked off) is in
`docs/superpowers/plans/2026-09-06-tab-pdf-video-generator.md`. The MP3-only merge
design is `docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md`, plan
`docs/superpowers/plans/2026-09-09-mp3-only-chord-merge.md`.
```

- [ ] **Step 2: Update "Running it"**

Find:

```
- **GUI (normal use):** double-click the `PlayAlongVideoProduction` desktop icon, or
  run `./run_playalongvideoproduction.sh` from the repo root. Supply a title, audio
  file, and either a tab PDF or a chords-text file, then click Generate. Built with
  Tkinter (`lyricvideo/gui.py`).
- **CLI (staged/resumable, useful for debugging one stage):**
  ```bash
  cd /home/doug/PlayAlongVideoProduction
  .venv/bin/python -m lyricvideo.pipeline --audio <path> --tab-pdf <path> \
      --work-dir <dir> --title "<title>" [--stage separate|parse|align|images|render]
  ```
  `--stage` resumes from a later stage using artifacts already written to
  `--work-dir` by an earlier run — useful since `separate`/`align`/`images`
  are the slow/expensive stages.
```

Replace with:

```
- **GUI (normal use):** double-click the `PlayAlongVideoProduction` desktop icon, or
  run `./run_playalongvideoproduction.sh` from the repo root. Supply just an audio
  file — title/artist/lyrics are identified and fetched automatically, chords are
  detected directly from the audio, and the title field is an editable override, not
  a required input — then click Generate. Built with Tkinter (`lyricvideo/gui.py`).
- **CLI (staged/resumable, useful for debugging one stage):**
  ```bash
  cd /home/doug/PlayAlongVideoProduction
  .venv/bin/python -m lyricvideo.pipeline --audio <path> --work-dir <dir> \
      [--title "<override>"] \
      [--stage identify|separate|fetch_lyrics|align|detect_chords|images|render]
  ```
  `--stage` resumes from a later stage using artifacts already written to
  `--work-dir` by an earlier run — useful since `separate`/`fetch_lyrics`/
  `detect_chords`/`images` are the slow/expensive stages.
```

- [ ] **Step 3: Rewrite "Pipeline stages" for the 7-stage MP3-only pipeline**

Find (the entire section from the heading through the end of stage 5):

```
## Pipeline stages (`lyricvideo/pipeline.py`, `STAGES`)

1. **separate** (`separate.py`) — Demucs two-stem split of `--audio` into
   vocals/instrumental (CPU). Output path convention
   (`work_dir/htdemucs/<audio_stem>/{vocals,no_vocals}.wav`) is what makes
   `--stage` resumption work — later stages look for the file at that same
   path rather than re-running Demucs.
2. **parse** — extracts structured `(lyric line, [word_index, chord] pairs)`
   data from the input. Three input paths, in order of preference:
   - `--chords-text-file`: owner-typed plain chord-over-lyric text
     (`plaintext_chords.py`) — pure deterministic parsing, **no Claude call at
     all**. Use this when vision-based chord mapping keeps hitting Anthropic
     content-filtering on a song's lyrics (confirmed real and
     non-deterministic on some songs).
   - `--tab-pdf` with a real text layer: `pdf_parse.py` (pdfplumber),
     deterministic, no Claude call.
   - `--tab-pdf` that's scanned/image-only (raises `NoTextLayerError`): falls
     back to `vision_parse.py`, which requires `--lyrics-file` (owner-supplied
     plain lyrics). Claude only returns `[word_index, chord]` position pairs
     against text you already gave it — **it never generates or alters lyric
     text itself.**
   - `pdf_parse.py`'s line-clustering/chord-pairing logic is now a shared
     `_assemble_from_words()` helper (generic word dicts, caller-supplied
     y-tolerance) so a pytesseract-based OCR path (`ocr_parse.py`, unit-tested)
     can reuse it against pixel-unit coordinates. `ocr_parse.py` is not yet
     wired into `run_pipeline`'s fallback cascade — today a scanned PDF still
     only reaches `vision_parse.py`, never OCR.
3. **align** — forced word-level alignment (`align.py`) against the isolated
   vocal stem; `combine.py` merges alignment timing back onto the parsed
   lines/chords; `instrumental_chords.py` times chords that fall in
   instrumental (no-lyric) gaps between lines by anchoring to real
   frame-to-frame chroma novelty peaks in the no-vocals stem, with a minimum
   time-spacing constraint between chosen boundaries (rejecting candidates
   too close to each other OR to the gap's own start/end) so one sharp
   transition's smeared neighboring frames can't crowd out every other real
   chord change — see the 2026-09-08 history entry for the concrete failure
   mode this fixed.
4. **images** — `imagery.py`: one Claude call summarizes the whole song's
   gist once (`summarize_song_gist`), then each *unique* lyric line gets its
   own generated background image (Replicate), cached by line text so a
   repeated chorus reuses its image instead of paying to regenerate it. Also
   reuses any `images_backup_*/` archive left in the work dir before
   generating new images — back up rather than delete `images/` if you want
   to regenerate render-only changes without re-paying for images.
5. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling
   lyrics + chord flashes + Ken Burns pans over the audio into the final
   1080p mp4 (`work_dir/<slugified-title>.mp4`).
```

Replace with:

```
## Pipeline stages (`lyricvideo/pipeline.py`, `STAGES`)

1. **identify** (`identify.py`) — resolves title/artist/duration from ID3/Vorbis/
   M4A tags, filename parsing, and (if the artist is still unknown) lrclib-artist-
   consensus + MusicBrainz-by-duration lookups. Written to `work_dir/song_info.json`.
   A caller-supplied `--title` overrides the identified title for display/filename
   purposes only — artist/duration always come from this stage's own resolution.
2. **separate** (`separate.py`) — Demucs two-stem split of `--audio` into
   vocals/instrumental (CPU). Output path convention
   (`work_dir/htdemucs/<audio_stem>/{vocals,no_vocals}.wav`) is what makes
   `--stage` resumption work — later stages look for the file at that same
   path rather than re-running Demucs.
3. **fetch_lyrics** (`fetch_lyrics.py` + `vocal_onset.py`) — plain lyric-line
   text, no manual input required: a sidecar `.lrc`/`.txt` next to the audio file,
   then lrclib.net (edition-consensus voting across every matching-length record,
   using `vocal_onset.py`'s narrow vocal-onset-rise check to disambiguate
   disagreeing first-line candidates), then the `syncedlyrics` aggregator as a
   last resort. Written to `work_dir/lyric_lines.json`. Any timestamps a provider's
   LRC carries are discarded — real timing always comes from the next stage.
4. **align** — forced word-level alignment (`align.py`) against the isolated
   vocal stem, timing `fetch_lyrics`'s text; `combine.py` merges the timing onto
   the lines. `align_words()` has no idea where its input words came from, so this
   is the same alignment mechanism the original tab-PDF design used.
5. **detect_chords** (`detect_chords.py` + `chord_theory.py`) — real chord
   identity, entirely independent of lyrics: harmonic/percussive separation → CQT
   chroma → beat-sync → template match against 12-root × {maj, min, 7, min7, maj7}
   → key-aware (Krumhansl-Schmuckler) Viterbi decoding, run on Demucs's own
   `no_vocals.wav`. Produces one `ChordTrack` (events + key + bpm) covering the
   whole song, saved onto the `Song`. This is the ONLY chord source in this
   program — there is no tab/sheet input to defer to, and audio-detected chords
   always win.
6. **images** — `imagery.py`: one Claude call summarizes the whole song's gist
   once (`summarize_song_gist`), then each *unique* lyric line AND each distinct
   chord label that occurs during an instrumental gap (`_instrumental_chord_labels`
   in `pipeline.py`) gets its own generated background image (Replicate), cached by
   content hash so a repeated chorus or a repeated chord anywhere in the song
   reuses one image instead of paying to regenerate it. Also reuses any
   `images_backup_*/` archive left in the work dir before generating new images.
7. **render** — `assemble.py`/`layout.py`/`render.py`: composites scrolling lyrics
   (karaoke word-highlight sweep, Ken Burns pans) plus a NOW/NEXT/segmented-
   timeline chord bar and a Key/BPM badge over the audio into the final 1080p mp4
   (`work_dir/<slugified-title>.mp4`). During an instrumental gap (past a line's
   own `end_time`, before the next line's `start_time`, or outside any line at
   all) the background image follows the active chord instead of freezing on the
   last-sung line — `layout.py`'s `_in_a_line()` decides which applies. No song
   title or artist text is drawn into the frame anywhere (owner decision,
   2026-09-09) — only the chord bar and the Key/BPM badge were added to the frame.
```

- [ ] **Step 4: Update "Redo an Existing Song"**

Find:

```
Re-runs a previously completed song through the current code, picking up
fixes made since the original run (e.g. the 2026-09-08 instrumental-chord
timing fix) without re-parsing the tab or re-running Demucs.
`pipeline.py`'s `run_pipeline()` only requires `tab_pdf_path`/
`chords_text_file` when `start_stage` will actually reach the parse stage
— a redo resumes at `"align"`, reusing the existing `parsed_tab.json` and
Demucs stems, via `list_redoable_songs()`/`load_redo_inputs()` (reads the
original `audio_path`/`title` back off the song's own `lyrics_timed.json`,
so the owner never re-browses for the original files). In `gui.py`, the
```

Replace with:

```
Re-runs a previously completed song through the current code, picking up fixes made
since the original run without re-running Demucs. A redo resumes at
`"fetch_lyrics"` (there is no `parsed_tab.json` to reuse post-merge — lyrics are
re-fetched and chords re-detected fresh on every redo, both cheap relative to
Demucs/images), reusing the existing Demucs stems and `work_dir/song_info.json`
(read via the `else` branch of `run_pipeline`'s `identify` stage, since a
`start_stage` past `"identify"` never re-runs it), via `list_redoable_songs()`/
`load_redo_inputs()` (reads the original `audio_path`/`title` back off the song's
own `lyrics_timed.json`, so the owner never re-browses for the original files). In
`gui.py`, the
```

- [ ] **Step 5: Write the CLAUDE_HISTORY.md entry**

Read the current end of `docs/CLAUDE_HISTORY.md` first to anchor the edit precisely
(its content shifted across this session's earlier commits — Step 1 below uses
whatever the file's actual last entry is, not a hardcoded guess), then append:

```markdown

## 2026-09-09 — MP3-only merge: LyricChord's chord detection replaces tab-PDF input

Implemented `docs/superpowers/specs/2026-09-09-chord-detection-merge-design.md` (plan:
`docs/superpowers/plans/2026-09-09-mp3-only-chord-merge.md`). Owner was impressed by
two things in a sibling project, LyricChord (`/home/doug/Lyric+Chord`, MIT licensed):
its chord detection ("spot on") and that it needs nothing but the audio file. Both
turned out to come from the same set of modules, so both got ported wholesale —
`identify.py`/`text_clean.py` (from LyricChord's `metadata.py`), `fetch_lyrics.py`/
`vocal_onset.py` (from `lyrics.py`/`vocal.py`), `detect_chords.py`/`chord_theory.py`
(from `chords/local.py`/`theory.py`), `audio_decode.py` (from `utils/audio.py` +
`utils/ffmpeg.py`) — while this program's own forced-alignment lyric sync, AI
Ken-Burns images, and Redo-an-Existing-Song feature were kept, since the owner
considers this program's lyric/vocal sync meaningfully better than trusting
LyricChord's third-party LRC timestamps directly.

Net effect: `run_pipeline()` now needs nothing but an audio file. The tab-PDF/
chords-text/vision-fallback input path (`pdf_parse.py`, `vision_parse.py`,
`plaintext_chords.py`, and the never-wired-in `ocr_parse.py`/`ocr_clean.py`
groundwork from the 2026-09-09 OCR-fallback commit earlier this same day) is deleted
entirely, along with `instrumental_chords.py`'s chroma-novelty gap-timing (superseded
— full-song `detect_chords` already assigns real chords through instrumental
sections, not just timing boundaries for chords a human already supplied).

Two owner-requested additions beyond the original chord-detection ask, both folded
into the spec before implementation:
- The bottom chord bar (NOW box, NEXT box + countdown, duration-proportional
  scrolling timeline, ported from LyricChord's `_draw_chords`) replaces the old
  above-word chord-flash display entirely — the owner wanted the complete display,
  not a partial port.
- Background images now follow the active chord during instrumental gaps instead of
  freezing on the last-sung line's image (`layout.py`'s `_in_a_line()` + the
  `images` stage's new per-instrumental-chord generation) — the owner noticed the
  freeze specifically while reviewing this same design.

`Song.instrumental_chords: list[InstrumentalChord]` became `Song.chord_track:
ChordTrack`; `ChordWord` (with its now-permanently-`None` `.chord` field) became
`Word`. `requirements.txt` gained `mutagen`/`requests`/`syncedlyrics`, lost
`pdfplumber`/`pytesseract`. The Update Available feature's `LyricVideoGen-releases`
distribution repo name is unaffected by any of this (separate, already-deferred
decision from the 2026-09-09 project rename).
```

- [ ] **Step 6: Verify the size gate (if this repo has adopted AITrading's CLAUDE.md
  byte-size pre-commit check)**

Run: `wc -c CLAUDE.md`. If a pre-commit hook in this repo enforces a byte limit
(check `.githooks/pre-commit` for a `wc -c`/size check, mirroring AITrading's), confirm
the file is under it; if not, this repo has no such gate and nothing to check.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md docs/CLAUDE_HISTORY.md
git commit -m "Document the MP3-only chord-detection merge in CLAUDE.md/HISTORY"
```

---

## Task 11: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full automated test suite one more time**

Run: `cd /home/doug/PlayAlongVideoProduction && .venv/bin/python -m pytest tests/ -v`
Expected: every test passes, zero collection errors, zero skips beyond the pre-existing
`test_font_path` skip-if-no-font fixture behavior.

- [ ] **Step 2: Confirm the deleted modules left no stray references anywhere**

Run:

```bash
grep -rn "ChordWord\|InstrumentalChord\|InstrumentalBlock\|parsed_tab\.json\|tab_pdf\|chords_text_file\|lyrics_file" \
    lyricvideo/*.py CLAUDE.md
```

Expected: no output (a genuine hit here means something was missed in Tasks 7-9).

- [ ] **Step 3: Manual end-to-end smoke test with a real song**

This needs real `ANTHROPIC_API_KEY`/`REPLICATE_API_TOKEN` in `.env` and takes several
minutes (Demucs + lyric/chord fetching + real image generation) — run it once, not
per-task. Use a song already known to work from a prior LyricVideoGen run if one
exists under `work/` (e.g. `work/eye-in-the-sky/` per this repo's own history), so
there's a known-good reference video to compare against:

```bash
cd /home/doug/PlayAlongVideoProduction
.venv/bin/python -m lyricvideo.pipeline --audio <path-to-a-real-mp3> \
    --work-dir /tmp/mp3-only-smoke-test
```

Confirm, by eye, on the resulting mp4:
- Lyrics scroll and karaoke-highlight in sync with the actual vocals (the sync
  quality this whole merge was meant to preserve).
- The chord bar (NOW box, NEXT box + countdown, scrolling timeline) is visible,
  legible over the background, and the chords audibly seem to match what's playing.
- No song title or artist text appears anywhere in the frame.
- During any instrumental section, the background image changes when the chord
  changes rather than staying frozen.
- The Key/BPM badge shows a plausible key and tempo.

If lyrics come back empty for this particular song (a real, expected possibility —
`fetch_lyric_lines` returns `[]` when no provider has anything), that is not a bug in
this merge; rerun with a well-known, popular song to validate the sync/chord/image
behavior instead.

- [ ] **Step 4: Manual Redo smoke test**

In the GUI, use "Redo an Existing Song" against the song from Step 3. Confirm it
resumes quickly (no re-run of Demucs — check the log for the "separate" stage being
skipped) and completes.

- [ ] **Step 5: Report completion**

Once Steps 1-4 all pass, the merge is complete: PlayAlongVideoProduction generates a
full play-along video from nothing but an audio file, with LyricChord's chord
detection and display, and this program's own forced-alignment sync and AI imagery.
