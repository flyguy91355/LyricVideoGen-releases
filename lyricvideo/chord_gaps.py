"""Fills the chord bar's N.C. where music is playing (owner, 2026-10-31: "shouldnt have NC when there is music playing").

crema reads the music stem (no_vocals.wav). On heavily distorted guitar (Wrong Heart to Break) it returned N for about 60 s
of loud playing, while the same model on the full mix found the chords for most of it. And crema has no power-chord
quality: a root+fifth riff with no third is N to it. So an N stretch over audible music is filled in two steps:
  1. the full mix's own crema chords for that stretch;
  2. what is still N: a power chord read from the music stem's harmonic chroma -- the root the loudest pitch class and its
     fifth clearly the second -- named the way the song itself uses that root (E5 in a song full of Em -> Em). Bar by
     bar; a bar too muddy to read (distorted riffs smear every note) takes the whole stretch's reading. Else N.
Silence (an ending's tail, a real break) is never filled. Used for the channel's own AI songs (ai_generated.py) only, until
it is checked on songs the owner already judged."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import numpy as np

from .models import ChordEvent, ChordTrack

log = logging.getLogger("playalongvideoproduction")

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MIN_GAP_SECONDS = 1.0
MUSIC_FLOOR_DB = -30.0        # a stretch this far below the song's average level is silence, not music
_FIFTH_STRENGTH = 0.5         # power chord: the fifth at least half as loud as the root...
_BAR_MARGIN = 0.05            # ...and, for a single bar, clearly ahead of the third-loudest pitch class


def _root_of(label: str) -> int | None:
    name = label[:2] if label[1:2] == "#" else label[:1]
    return NOTE_NAMES.index(name) if name in NOTE_NAMES else None


def _clip(events: list[ChordEvent], start: float, end: float) -> list[ChordEvent]:
    out = []
    for e in events:
        a, b = max(e.start, start), min(e.end, end)
        if b > a:
            out.append(ChordEvent(a, b, e.label))
    return out


def _merge(events: list[ChordEvent], min_seconds: float) -> list[ChordEvent]:
    from .detect_chords import _merge_short_events
    out: list[ChordEvent] = []
    for e in _merge_short_events(sorted(events, key=lambda e: e.start), min_seconds):
        if out and out[-1].label == e.label:
            out[-1] = ChordEvent(out[-1].start, e.end, e.label)
        else:
            out.append(e)
    return out


def name_for_root(root: int, events: list[ChordEvent], minor_hint: bool) -> str:
    """The triad the song itself plays on `root` most (by time); unused root -> the chroma's own third decides."""
    major, minor = NOTE_NAMES[root], NOTE_NAMES[root] + "m"
    time = {major: 0.0, minor: 0.0}
    for e in events:
        if e.label in time:
            time[e.label] += e.end - e.start
    if time[major] or time[minor]:
        return minor if time[minor] > time[major] else major
    return minor if minor_hint else major


def power_chord_root(chroma: np.ndarray, margin: float = 0.0) -> tuple[int, bool] | None:
    """(root, minor third louder than major third) when the loudest pitch class's fifth is the second loudest (by
    `margin` over the third loudest); None otherwise."""
    peak = float(chroma.max())
    if peak <= 0:
        return None
    c = chroma / peak
    order = sorted(range(12), key=lambda i: -c[i])
    root, fifth = order[0], (order[0] + 7) % 12
    if order[1] != fifth or c[fifth] < _FIFTH_STRENGTH or c[fifth] - c[order[2]] < margin:
        return None
    return root, bool(c[(root + 3) % 12] > c[(root + 4) % 12])


def _stem_level_db(path: Path) -> Callable[[float, float], float]:
    import soundfile as sf
    audio, sr = sf.read(str(path), always_2d=True)
    mono = audio.mean(axis=1)
    average = float(np.sqrt(np.mean(mono ** 2))) or 1e-9

    def level(start: float, end: float) -> float:
        seg = mono[int(start * sr):int(end * sr)]
        return -120.0 if seg.size == 0 else 20 * np.log10(float(np.sqrt(np.mean(seg ** 2))) / average + 1e-9)
    return level


def _stem_bar_chroma(path: Path) -> Callable[[float, float], np.ndarray]:
    import librosa
    y, sr = librosa.load(str(path), sr=22050, mono=True)
    harmonic = librosa.effects.harmonic(y)
    hop = 512
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr, hop_length=hop)

    def at(start: float, end: float) -> np.ndarray:
        a, b = int(start * sr / hop), max(int(end * sr / hop), int(start * sr / hop) + 1)
        return chroma[:, a:b].mean(axis=1) if b <= chroma.shape[1] else np.zeros(12)
    return at


def fill_no_chord_gaps(
    track: ChordTrack,
    *,
    level_db: Callable[[float, float], float],
    full_mix_events: Callable[[], list[ChordEvent]],
    bar_chroma: Callable[[float, float], np.ndarray],
    min_chord_seconds: float = 0.0,
) -> tuple[ChordTrack, float]:
    """(the track with N filled where music plays, seconds filled). The full mix is analysed only when needed."""
    gaps = [e for e in track.events if e.label == "N" and e.end - e.start >= MIN_GAP_SECONDS
            and level_db(e.start, e.end) > MUSIC_FLOOR_DB]
    if not gaps:
        return track, 0.0
    mix = [e for e in full_mix_events() if e.label != "N"]
    kept = [e for e in track.events if e not in gaps]
    bar = 4 * 60.0 / track.bpm if track.bpm and track.bpm > 0 else 2.0
    filled = 0.0
    for gap in gaps:
        pieces = _clip(mix, gap.start, gap.end)
        cursor, holes = gap.start, []
        for p in pieces:
            if p.start > cursor:
                holes.append((cursor, p.start))
            cursor = max(cursor, p.end)
        if cursor < gap.end:
            holes.append((cursor, gap.end))
        for a, b in holes:
            whole = power_chord_root(bar_chroma(a, b))
            t = a
            while t < b - 1e-6:
                u = min(b, t + bar)
                found = None
                if level_db(t, u) > MUSIC_FLOOR_DB:
                    found = power_chord_root(bar_chroma(t, u), _BAR_MARGIN) or whole
                if found is None:
                    pieces.append(ChordEvent(t, u, "N"))
                else:
                    pieces.append(ChordEvent(t, u, name_for_root(found[0], track.events + mix, found[1])))
                t = u
        filled += sum(p.end - p.start for p in pieces if p.label != "N")
        kept.extend(pieces)
    events = _merge(kept, min_chord_seconds)
    return ChordTrack(events=events, key=track.key, bpm=track.bpm), filled


def fill_gaps_from_audio(track: ChordTrack, music_stem: Path, full_mix: Path, **detect_kwargs) -> ChordTrack:
    """fill_no_chord_gaps on real files: crema on the full mix (with the music stem's own detect_chords settings), level
    and chroma from the music stem."""
    from .detect_chords import MIN_CHORD_SECONDS, detect_chords
    result, filled = fill_no_chord_gaps(
        track, level_db=_stem_level_db(music_stem), bar_chroma=_stem_bar_chroma(music_stem),
        full_mix_events=lambda: detect_chords(full_mix, **detect_kwargs).events,
        min_chord_seconds=detect_kwargs.get("min_chord_seconds", MIN_CHORD_SECONDS),
    )
    if filled:
        log.info("Chords: filled %.0f s of N.C. where music was playing (full mix, then power chords).", filled)
    return result
