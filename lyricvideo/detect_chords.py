"""Local chord detection, ported from LyricChord's chords/local.py and later
upgraded from plain CQT chroma to essentia's HPCP feature.

Pipeline:
  audio -> harmonic/percussive separation -> HPCP chroma -> beat-synchronous chroma
        -> cosine similarity against chord templates -> key prior
        -> Viterbi decoding with a "sticky" transition matrix -> merged segments

Beat tracking, the Viterbi decoder, key-snapping, and silence detection are all
still librosa -- only the chroma FEATURE itself comes from essentia's HPCP now
(real incident, 2026-09-13: an owner report of the chord bar being "out of
sync," especially in the first half of a song, traced to measurable chord-
label flicker concentrated in quieter/sparser passages -- verified on real
songs, not assumed: e.g. "Bridge Over Troubled Water" had 71 chord segments
averaging 2.1s in its first half vs 42 averaging 3.5s in its second, a
consistent pattern across every quiet-intro-to-loud-chorus song checked).
essentia's HPCP does harmonic summation across overtones (`harmonics=8`)
rather than librosa's plain per-bin CQT magnitude, giving materially more
stable, more musically plausible chords especially where the plain-CQT
signal was weakest -- verified against real, well-known songs' actual
published chord progressions, not just a flicker-count metric alone (see
docs/CLAUDE_HISTORY.md's 2026-09-13 entry for the specific before/after).
Still "not as accurate as a trained model" (no ML/training data involved,
same as before), but noticeably better than the plain-CQT baseline it
replaced.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import chord_theory as theory
from .audio_decode import decode_audio
from .models import ChordEvent, ChordTrack

SR = 22050
HOP = 512
HPCP_FRAME_SIZE = 2048         # ~93ms at SR=22050 -- enough low-frequency resolution
# essentia's HPCP bin 0 is NOT pitch class C -- empirically verified with a pure
# 261.63Hz (C4) sine tone fed through this exact Windowing/Spectrum/SpectralPeaks/
# HPCP chain: the resulting HPCP vector peaks at bin 8, not bin 0. Every other
# module in this codebase (chord_theory.py, ChordTrack, etc.) uses the standard
# C=0, C#=1, ..., B=11 convention, so HPCP output must be rolled by -HPCP_C_BIN
# before it can be compared against chord_theory's templates or key logic.
HPCP_C_BIN = 8
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


def _hpcp_chroma(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Frame-by-frame HPCP (harmonic pitch class profile) chroma via essentia,
    already rolled to chord_theory's C=0 convention. Returns (chroma, frame_times)
    where chroma is shape (12, n_hpcp_frames) -- a DIFFERENT frame count/grid
    than librosa's beat-tracking frames, since essentia's FrameGenerator and
    librosa's centered STFT-style framing use different edge conventions; this
    is why beat-syncing (below) matches frames to beat segments by TIME rather
    than by frame index."""
    import essentia.standard as es  # imported lazily: only needed here

    windowing = es.Windowing(type="blackmanharris62")
    spectrum = es.Spectrum(size=HPCP_FRAME_SIZE)
    spectral_peaks = es.SpectralPeaks(
        orderBy="magnitude", magnitudeThreshold=1e-5, minFrequency=40, maxFrequency=5000, maxPeaks=100,
    )
    hpcp = es.HPCP(
        harmonics=8, size=12, bandPreset=True, minFrequency=40, maxFrequency=5000,
        bandSplitFrequency=500, weightType="cosine", nonLinear=False, windowSize=1.0,
    )

    frames = []
    for frame in es.FrameGenerator(y.astype(np.float32), frameSize=HPCP_FRAME_SIZE, hopSize=HOP, startFromZero=True):
        spectrum_frame = spectrum(windowing(frame))
        freqs, mags = spectral_peaks(spectrum_frame)
        frames.append(hpcp(freqs, mags))

    chroma = np.array(frames).T  # (12, n_hpcp_frames)
    chroma = np.roll(chroma, -HPCP_C_BIN, axis=0)
    frame_times = (np.arange(chroma.shape[1]) * HOP + HPCP_FRAME_SIZE / 2) / SR
    return chroma, frame_times


