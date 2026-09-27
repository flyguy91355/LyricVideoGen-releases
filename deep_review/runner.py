"""Orchestrates the deep review: diagnose every song set aside for review, and only spend research effort
where diagnosis.py says a lyrics fix could plausibly help. A song is only ever counted "passed" on the
strength of a REAL post-redo timing-gate recheck -- never on the research step's own self-reported
confidence, and never by assumption. Everything this program cannot fix (damaged audio, alignment-only
imprecision, anything undiagnosable) is reported, never touched.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, replace
from pathlib import Path

from lyricvideo.key_decision import KEY_HOLD_PREFIX
from lyricvideo.models import load_song, save_song
from lyricvideo.owner_lyrics import OWNER_LYRICS_FILE, save_owner_lyrics
from lyricvideo.owner_verified import verification
from lyricvideo.pipeline import (
    HeldBeforeVideo, backup_song_outputs, build_capo_variant, held_before_video, load_redo_inputs, run_pipeline, slugify,
    song_video_path,
)
from lyricvideo.timing_gate import check_saved_song, hold_if_timing_fails, is_gate_concern
from lyricvideo.transcribe import load_transcript_segments

from .diagnosis import Category, diagnose_song, is_lyric_text_concern
from .needs_human import DEFAULT_DIR as NEEDS_HUMAN_DIR
from .needs_human import clear_marker, write_marker
from .research import research_lyrics

_MIN_CONFIDENCE_TO_APPLY = "high"
# Owner, 2026-09-22: "the goal of the program is to get every song above 90% and try for 100%... if all
# avenues fail then straight to human attention after all attemps have been made." Matches the app's own
# existing "how many tries before giving up" convention elsewhere (imagery.get_or_generate_image's
# _MAX_GENERATION_ATTEMPTS is also 3).
MAX_ATTEMPTS = 3
# Owner, 2026-09-22: "i cant do any more than 5 cents per song ... if i have to do more, my opinion, its not
# worth doing." Checked BEFORE each attempt (including implicitly before the first, where cost_usd starts at
# 0.0) -- a single attempt's own cost is only known after it happens, so this can't prevent one expensive
# call from landing over the cap, but it always stops any COMPOUNDING beyond that: once the running total
# for a song is at or over this, no further attempt is made.
MAX_COST_PER_SONG_USD = 0.05
# Owner, 2026-09-22: "song like 89.7. not sure if i need to spend more money on those as well" -- real
# evidence (1979: 3 attempts, never moved, its own research said the lyrics probably already match the
# studio album) says a song already this close has very few out-of-sync lines and is more likely a
# borderline alignment case than genuinely wrong lyrics. Checked against the INITIAL diagnosis only, before
# any money is spent -- not re-checked after an attempt, since by then real money already bought real
# evidence either way.
SKIP_RESEARCH_ABOVE_SHARE = 0.85
# An EASY CHORD (capo) variant lives at "<song>/easychords" and is rebuilt from its original by
# pipeline.build_capo_variant() -- never redone on its own (a redo would re-detect un-capo'd chords and render
# the hard ones over the EASY video). It is reported, never touched.
CAPO_VARIANT_DIR = "easychords"
CAPO_VARIANT_CATEGORY = "easy_chords_variant"


@dataclass
class SongResult:
    slug: str
    category: str
    action: str = ""              # "cleared" | "skipped" | "skipped: waiting for Set Key" | "researched_and_redone" |
                                  # "lyrics fixed; waiting for Set Key" | "left for manual review" | "failed"
    passed: bool | None = None    # a REAL post-redo recheck; None only when nothing was ever tried (dry run, or n/a)
    before_share: float | None = None
    after_share: float | None = None
    attempts: int = 0             # how many research+redo attempts were actually tried (0 for categories that never try)
    cost_usd: float = 0.0         # real, measured spend across every attempt (0.0 for categories that never call the API)
    detail: str = ""


def is_capo_variant(slug: str) -> bool:
    return Path(slug).name == CAPO_VARIANT_DIR


def _needed(settings) -> float | None:
    """The owner's timing bar as a share (Settings.timing_pass_percent / 100, kept in timing_gate's own 0.5-1.0
    range), or None to use the bar in force -- every judgement this program makes uses the same bar the redo's
    own render does, never the default 90% behind the owner's back."""
    if settings is None:
        return None
    try:
        return min(max(float(settings.timing_pass_percent) / 100, 0.5), 1.0)
    except (AttributeError, TypeError, ValueError):
        return None


