"""Is the lyric timing right? Compares where the aligner put each line with where Whisper heard it (anchors.py) and
decides which alignment to trust -- the whole-song pass (precise when it has not drifted) or the anchored,
window-bounded one -- or that neither can be trusted and the song must be set aside for the owner's review.

Real failure this exists for: 'Girls Just Want to Have Fun' had lines 20-50 s early and nothing noticed."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .anchors import LineAnchor

AGREE_WITHIN_SECONDS = 2.0     # a line "agrees" when it starts this close to where it was heard...
EXPECTED_ANCHOR_BIAS = 2.5     # ...after allowing up to this much CONSISTENT offset: Whisper's first-word times run
                               # ~2 s before the true line start (lrclib agreed with the alignment, not the anchors)
KEEP_WHOLE_SONG_AT = 0.85      # the original whole-song alignment is kept when at least this much agrees
TRUST_AT_LEAST = 0.70          # below this even the best alignment is not trusted
MIN_ANCHORED_LINES = 4         # fewer heard lines than this (or 25% of the song) cannot verify timing
SOURCE_OFFSET_ALLOWED = 3.0    # the lyrics source's own line times may run this much CONSISTENTLY early/late (another edition)
SOURCE_LINE_WITHIN = 3.0       # second opinion: a line "agrees" with its source time when this close (after the offset)...
SOURCE_LINE_FAR = 5.0          # ...and no line may be further than this
SOURCE_SHARE_NEEDED = 0.85     # ...and at least this share must agree
WHISPER_MIN_SUPPORT = 0.50     # the source only backs up an alignment Whisper half-confirms, never one it contradicts


@dataclass(frozen=True)
class SyncReport:
    agreement: float           # share of anchored lines whose aligned start is within AGREE_WITHIN_SECONDS
    anchored: int
    total_lines: int
    verifiable: bool


@dataclass(frozen=True)
class SyncDecision:
    method: str                # "whole-song" | "anchored"
    report: SyncReport         # the chosen alignment's agreement
    ok: bool
    concern: str               # "" when ok, otherwise why the song is set aside


def sync_agreement(line_starts: list[float], anchors: dict[int, LineAnchor]) -> SyncReport:
    total = len(line_starts)
    scored = [i for i in anchors if i < total]
    verifiable = len(scored) >= max(MIN_ANCHORED_LINES, 0.25 * total)
    if not scored:
        return SyncReport(0.0, 0, total, False)
    differences = [line_starts[i] - anchors[i].start for i in scored]
    offset = max(-EXPECTED_ANCHOR_BIAS, min(EXPECTED_ANCHOR_BIAS, statistics.median(differences)))
    agree = sum(abs(d - offset) <= AGREE_WITHIN_SECONDS for d in differences)
    return SyncReport(agree / len(scored), len(scored), total, verifiable)


def source_timing_agrees(line_starts: list[float], source_times: list[float] | None) -> bool:
    """Do the lines start where the lyrics source (lrclib/NetEase, human-made) says, apart from one consistent offset?
    Real ('Back in the Saddle'): 31 of 33 lines within 3 s and none 5 s off, while Whisper on that loud recording
    confirmed only ~70%. Never enough alone -- decide_alignment only asks it about a song Whisper half-confirms."""
    if not source_times or len(source_times) != len(line_starts):
        return False
    differences = [s - t for s, t in zip(line_starts, source_times)]
    offset = max(-SOURCE_OFFSET_ALLOWED, min(SOURCE_OFFSET_ALLOWED, statistics.median(differences)))
    off_by = [abs(d - offset) for d in differences]
    close = sum(d <= SOURCE_LINE_WITHIN for d in off_by)
    return max(off_by) <= SOURCE_LINE_FAR and close / len(off_by) >= SOURCE_SHARE_NEEDED


def decide_alignment(
    whole_song_starts: list[float], anchored_starts: list[float], anchors: dict[int, LineAnchor],
    source_times: list[float] | None = None,
) -> SyncDecision:
    whole = sync_agreement(whole_song_starts, anchors)
    if not whole.verifiable:
        return SyncDecision("whole-song", whole, True, "")           # nothing to check it against: unchanged behavior
    if whole.agreement >= KEEP_WHOLE_SONG_AT:
        return SyncDecision("whole-song", whole, True, "")
    anchored = sync_agreement(anchored_starts, anchors)
    if anchored.agreement >= TRUST_AT_LEAST:
        return SyncDecision("anchored", anchored, True, "")
    best_method, best = ("anchored", anchored) if anchored.agreement >= whole.agreement else ("whole-song", whole)
    best_starts = anchored_starts if best_method == "anchored" else whole_song_starts
    if best.agreement >= WHISPER_MIN_SUPPORT and source_timing_agrees(best_starts, source_times):
        return SyncDecision(best_method, best, True, "")
    return SyncDecision(
        best_method, best, False,
        f"SET ASIDE FOR REVIEW -- the lyric timing could not be fixed automatically: only {best.agreement:.0%} of the "
        f"{best.anchored} lines that could be checked start where the recording says (best of both alignment "
        "methods). The video is made but not uploaded; watch it and correct the lyrics or timing.",
    )
