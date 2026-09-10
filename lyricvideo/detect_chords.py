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

from pathlib import Path

import numpy as np

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


def detect_chords(
    path: Path,
    *,
    snap_chords_to_key: bool = SNAP_CHORDS_TO_KEY,
    prefer_flats: bool = PREFER_FLATS,
    include_seventh_chords: bool = INCLUDE_SEVENTH_CHORDS,
    min_chord_seconds: float = MIN_CHORD_SECONDS,
) -> ChordTrack:
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
    qualities = theory.TRIADS + (theory.SEVENTHS if include_seventh_chords else [])
    chords, templates = theory.build_templates(qualities)
    sim = templates @ seg_chroma

    tonic, mode, _key_conf = theory.estimate_key(chroma.mean(axis=1))
    if snap_chords_to_key:
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

    use_flats = prefer_flats and theory.key_uses_flats(tonic, mode)
    times = librosa.frames_to_time(frames=boundaries, sr=SR, hop_length=HOP)
    events: list[ChordEvent] = []
    for i, state in enumerate(states):
        label = "N" if silent[i] else theory.spell(chords[state][0], chords[state][1], use_flats)
        start, end = float(times[i]), float(times[i + 1])
        if events and events[-1].label == label:
            events[-1].end = end
        else:
            events.append(ChordEvent(start, end, label))

    events = _merge_short_events(events, min_chord_seconds)
    duration = y.size / SR
    if events:
        events[-1].end = max(events[-1].end, duration)

    return ChordTrack(events=events, key=theory.key_name(tonic, mode, prefer_flats), bpm=round(tempo, 1))