def _recheck(work_dir: Path, needed: float | None = None) -> tuple[bool, float | None]:
    """(passed, share) after a real redo. Prefers a fresh timing-gate recheck; falls back to "no concern
    recorded" when the recheck itself can't run (e.g. no transcript survives in a test double) -- the
    pipeline's own saved concern is still real evidence, just less precise than a share. A lyric-text concern
    the redo left behind is never a pass, whatever the timing says."""
    concern = load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern
    report = check_saved_song(work_dir, needed)
    if report is not None:
        return report.passes and not is_lyric_text_concern(concern), report.share
    return not concern, None


def _clear_concern(work_dir: Path) -> None:
    """Blanks a stale TIMING concern the current gate passes. Refuses a lyric-text concern outright (a timing pass
    says nothing about the words; the app's own timing gate never touches another check's concern either)."""
    timed_path = work_dir / "lyrics_timed.json"
    song = load_song(timed_path)
    if not song.lyrics_accuracy_concern:
        return
    if is_lyric_text_concern(song.lyrics_accuracy_concern):
        raise ValueError(f"refusing to clear a lyric-text concern: {song.lyrics_accuracy_concern[:120]}")
    save_song(replace(song, lyrics_accuracy_concern=""), timed_path)


def _restore_owner_lyrics(work_dir: Path, original: bytes | None) -> None:
    """Puts lyrics_owner.txt back exactly as it was before this review, byte for byte (or removes it if there was
    none): a research guess only stays there when a real recheck passed. That file is the owner's own final word --
    every later Redo trusts it with no online lookup or AI check -- so a rejected guess must never be left in it."""
    path = work_dir / OWNER_LYRICS_FILE
    try:
        if original is None:
            path.unlink(missing_ok=True)
        elif not path.exists() or path.read_bytes() != original:
            path.write_bytes(original)
    except OSError as e:
        print(f"WARNING: could not restore {path}: {type(e).__name__}: {e}", file=sys.stderr)


