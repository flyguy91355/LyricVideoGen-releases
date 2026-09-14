"""Chord detection via crema (Brian McFee's trained CNN/CRNN chord recognizer),
replacing an earlier hand-tuned CQT-chroma + template-matching + Viterbi pipeline.

Real incident, 2026-09-13: the owner reported the chord bar being out of sync,
especially in the first half of a song. Investigation (docs/CLAUDE_HISTORY.md)
found real, measured chord-label flicker concentrated in quieter/sparser
passages -- an inherent accuracy ceiling of hand-crafted chroma-template
matching, not a timing bug. A follow-up attempt (swapping to essentia's HPCP
chroma feature, keeping the same template-matching/Viterbi machinery) fixed
that on 3 acoustic/ballad test songs but caused a confirmed regression on
distorted, riff-driven material and was reverted the same night.

crema is a real trained model (McFee & Bello, ISMIR 2017/2019), evaluated
directly against several of this project's own real songs -- including the
exact song that broke the HPCP attempt -- and found to produce dramatically
more musically plausible, stable chords across acoustic ballads, blues,
grunge (a song this project's own history flags as "a known hard case for
the chroma-based detector"), and 70s rock alike. See CLAUDE_HISTORY's
2026-09-13 entries for the full investigation and evidence.

crema needs an old TensorFlow/Keras/scikit-learn stack that only has wheels
for Python 3.11 (not 3.12) -- this project's whole venv was rebuilt on 3.11
to accommodate it (see requirements.txt), rather than running it in a
separate environment/subprocess, since a real combined-install test proved
crema, torch, and demucs all coexist correctly in one environment.

crema handles beat-independent segmentation, chord identity, AND silence
("N") detection all as part of its own trained output -- unlike the old
pipeline, this module no longer needs its own beat-tracking-based
segmentation or RMS-based silence heuristic for chord identity. Key and BPM
are still estimated independently via librosa (unrelated to chord identity,
never implicated in either past incident), purely for the on-screen Key/BPM
badge and the countdown lead-in's tempo.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import chord_theory as theory
from .audio_decode import decode_audio
from .models import ChordEvent, ChordTrack

SR = 22050
HOP = 512
# crema is trained on real recordings, which always carry some noise floor even
# in "silent" passages -- true digital silence (exact zero amplitude, as in a
# synthetic test tone or a rare fully-null stem) is out-of-distribution for it,
# and it does NOT reliably output its own "N" class for that case: verified
# live, 2026-09-13, crema guessed "F:7/b7" at confidence 0.26 for 3 full
# seconds of true digital silence, well below any real chord's typical
# confidence (min 0.30 observed across a real, correctly-detected song's own
# quietest legitimate passages) but not safely distinguishable from those by
# confidence alone. An ABSOLUTE (not peak-relative) RMS floor catches only
# genuinely silent audio without the old pipeline's own bug (a peak-relative
# threshold that misclassified quiet-but-real passages as silence).
_ABSOLUTE_SILENCE_RMS = 1e-4

# LyricChord's own Settings defaults (config.py), fixed here since this program has
# no per-run chord-detection settings UI.
SNAP_CHORDS_TO_KEY = True
PREFER_FLATS = True
MIN_CHORD_SECONDS = 0.5
INCLUDE_SEVENTH_CHORDS = False

# crema/mir_eval always spells roots with sharps (e.g. "D#:maj", never "Eb:maj") --
# accept both spellings when parsing so this stays robust to either convention.
_PITCH_CLASS: dict[str, int] = {name: i for i, name in enumerate(theory.NOTES_SHARP)}
_PITCH_CLASS.update({name: i for i, name in enumerate(theory.NOTES_FLAT)})

# crema's chord vocabulary (602 classes with inversions) is far richer than this
# app's deliberately small, playable set (see chord_theory.py's own docstring on
# why: qualities are kept small so the on-screen chords stay playable for a
# strumming guitarist/pianist). Every quality crema can output collapses to one
# of this app's 5 (maj/min/7/min7/maj7) -- extended/altered qualities collapse to
# their closest simple relative (e.g. a half-diminished 7th, which has a minor
# third and minor seventh, collapses to min7; a sus chord, which has no third,
# defaults to major as the more common realization); slash-chord inversions and
# extended tensions (9ths/11ths/13ths) are dropped entirely, keeping only the
# root and base triad/seventh quality.
_QUALITY_TO_TRIAD_OR_SEVENTH: dict[str, str] = {
    "maj": "maj", "min": "min", "maj7": "maj7", "min7": "min7", "7": "7",
    "dim": "min", "dim7": "min", "hdim7": "min7", "aug": "maj",
    "sus2": "maj", "sus4": "maj", "maj6": "maj", "min6": "min",
    "9": "7", "maj9": "maj7", "min9": "min7", "11": "7", "13": "7",
    "1": "maj", "5": "maj",
}
# When include_seventh_chords is False, every 7th-family quality above collapses
# further down to its base triad (matches the old pipeline's behavior: with
# sevenths disabled, only plain maj/min triads could ever be output at all).
_SEVENTH_TO_TRIAD: dict[str, str] = {"7": "maj", "min7": "min", "maj7": "maj"}


def _parse_crema_label(label: str) -> tuple[int, str] | None:
    """Parses a crema/mir_eval-style chord label ("F#:min7", "C:maj/5", "N") into
    (root pitch class, raw quality string). Returns None for "N" (no chord) or
    "X" (model couldn't determine one) or any unrecognized root -- all treated
    as no-chord, since guessing would be worse than admitting uncertainty."""
    if label in ("N", "X"):
        return None
    root_name, _, rest = label.partition(":")
    root = _PITCH_CLASS.get(root_name)
    if root is None:
        return None
    quality = rest.split("/", 1)[0] or "maj"
    return root, quality


def _simplify_chord_label(label: str, include_seventh_chords: bool, use_flats: bool) -> str:
    """Collapses one of crema's raw chord labels down to this app's own 5-quality
    vocabulary, respelled with this song's own detected sharp/flat convention
    (crema always spells with sharps; the app's display follows the song's key)."""
    parsed = _parse_crema_label(label)
    if parsed is None:
        return "N"
    root, raw_quality = parsed
    quality = _QUALITY_TO_TRIAD_OR_SEVENTH.get(raw_quality, "maj")
    if not include_seventh_chords:
        quality = _SEVENTH_TO_TRIAD.get(quality, quality)
    return theory.spell(root, quality, use_flats)


def _fold_tempo(bpm: float) -> float:
    """Fold octave errors of the beat tracker into the common 60-190 BPM range."""
    if bpm <= 0:
        return 0.0
    while bpm < 60:
        bpm *= 2
    while bpm > 190:
        bpm /= 2
    return bpm


def _merge_short_events(events: list[ChordEvent], min_seconds: float) -> list[ChordEvent]:
    """Absorb segments shorter than `min_seconds` into their neighbours.
    Works on copies -- the input list's own ChordEvents are never edited in
    place (the `events[i + 1].start = ...` path below would otherwise reach
    back into the caller's objects)."""
    events = [ChordEvent(e.start, e.end, e.label) for e in events]
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
    """Analyse an audio file and return its ChordTrack.

    snap_chords_to_key is accepted for backward compatibility with existing
    Settings files and the Settings panel's own checkbox, but no longer
    changes chord identity: it was the old template-matching pipeline's
    per-candidate similarity prior, a mechanism that no longer exists now
    that a trained model decides chords directly. Removing the Settings
    control itself is a separate, deliberately out-of-scope cleanup (see
    CLAUDE_HISTORY, 2026-09-13) -- left in place, inert, rather than
    reworking the Settings persistence/dirty-diff system in the same change
    that swapped the detection backend.
    """
    import librosa  # imported lazily: slow to import, only needed here
    from crema.analyze import analyze  # imported lazily: very slow to import (loads TensorFlow)

    y = decode_audio(path, SR)
    if y.size < SR:  # < 1 s of audio
        return ChordTrack()

    # Key and BPM are estimated independently of chord identity, purely for the
    # on-screen Key/BPM badge and the countdown's beat-synced tempo -- unrelated
    # to (and never implicated in) either past chord-accuracy incident.
    y_harm = librosa.effects.harmonic(y=y, margin=3.0)
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=SR, hop_length=HOP)
    tonic, mode, _key_conf = theory.estimate_key(chroma.mean(axis=1))
    use_flats = prefer_flats and theory.key_uses_flats(tonic, mode)

    try:
        tempo, _beats = librosa.beat.beat_track(y=y, sr=SR, hop_length=HOP, units="frames")
        tempo = _fold_tempo(float(np.atleast_1d(tempo)[0]))
    except Exception:
        tempo = 0.0

    jam = analyze(filename=str(path))
    chord_annotations = jam.annotations["chord", 0]
    duration = y.size / SR
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]

    events: list[ChordEvent] = []
    for obs in chord_annotations.data:
        start, end = float(obs.time), float(obs.time + obs.duration)
        if end <= start:
            continue
        start_frame = int(start * SR / HOP)
        end_frame = max(start_frame + 1, int(end * SR / HOP))
        segment_rms = rms[start_frame:end_frame]
        if segment_rms.size and segment_rms.mean() < _ABSOLUTE_SILENCE_RMS:
            label = "N"
        else:
            label = _simplify_chord_label(obs.value, include_seventh_chords, use_flats)
        if events and events[-1].label == label:
            events[-1].end = end
        else:
            events.append(ChordEvent(start, end, label))

    events = _merge_short_events(events, min_chord_seconds)
    if events:
        events[-1].end = max(events[-1].end, duration)

    return ChordTrack(events=events, key=theory.key_name(tonic, mode, prefer_flats), bpm=round(tempo, 1))