def _sync_chroma_to_segments(
    chroma: np.ndarray, frame_times: np.ndarray, seg_times: np.ndarray,
) -> np.ndarray:
    """Aggregates HPCP frames (by their center time) into each [seg_times[i],
    seg_times[i+1]) beat segment via the median, same aggregation
    librosa.util.sync used for the plain-CQT-chroma predecessor of this
    function. A segment with no HPCP frame center actually inside it (can
    happen for a very short beat-to-beat gap) falls back to the single
    nearest frame by time, rather than producing an all-zero vector."""
    seg_chroma = np.empty((chroma.shape[0], len(seg_times) - 1))
    for i in range(len(seg_times) - 1):
        mask = (frame_times >= seg_times[i]) & (frame_times < seg_times[i + 1])
        if mask.any():
            seg_chroma[:, i] = np.median(chroma[:, mask], axis=1)
        else:
            nearest = np.argmin(np.abs(frame_times - (seg_times[i] + seg_times[i + 1]) / 2))
            seg_chroma[:, i] = chroma[:, nearest]
    return seg_chroma


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

    # 2) HPCP chroma (harmonic pitch class profile) -- see module docstring for
    # why this replaced a plain CQT chroma. rms/n_frames below still use
    # librosa's own hop-frame grid (unchanged), since HPCP has a different
    # frame count/grid and is synced to beat segments by time, not index.
    hpcp_chroma, hpcp_times = _hpcp_chroma(y_harm)

    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    n_frames = rms.shape[0]

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
    seg_times = librosa.frames_to_time(frames=boundaries, sr=SR, hop_length=HOP)

    seg_chroma = _sync_chroma_to_segments(hpcp_chroma, hpcp_times, seg_times)
    seg_chroma = seg_chroma / (np.linalg.norm(seg_chroma, axis=0, keepdims=True) + 1e-9)

    # 4) Template matching.
    qualities = theory.TRIADS + (theory.SEVENTHS if include_seventh_chords else [])
    chords, templates = theory.build_templates(qualities)
    sim = templates @ seg_chroma

    tonic, mode, _key_conf = theory.estimate_key(hpcp_chroma.mean(axis=1))
    if snap_chords_to_key:
        diatonic = theory.diatonic_chords(tonic, mode, include_sevenths=True)
        prior = np.array([1.0 if c in diatonic else NON_DIATONIC_PENALTY for c in chords])
        sim = sim * prior[:, None]

    # 5) Viterbi smoothing: chords tend to persist for several beats.
    prob = _softmax(sim / SOFTMAX_TEMPERATURE, axis=0)
    transition = librosa.sequence.transition_loop(n_states=len(chords), prob=STAY_PROBABILITY)
    states = librosa.sequence.viterbi(prob=prob, transition=transition)

    # 6) Silence detection -> "N" (no chord).
    seg_rms = np.array([rms[s].mean() if s.stop > s.start else 0.0 for s in segs])
    silent = seg_rms < max(1e-4, SILENCE_RATIO * seg_rms.max())

    use_flats = prefer_flats and theory.key_uses_flats(tonic, mode)
    events: list[ChordEvent] = []
    for i, state in enumerate(states):
        label = "N" if silent[i] else theory.spell(chords[state][0], chords[state][1], use_flats)
        start, end = float(seg_times[i]), float(seg_times[i + 1])
        if events and events[-1].label == label:
            events[-1].end = end
        else:
            events.append(ChordEvent(start, end, label))

    events = _merge_short_events(events, min_chord_seconds)
    duration = y.size / SR
    if events:
        events[-1].end = max(events[-1].end, duration)

    return ChordTrack(events=events, key=theory.key_name(tonic, mode, prefer_flats), bpm=round(tempo, 1))
