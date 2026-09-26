"""The song's key, worked out from its DETECTED CHORDS (owner, 2026-09-26: the old estimate -- the average pitch of the
whole recording, `chord_theory.estimate_key` -- was right for only 55 of the 80 songs whose true key was checked against
Musicnotes/Tunebat/Hooktheory; this chord-based estimate gets 73). Pure functions over a ChordTrack; nothing here reads
audio or talks to a network. It is still only an estimate -- see key_decision.py, which never lets it stand alone."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .chord_theory import NOTES_FLAT, NOTES_SHARP, key_name, key_uses_flats, parse_chord_label, spell
from .models import ChordEvent, ChordTrack

TONIC_WEIGHT = 0.75     # how much time on the key's own chord counts (chosen on the 80 verified songs; 0.5-1.0 all score 72-73)
EDGE_WEIGHT = 0.25      # how much a song opening / closing on the key's own chord counts (its first chord and last two)

# (semitones above the tonic, triad quality): the chords a key contains. A minor key also takes the major V and the
# major chords built on its flat 3rd / 6th / 7th, as rock and pop use them.
_MAJOR_KEY = ((0, "maj"), (2, "min"), (4, "min"), (5, "maj"), (7, "maj"), (9, "min"), (11, "dim"))
_MINOR_KEY = ((0, "min"), (2, "dim"), (3, "maj"), (5, "min"), (7, "min"), (7, "maj"), (8, "maj"), (10, "maj"), (11, "dim"))


@dataclass(frozen=True)
class KeyEstimate:
    tonic: int
    mode: str           # "major" | "minor"
    margin: float       # how far the best key beat the runner-up -- small means "could easily be another key"

    @property
    def name(self) -> str:
        return key_name(self.tonic, self.mode, True)


def _triad(quality: str) -> str:
    return "min" if quality in ("min", "min7") else "maj"


def _scored_keys(track: ChordTrack) -> list[tuple[float, int, str]]:
    """Every (score, tonic, mode), best first -- empty when the track has no real chord."""
    seconds: dict[tuple[int, str], float] = {}
    order: list[tuple[int, str]] = []
    for e in track.events:
        parsed = parse_chord_label(e.label)
        if parsed is None:
            continue
        chord = (parsed[0], _triad(parsed[1]))
        seconds[chord] = seconds.get(chord, 0.0) + (e.end - e.start)
        order.append(chord)
    total = sum(seconds.values())
    if not seconds or total <= 0:
        return []
    edge = order[:1] + order[-2:]
    scored = []
    for tonic in range(12):
        for mode, degrees in (("major", _MAJOR_KEY), ("minor", _MINOR_KEY)):
            inside = {((tonic + step) % 12, quality) for step, quality in degrees}
            home = (tonic, "maj" if mode == "major" else "min")
            score = (
                sum(s for c, s in seconds.items() if c in inside) / total
                + TONIC_WEIGHT * seconds.get(home, 0.0) / total
                + EDGE_WEIGHT * sum(1 for c in edge if c == home) / len(edge)
            )
            scored.append((score, tonic, mode))
    scored.sort(reverse=True)
    return scored


def estimate_key_from_chords(track: ChordTrack) -> KeyEstimate | None:
    """None when the track has no real chord ("N" and unparseable labels do not count)."""
    scored = _scored_keys(track)
    if not scored:
        return None
    return KeyEstimate(tonic=scored[0][1], mode=scored[0][2], margin=scored[0][0] - scored[1][0])


def candidate_keys(track: ChordTrack, count: int = 4) -> list[str]:
    """The `count` best-fitting keys, best first ("D major", "B minor", ...) -- what a second opinion chooses between."""
    return [key_name(tonic, mode, True) for _score, tonic, mode in _scored_keys(track)[:count]]


def chord_summary(track: ChordTrack, count: int = 8) -> str:
    """"D 48%, G 26%, A 11%, ..." -- the chords the song spends the most time on, for showing to a second opinion."""
    seconds: dict[str, float] = {}
    for e in track.events:
        if parse_chord_label(e.label) is not None:
            seconds[e.label] = seconds.get(e.label, 0.0) + (e.end - e.start)
    total = sum(seconds.values()) or 1.0
    top = sorted(seconds.items(), key=lambda kv: -kv[1])[:count]
    return ", ".join(f"{label} {round(100 * secs / total)}%" for label, secs in top)


_NOTE_PC = {name: pc for names in (NOTES_SHARP, NOTES_FLAT) for pc, name in enumerate(names)}
_KEY_TEXT = re.compile(r"^([A-G][#b]?)\s*(major|maj|minor|min|m)?$")


def parse_key(text: str | None) -> tuple[int, str] | None:
    """"Bb major" / "F#m" / "a minor" / "Eb" -> (pitch class, "major"|"minor"); a bare note means major. None for
    anything else (blank, a mode like "lydian", a made-up note) -- a key is never guessed."""
    if not text or not text.strip():
        return None
    cleaned = " ".join(text.split())
    cleaned = cleaned[0].upper() + cleaned[1:]
    m = _KEY_TEXT.match(cleaned.replace("♯", "#").replace("♭", "b"))
    if m is None:
        m = _KEY_TEXT.match(cleaned[0] + cleaned[1:].lower())    # "C# Minor", "D MAJOR"
    if m is None:
        return None
    mode_word = (m.group(2) or "major").lower()
    return _NOTE_PC[m.group(1)], "minor" if mode_word in ("minor", "min", "m") else "major"


def respell_chord_track(track: ChordTrack, key: str) -> ChordTrack:
    """A copy of `track` whose chord names follow `key`'s flat/sharp convention (Gbm becomes F#m when the key turns out
    to be F# minor) and whose .key is that key's canonical name. Same chords, same times; "N" is left alone."""
    parsed = parse_key(key)
    if parsed is None:
        raise ValueError(f"not a key: {key!r}")
    tonic, mode = parsed
    use_flats = key_uses_flats(tonic, mode)
    events = []
    for e in track.events:
        chord = parse_chord_label(e.label)
        label = spell(chord[0], chord[1], use_flats) if chord else e.label
        events.append(ChordEvent(start=e.start, end=e.end, label=label))
    return ChordTrack(events=events, key=key_name(tonic, mode, True), bpm=track.bpm)
