"""Settles a song's key (owner, 2026-09-26: "the display of the key and description MUST be the original key to the song").
Two independent answers must AGREE before a key reaches a video: the chord-based estimate (key_estimate.py) and a second
opinion choosing among the keys those chords allow (key_opinion.py). Agreement was right for 58 of 60 checked songs
where they agreed; anything else -- no second opinion, no chords, a disagreement -- is held for the owner's own answer
(key_owner.json, which always wins and survives a Redo). The decision, and what each side said, is saved beside the
song as key_decision.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .key_estimate import (
    KeyEstimate, candidate_keys, chord_summary, estimate_key_from_chords, parse_key, respell_chord_track,
)
from .key_opinion import ask_published_key
from .models import ChordTrack, Song
from .chord_theory import key_name

KEY_DECISION_FILE = "key_decision.json"
KEY_OWNER_FILE = "key_owner.json"
KEY_HOLD_PREFIX = "Key check:"       # a HeldBeforeVideo concern starting with this is a key hold (the GUI words it accordingly)


@dataclass
class KeyDecision:
    status: str                     # "confirmed" | "review"
    key: str = ""                   # the settled key; "" while in review
    source: str = ""                # "owner" | "agreed" ("" while in review)
    chord_key: str = ""             # what the chords say
    published_key: str = ""         # what the second opinion chose ("" = none)
    candidates: list[str] = field(default_factory=list)
    margin: float = 0.0
    at: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    def concern(self) -> str:
        if self.confirmed:
            return ""
        if not self.chord_key:
            return f"{KEY_HOLD_PREFIX} no chords were detected, so the song's key is unknown. Set Key to give it, then make the video."
        opinion = f"the second opinion says {self.published_key}" if self.published_key else "there was no second opinion"
        return (
            f"{KEY_HOLD_PREFIX} the song's key needs your confirmation. The chords say {self.chord_key}; {opinion}. "
            "Set Key to choose it, then make the video."
        )


def decide_key(estimate: KeyEstimate | None, published: str | None, owner: str | None, candidates: list[str]) -> KeyDecision:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    chord_key = estimate.name if estimate else ""
    base = dict(chord_key=chord_key, published_key=published or "", candidates=list(candidates),
                margin=estimate.margin if estimate else 0.0, at=stamp)
    owner_parsed = parse_key(owner)
    if owner_parsed is not None:
        return KeyDecision(status="confirmed", key=key_name(owner_parsed[0], owner_parsed[1], True), source="owner", **base)
    if estimate is not None and published and parse_key(published) == (estimate.tonic, estimate.mode):
        return KeyDecision(status="confirmed", key=estimate.name, source="agreed", **base)
    return KeyDecision(status="review", **base)


def load_owner_key(work_dir: Path) -> str | None:
    try:
        key = json.loads((Path(work_dir) / KEY_OWNER_FILE).read_text(encoding="utf-8")).get("key")
    except (OSError, ValueError):
        return None
    parsed = parse_key(key)
    return key_name(parsed[0], parsed[1], True) if parsed else None


def save_owner_key(work_dir: Path, key: str) -> str:
    """Stores the owner's answer; returns the key in the app's own spelling. Raises ValueError for something that is not a key."""
    parsed = parse_key(key)
    if parsed is None:
        raise ValueError(f"not a key: {key!r} (write it like 'D major' or 'F# minor')")
    canonical = key_name(parsed[0], parsed[1], True)
    (Path(work_dir) / KEY_OWNER_FILE).write_text(
        json.dumps({"key": canonical, "at": datetime.now().astimezone().isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )
    return canonical


def save_decision(work_dir: Path, decision: KeyDecision) -> None:
    (Path(work_dir) / KEY_DECISION_FILE).write_text(json.dumps(asdict(decision), indent=2), encoding="utf-8")


def load_decision(work_dir: Path) -> KeyDecision | None:
    try:
        data = json.loads((Path(work_dir) / KEY_DECISION_FILE).read_text(encoding="utf-8"))
        return KeyDecision(**data)
    except (OSError, ValueError, TypeError):
        return None


def settle_song_key(
    work_dir: Path, chord_track: ChordTrack, title: str, artist: str, anthropic_client=None, *, save: bool = True,
) -> tuple[KeyDecision, ChordTrack]:
    """(the decision, the chord track to use). A confirmed key respells the chords to its convention; a track still in
    review comes back respelled to the chords' own best guess -- provisional, never rendered (the caller holds the song)."""
    work_dir = Path(work_dir)
    owner = load_owner_key(work_dir)
    estimate = estimate_key_from_chords(chord_track)
    candidates = candidate_keys(chord_track)
    published = None
    if owner is None and estimate is not None and anthropic_client is not None:
        published = ask_published_key(anthropic_client, title, artist, candidates, chord_summary(chord_track))
    decision = decide_key(estimate, published, owner, candidates)
    if save:
        save_decision(work_dir, decision)
    if decision.confirmed:
        return decision, respell_chord_track(chord_track, decision.key)
    if estimate is not None:
        return decision, respell_chord_track(chord_track, estimate.name)
    return decision, chord_track


def apply_saved_owner_key(work_dir: Path, song: Song) -> bool:
    """For a song resumed from its saved file (Render Anyway after the owner chose a key): puts the owner's key on the
    saved chords. True when it changed something."""
    owner = load_owner_key(work_dir)
    if owner is None or song.chord_track.key == owner:
        return False
    song.chord_track = respell_chord_track(song.chord_track, owner)
    estimate = estimate_key_from_chords(song.chord_track)
    save_decision(work_dir, decide_key(estimate, None, owner, candidate_keys(song.chord_track)))
    return True


class KeyNotConfirmed(RuntimeError):
    """A video is not uploaded while its song's key is unchecked or waiting for the owner (owner, 2026-09-26)."""


def _key_dir(work_dir: Path) -> Path:
    work_dir = Path(work_dir)
    return work_dir.parent if work_dir.name == "easychords" else work_dir      # an EASY CHORD folder follows its song's key


def key_state(work_dir: Path) -> str:
    """"confirmed" | "review" (waiting for the owner) | "unchecked" (no key decision on file -- a song made before the
    key check existed, until settle_keys.py has been run on it)."""
    decision = load_decision(_key_dir(work_dir))
    if decision is None:
        return "unchecked"
    return "confirmed" if decision.confirmed else "review"


def confirmed_key_for_upload(work_dir: Path) -> str:
    """The song's settled key, for its YouTube description; raises KeyNotConfirmed when there is none."""
    decision = load_decision(_key_dir(work_dir))
    if decision is None or not decision.confirmed:
        state = key_state(work_dir)
        raise KeyNotConfirmed(
            f"{Path(work_dir).name}: the song's key is {'waiting for you to confirm it' if state == 'review' else 'not checked yet'}, "
            "so it was not uploaded."
        )
    return decision.key


def key_needs_attention(work_dir: Path) -> bool:
    """True for a song that is NOT on YouTube yet and whose key is unchecked or waiting for the owner -- the songs Flagged
    for review lists for Set Key and that no upload offers. A song already uploaded is left alone."""
    from .youtube_state import STATE_FILENAME
    work_dir = Path(work_dir)
    return key_state(work_dir) != "confirmed" and not (work_dir / STATE_FILENAME).exists()