def _rebuild_capo_variant(work_dir: Path, settings) -> None:
    """After a fixed song's new video, its existing EASY CHORD variant is re-rendered from it so the variant shows
    the corrected lyrics too (run_pipeline already did that when generate_easy_chord_versions is on). A problem
    here never un-fixes the song itself."""
    if not (work_dir / CAPO_VARIANT_DIR).is_dir():
        return
    if settings is not None and getattr(settings, "generate_easy_chord_versions", False):
        return
    try:
        build_capo_variant(work_dir, settings=settings)       # the owner's render settings, like the GUI's rebuild
    except Exception as e:
        print(f"WARNING: could not rebuild the EASY CHORD version of {work_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)


def _artist_for(work_dir: Path) -> str:
    """"" when unknown, same convention as build_play_along_title() -- Song itself carries no artist field;
    identify's own resolution lives in song_info.json."""
    try:
        return str(json.loads((work_dir / "song_info.json").read_text(encoding="utf-8")).get("artist") or "")
    except (OSError, ValueError, KeyError):
        return ""


def _review_one(
    work_root: Path, slug: str, anthropic_client, settings, dry_run: bool, needed: float | None = None,
) -> SongResult:
    work_dir = work_root / slug
    if is_capo_variant(slug):
        return SongResult(
            slug=slug, category=CAPO_VARIANT_CATEGORY, action="skipped",
            detail="EASY CHORD version: fix its original song, then rebuild this from it -- never redone on its own",
        )
    diagnosis = diagnose_song(work_dir, needed)
    result = SongResult(slug=slug, category=diagnosis.category.value, before_share=diagnosis.sync_share,
                         detail=diagnosis.detail)

    if diagnosis.category == Category.ALREADY_PASSES:
        if not dry_run:
            _clear_concern(work_dir)
        result.action, result.passed, result.after_share = "cleared", True, diagnosis.sync_share
        return result

    if diagnosis.category == Category.WAITING_FOR_KEY:
        # Nothing for a lyrics program to do, and not a pass either: the video waits for the owner's key.
        result.action, result.passed = "skipped: waiting for Set Key", None
        return result

    if diagnosis.category in (Category.DAMAGED_AUDIO, Category.ALIGNMENT_ONLY, Category.UNKNOWN):
        result.action, result.passed = "skipped", False if diagnosis.category != Category.UNKNOWN else None
        return result

    if diagnosis.sync_share is not None and diagnosis.sync_share >= SKIP_RESEARCH_ABOVE_SHARE:
        result.action, result.passed = "left for manual review", False
        result.detail = (
            f"already at {diagnosis.sync_share:.1%}, close enough that this is more likely alignment than "
            f"wrong lyrics -- no research spent (cutoff: {SKIP_RESEARCH_ABOVE_SHARE:.0%})"
        )
        return result

    # LYRICS_WRONG: the only category this program can actually try to fix. Each attempt writes its research
    # into lyrics_owner.txt (the redo's owner-lyrics channel); unless a real recheck passes, that file is put back
    # exactly as it was -- the owner's own edit is never lost, and a rejected guess is never left as "owner lyrics".
    owner_path = work_dir / OWNER_LYRICS_FILE
    # Bytes, not text: restored exactly (line endings, any encoding), and an odd file can never stop the review.
    original_owner = owner_path.read_bytes() if owner_path.exists() else None
    try:
        _research_and_redo(work_dir, slug, diagnosis, result, anthropic_client, settings, dry_run, needed, original_owner)
    finally:
        if not dry_run and result.passed is not True:
            _restore_owner_lyrics(work_dir, original_owner)
    return result


def _research_and_redo(
    work_dir: Path, slug: str, diagnosis, result: SongResult, anthropic_client, settings, dry_run: bool,
    needed: float | None, original_owner: bytes | None,
) -> None:
    """Keeps trying different avenues, re-diagnosing after each one, until the song passes, the problem turns out
    not to be lyrics after all, or every attempt is used up (owner, 2026-09-22: try for 100%, only give up after
    every avenue has failed). Fills in `result`."""
    current = diagnosis
    previous_attempts: list[dict] = []
    first_backup: Path | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if result.cost_usd >= MAX_COST_PER_SONG_USD:
            result.action, result.passed = "left for manual review", False
            result.detail = (
                f"cost cap (${MAX_COST_PER_SONG_USD:.2f}/song) reached after {attempt - 1} attempt(s): "
                f"${result.cost_usd:.2f} spent"
            ) + _backup_note(first_backup)
            return
        result.attempts = attempt
        song = load_song(work_dir / "lyrics_timed.json")
        segments = load_transcript_segments(work_dir)
        try:
            attempt_result = research_lyrics(
                anthropic_client, song.title, _artist_for(work_dir), [line.text for line in song.lines],
                segments, mismatched_lines=current.mismatched_lines or None,
                previous_attempts=previous_attempts or None,
            )
        except Exception as e:
            result.detail = f"{type(e).__name__}: {e}"
            print(f"WARNING: research failed for {slug!r} (attempt {attempt}/{MAX_ATTEMPTS}): {result.detail}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            previous_attempts.append({"lines": [], "outcome": f"crashed: {result.detail}"})
            continue

        result.cost_usd += attempt_result.cost_usd   # the call cost real money whether or not it was usable
        research = attempt_result.research
        if research is None or research.confidence != _MIN_CONFIDENCE_TO_APPLY:
            result.detail = (research.notes if research else "") or "research was not confident enough to apply automatically"
            if research is not None:
                previous_attempts.append({"lines": research.lines, "outcome": f"low confidence: {result.detail}"})
            continue

        if dry_run:
            result.action, result.passed = "would research_and_redo (dry run)", None
            result.detail = "; ".join(research.lines[:3]) + (" ..." if len(research.lines) > 3 else "")
            return

        try:
            # Backed up BEFORE this attempt changes anything; the owner's own lyrics file goes in with the video
            # and timing, so the pre-review state is complete in one folder.
            backup_dir = backup_song_outputs(work_dir, slugify(song.title))
            if backup_dir is not None:
                first_backup = first_backup or backup_dir
                if original_owner is not None and not (backup_dir / OWNER_LYRICS_FILE).exists():
                    (backup_dir / OWNER_LYRICS_FILE).write_bytes(original_owner)
            save_owner_lyrics(work_dir, "\n".join(research.lines))
            audio_path, title = load_redo_inputs(work_dir)
            run_pipeline(audio_path, work_dir, title, start_stage="fetch_lyrics", settings=settings)
        except HeldBeforeVideo as e:
            result.detail = e.concern
            if e.concern.startswith(KEY_HOLD_PREFIX):
                # Stopped at the KEY check, which only runs after the lyrics and timing passed: the lyrics are fixed;
                # the video is made once the owner confirms the key -- not a lyrics failure to retry.
                passed, share = _recheck(work_dir, needed)
                result.after_share = share
                if passed:
                    result.action, result.passed = "lyrics fixed; waiting for Set Key", True
                    result.detail = "the lyrics and timing now pass; the video is made once you confirm the key (Set Key)"
                    return
        except Exception as e:
            result.detail = f"{type(e).__name__}: {e}"
            print(f"WARNING: redo failed for {slug!r} (attempt {attempt}/{MAX_ATTEMPTS}): {result.detail}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            previous_attempts.append({"lines": research.lines, "outcome": f"redo crashed: {result.detail}"})
            continue
        else:
            passed, share = _recheck(work_dir, needed)
            result.after_share = share
            if passed:
                result.action, result.passed, result.detail = "researched_and_redone", True, ""
                _rebuild_capo_variant(work_dir, settings)
                return

        previous_attempts.append({"lines": research.lines, "outcome": f"still not passing: {result.detail}"})
        current = diagnose_song(work_dir, needed)
        if current.sync_share is not None:
            result.after_share = current.sync_share
        if current.category != Category.LYRICS_WRONG:
            # No longer this program's problem: either some other measure now calls it fine, or what's left
            # (e.g. alignment-only) needs a different fix than more lyrics research can offer.
            result.action, result.passed = "left for manual review", False
            result.detail = f"after {attempt} attempt(s), now {current.category.value}: {current.detail}" + _backup_note(first_backup)
            return

    result.action, result.passed = "left for manual review", False
    result.detail = (result.detail or f"no confident fix found after {MAX_ATTEMPTS} attempts") + _backup_note(first_backup)


def _backup_note(backup_dir: Path | None) -> str:
    """Where the pre-review video/timing/owner lyrics are, for a song this run redid but could not fix."""
    return f" (before this review: {backup_dir.name})" if backup_dir is not None else ""


def _priority(work_root: Path, slug: str, needed: float | None = None) -> tuple:
    """Sort key: cleanly-clearable songs first (zero effort), then LYRICS_WRONG songs closest to the pass
    mark first (owner request, 2026-09-22: "the easy ones done first") -- a song already at 87.5% likely
    needs one small correction, one at 20% needs much more. A song with no computable share (the plain
    lyrics-accuracy-concern path, no gate score) sorts last within its group: no evidence it's easy.
    Everything this program can't act on (alignment-only/damaged-audio/unknown/waiting for its key) sorts
    last overall -- their own order doesn't matter, since they're only ever reported, never researched; an
    EASY CHORD variant goes after all of them."""
    if is_capo_variant(slug):
        return (3, 0.0)
    try:
        diagnosis = diagnose_song(work_root / slug, needed)
    except Exception:
        return (2, 1.0)                     # _review_one reports what went wrong; sorting must never end the run
    group = {Category.ALREADY_PASSES: 0, Category.LYRICS_WRONG: 1}.get(diagnosis.category, 2)
    closeness = -(diagnosis.sync_share if diagnosis.sync_share is not None else -1.0)
    return (group, closeness)


def list_songs_to_review(work_root: Path, needed: float | None = None, write_holds: bool = True) -> list[str]:
    """The default song list: every rendered (or held-before-video) top-level song set aside for a LYRICS or
    TIMING reason, uploaded or not -- the GUI's Flagged for Lyrics Review list minus what this program must not
    touch: an EASY CHORD variant ("<song>/easychords", rebuilt from its original, never redone) and a song flagged
    only for its key (Set Key). An owner-verified song is left alone. `needed` is the owner's bar.
    `write_holds=True` keeps each song's timing hold in step with that bar exactly as the GUI's lists do
    (hold_if_timing_fails); False (a dry run) judges the timing read-only and writes nothing."""
    work_root = Path(work_root)
    if not work_root.exists():
        return []
    slugs: list[str] = []
    for entry in sorted(work_root.iterdir(), key=lambda e: e.name):
        if not entry.is_dir() or not (entry / "lyrics_timed.json").exists():
            continue
        if song_video_path(entry) is None and not held_before_video(entry):
            continue
        if verification(entry):
            continue                        # the owner approved this version: overrides every check until a Redo
        try:
            concern = load_song(entry / "lyrics_timed.json").lyrics_accuracy_concern
        except Exception:
            continue
        if concern and not is_gate_concern(concern):
            slugs.append(entry.name)        # the lyric-text checks (or an older timing check): held whatever the gate says
            continue
        if write_holds:
            if hold_if_timing_fails(entry, needed):
                slugs.append(entry.name)
            continue
        report = check_saved_song(entry, needed)
        if report is None or report.share is None:
            if concern:
                slugs.append(entry.name)    # cannot be judged here: keeps whatever hold it had
        elif not report.passes:
            slugs.append(entry.name)
    return slugs


def _update_marker(needs_human_dir: Path, result: SongResult) -> None:
    """A song that reached a real pass (or needs nothing from a lyrics program -- only its key) loses any earlier
    marker; anything else gets one. An EASY CHORD variant is never reviewed, so it is left alone. A marker that
    cannot be written is a warning, never the end of the run."""
    if result.category == CAPO_VARIANT_CATEGORY:
        return
    try:
        if result.passed or result.category == Category.WAITING_FOR_KEY.value:
            clear_marker(needs_human_dir, result.slug)
        else:
            write_marker(needs_human_dir, result)
    except OSError as e:
        print(f"WARNING: could not update the needs-human marker for {result.slug!r}: {type(e).__name__}: {e}", file=sys.stderr)


def run_deep_review(
    work_root: Path, anthropic_client, songs: list[str] | None = None, limit: int | None = None,
    settings=None, dry_run: bool = False, needs_human_dir: Path = NEEDS_HUMAN_DIR,
) -> list[SongResult]:
    """Runs the whole review over `songs` (default: list_songs_to_review() -- the GUI's Flagged for Lyrics
    Review list, less EASY CHORD variants and songs flagged only for their key), sorted closest-to-passing first
    (see _priority) and capped at `limit` if given -- so a pilot run naturally picks the easiest songs. One song
    failing never stops the rest. Every timing judgement uses the owner's bar (`settings.timing_pass_percent`),
    the same one the redo's own render uses. `dry_run=True` diagnoses and researches but never writes a real
    file (not even a timing hold while listing), triggers a redo, or touches `needs_human_dir` -- safe to run to
    see what it WOULD do and roughly what it would cost first. Every song this run could not get to a real pass
    gets a marker file in `needs_human_dir` (needs_human.py) for the owner to review on its own, separate from
    everything that passed or was cleanly cleared; a song this run DID fix has any earlier marker removed."""
    work_root = Path(work_root)
    needed = _needed(settings)
    slugs = list(songs) if songs is not None else list_songs_to_review(work_root, needed, write_holds=not dry_run)
    slugs = sorted(slugs, key=lambda slug: _priority(work_root, slug, needed))
    if limit is not None:
        slugs = slugs[:limit]

    results = []
    for slug in slugs:
        try:
            result = _review_one(work_root, slug, anthropic_client, settings, dry_run, needed)
        except Exception as e:
            print(f"WARNING: could not review {slug!r} at all: {type(e).__name__}: {e}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            result = SongResult(slug=slug, category="unknown", action="failed", passed=False,
                                 detail=f"{type(e).__name__}: {e}")
        results.append(result)
        if not dry_run:
            _update_marker(needs_human_dir, result)
    return results
