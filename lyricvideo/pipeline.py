from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

import anthropic
import torchaudio
from dotenv import load_dotenv

from .align import align_words, align_words_anchored, prepare_alignment, vocal_loudness
from .anchors import HeardWord, combine_anchors, drop_words_in_silence, line_anchors
from .assemble import assemble_video
from .combine import combine_alignment
from .detect_chords import detect_chords
from .fetch_lyrics import fetch_lyric_lines_verified
from .identify import extract_metadata
from .imagery import get_or_generate_image, is_fallback_image, substitute_fallback_images, summarize_song_gist
from .layout import instrumental_image_captions
from .lyric_arbiter import arbitrate
from .lyric_audio_match import drop_unsung_leading_lines, drop_unsung_trailing_lines, score_lyrics_against_transcript
from .lyric_reconcile import SUGGESTION_FILENAME, reconcile_lyrics
from .owner_lyrics import owner_lyrics_lines
from .chord_theory import (  # noqa: F401 -- ordered_unique_chords re-exported: every existing `pipeline.ordered_unique_chords` caller keeps working unchanged
    capo_and_shape_key, is_easy_key, ordered_unique_chords, save_easy_chord_capo_marker, transpose_chord_track,
)
from .cleared_log import record_cleared, record_removed
from .redo_log import note_redo_finished, note_redo_started
from .models import ChordTrack, LyricLine, Song, Word, display_slug, load_song, save_song
from .separate import separate_vocals, stems_look_complete
from .precision import blend, choose_alignment, match_words
from .sync import decide_alignment, sync_agreement
from .settings import Settings
from .owner_verified import verification
from .timing_gate import (
    check_saved_song, heard_text_near_line, hold_if_timing_fails, is_gate_concern, percent_display, settle_alignment,
)
from .transcribe import load_transcript_segments, load_transcript_text, load_transcript_words, transcribe_vocals
from .youtube_state import STATE_FILENAME

STAGES = ["identify", "separate", "fetch_lyrics", "align", "detect_chords", "images", "render"]

log = logging.getLogger("playalongvideoproduction")


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled-song"


def song_end_time(song: Song) -> float:
    """Where the song's own detected content ends -- the later of the last
    chord event's end (detect_chords extends that to the analyzed stem's full
    duration) and the last timed lyric line's end. The images stage uses this
    as the horizon for listing every instrumental image the render will need,
    without decoding the audio again; a container whose decoded duration runs
    a hair past this (MP3 decoder padding) is covered at render time by the
    nearest-real-image fallback in assemble_video()."""
    ends = [event.end for event in song.chord_track.events]
    ends += [line.end_time for line in song.lines if line.end_time is not None]
    return max(ends, default=0.0)




def default_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        # Windows: bold Arial / Segoe UI ship with every install.
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
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


def _candidate_song_dirs(work_root: Path) -> list[tuple[str, Path]]:
    """Every directory that could hold its own distinct song: each of work_root's immediate
    subdirectories, plus that subdirectory's own `easychords/` nested folder if it has one -- an EASY
    CHORD (capo) variant (owner, 2026-09-23: "i dont need twice the folder") lives nested inside its
    original song's own folder rather than as a separate top-level sibling, but is still tracked/listed as
    its own distinct song everywhere else. Returns (slug, path) pairs sorted by slug; a slug already works
    unchanged everywhere one is turned back into a path via `PROJECT_ROOT / "work" / slug`, since pathlib
    splits a "/"-containing string into path segments on every platform. Deliberately NOT used by
    list_redoable_songs() -- a capo variant is rebuilt via build_capo_variant(), never "Redo"-d (that would
    pointlessly re-fetch lyrics/re-detect chords a derived video has no business re-deciding)."""
    pairs: list[tuple[str, Path]] = []
    for entry in work_root.iterdir():
        if not entry.is_dir():
            continue
        pairs.append((entry.name, entry))
        nested = entry / "easychords"
        if nested.is_dir():
            pairs.append((f"{entry.name}/easychords", nested))
    return sorted(pairs, key=lambda pair: pair[0])


def song_video_path(work_dir: Path) -> Path | None:
    """The rendered mp4 for work_dir, if it exists -- None if the song has
    no lyrics_timed.json yet, or hasn't been rendered yet. Shared by
    list_rendered_songs()/list_pending_uploads() below and the GUI's Watch
    button (owner request, 2026-09-15)."""
    timed_path = work_dir / "lyrics_timed.json"
    if not timed_path.exists():
        return None
    song = load_song(timed_path)
    video_path = work_dir / f"{slugify(song.title)}.mp4"
    return video_path if video_path.exists() else None


def list_rendered_songs(work_root: Path) -> list[str]:
    """Names (or "<song>/easychords" slugs -- see _candidate_song_dirs) of work_root's songs that have a
    rendered video, whether or not it's ever been uploaded to YouTube -- backs the GUI's single-song Upload
    dropdown, which (unlike list_pending_uploads()) must also offer an already-uploaded song so the owner
    can force a fresh re-upload (a correction/re-post) for any past song, not just the one from the current
    session."""
    if not work_root.exists():
        return []
    return [slug for slug, path in _candidate_song_dirs(work_root) if song_video_path(path) is not None]


def list_uploadable_songs(work_root: Path) -> list[str]:
    """The Upload to YouTube list (owner, 2026-09-20: only good videos): rendered songs, uploaded or not, that positively
    PASS the timing check at the current pass mark and carry no other concern. A song that fails, or cannot be checked
    (no transcript), is left out. Read-only: a live video is never written to here."""
    root = Path(work_root)
    return [name for name in list_rendered_songs(root) if _passes_for_upload(root / name)]


def list_easy_chord_backfill_candidates(work_root: Path) -> list[str]:
    """Names of work_root's already-passing songs whose own key is hard (not is_easy_key) and that don't
    already have their own nested `<slug>/easychords` folder -- backs the one-time "Generate EASY CHORD
    Versions" catch-up action (owner, 2026-09-23: "this idea will give me hundreds of new songs"; trigger 3
    of 3 from the design spec). A song's own capo variant is naturally excluded here too, without
    special-casing it -- its OWN key is the easy shape key it was converted to, so it never passes the
    is_easy_key check itself."""
    root = Path(work_root)
    candidates = []
    for name in list_uploadable_songs(root):
        if (root / name / "easychords").exists():
            continue
        try:
            song = load_song(root / name / "lyrics_timed.json")
        except Exception:
            continue
        if is_easy_key(song.chord_track.key):
            continue
        candidates.append(name)
    return candidates


def _passes_for_upload(song_dir: Path) -> bool:
    if verification(song_dir):
        return True                                 # "if i decide its a good video its a good video"
    try:
        concern = load_song(song_dir / "lyrics_timed.json").lyrics_accuracy_concern
    except Exception:
        return False
    if concern and not is_gate_concern(concern):
        return False
    report = check_saved_song(song_dir)
    return report is not None and report.passes


def list_pending_uploads(work_root: Path) -> list[str]:
    """Names (or "<song>/easychords" slugs) of work_root's songs that have a rendered video but no
    recorded YouTube upload yet -- backs the GUI's retry-upload dropdown for a failed upload (e.g.
    YouTube's daily uploadLimitExceeded cap) on a song that isn't self._last_work_dir (only set by
    Generate/Redo in the same session, not Batch). Filesystem-only, no live YouTube call:
    schedule_upload() only ever writes youtube_state.json AFTER a successful upload, so a missing one is
    already the right signal that nothing succeeded -- no need for a network round trip per song just to
    build this list. An EASY CHORD variant's own youtube_state.json is tracked independently of its
    original song's, so one can be pending while the other is already uploaded, or vice versa."""
    if not work_root.exists():
        return []
    return [
        slug for slug, path in _candidate_song_dirs(work_root)
        if not (path / STATE_FILENAME).exists()
        and song_video_path(path) is not None
        and not _held_for_review(path)
    ]


def needs_review(song_dir: Path) -> bool:
    """True when the song is held back from upload (a concern, or timing that fails the pass mark) and the owner has not
    approved this version -- the songs Flagged for Lyrics Review lists."""
    return _held_for_review(Path(song_dir))


def _held_for_review(song_dir: Path) -> bool:
    """A song with any lyrics/timing concern is not CLEARED (cleared_log.py): it is offered in Flagged for Lyrics
    Review, never as a pending-upload checkbox that Select All + Upload Selected could send out."""
    timed_path = song_dir / "lyrics_timed.json"
    if not timed_path.exists():
        return False
    if verification(song_dir):
        return False                                # the owner watched this version and approved it
    try:
        concern = load_song(timed_path).lyrics_accuracy_concern
    except Exception:
        return False
    if concern and not is_gate_concern(concern):
        return True
    return bool(hold_if_timing_fails(song_dir))     # keeps the timing hold in step with the bar (holds, or releases)


def list_flagged_songs(work_root: Path, include_uploaded: bool = False) -> list[str]:
    """Names (or "<song>/easychords" slugs) of work_root's songs whose lyrics were never confirmed
    accurate by check_lyric_accuracy() across every source tried (Song.lyrics_accuracy_concern non-empty)
    and that haven't been uploaded yet -- backs the GUI's "Flagged for Lyrics Review" list. Same
    not-yet-uploaded convention as list_pending_uploads() above, so a song naturally drops off this list
    once the owner uploads it anyway (via the review panel's own Upload Anyway) without needing a separate
    "dismiss" action -- and once a later Redo's fresh fetch clears the concern, it drops off too."""
    if not work_root.exists():
        return []
    flagged = []
    for slug, entry in _candidate_song_dirs(work_root):
        if song_video_path(entry) is None and not held_before_video(entry):
            continue
        if not include_uploaded and (entry / STATE_FILENAME).exists():
            continue
        timed_path = entry / "lyrics_timed.json"
        if not timed_path.exists():
            continue
        try:
            song = load_song(timed_path)
        except Exception:
            continue
        concern = song.lyrics_accuracy_concern
        if held_before_video(entry):
            flagged.append(slug)              # no video yet: Edit Lyrics / Redo / Render Anyway (whatever the mark is now)
            continue
        if verification(entry):
            continue                                # approved by the owner: not up for review any more
        if (concern and not is_gate_concern(concern)) or hold_if_timing_fails(entry):
            flagged.append(slug)
    return flagged


def load_redo_inputs(song_dir: Path) -> tuple[Path, str]:
    """Reads back the (audio_path, title) a prior run saved onto its own
    Song, so a redo never needs the owner to re-browse for the original
    audio file. Prefers run_pipeline()'s own local copy of the audio inside
    song_dir over the original external audio_path -- a batch-run song's
    original path points into a staging folder the owner empties before the
    next batch, so by the time an older song is redone that file may already
    be gone. Falls back to the original external path for a song generated
    before this local copy existed."""
    song = load_song(song_dir / "lyrics_timed.json")
    local_copy = song_dir / Path(song.audio_path).name
    if local_copy.exists():
        return local_copy, song.title
    return Path(song.audio_path), song.title


def whisper_text_for(work_dir: Path, model=None) -> str:
    """What Whisper heard sung, for the GUI's Whisper Text review button: the cached transcript if this song
    already has one (every song set aside for review does, since the checks that set it aside needed one --
    this is the instant, common case), else transcribes the vocal stem fresh via transcribe_vocals() (which
    caches its own result, so a second click is instant too). `model` is injectable for tests, same as
    transcribe_vocals() itself. Raises FileNotFoundError (via transcribe_vocals) if the song has no separated
    vocal stem to transcribe."""
    work_dir = Path(work_dir)
    cached = load_transcript_text(work_dir)
    if cached is not None:
        return cached
    audio_path, _title = load_redo_inputs(work_dir)
    vocals_path = work_dir / "htdemucs" / Path(audio_path).stem / "vocals.wav"
    return transcribe_vocals(vocals_path, work_dir, model=model)


def whisper_lines_for(work_dir: Path, model=None) -> list[str]:
    """What Whisper heard sung, one entry per LYRIC line rather than one flat block (owner request, 2026-09-22:
    "make the whisper text line by line like the lyrics text ... would make it a lot easier to figure out") --
    each line shows only the words heard within that line's own time window (the same +-SEARCH_SECONDS window
    timing_gate's sync check searches around a line's placed time), so line N here always lines up with line N
    of the lyrics for a direct side-by-side read, even where the two disagree about what's sung when. A line
    with nothing heard nearby reads as "(nothing heard)"; a blank lyric line (no words at all) is skipped, same
    as the lyrics editor already does. `model` is injectable, same as whisper_text_for()/transcribe_vocals()."""
    work_dir = Path(work_dir)
    whisper_text_for(work_dir, model=model)  # ensures transcript.json (text/segments/words) is cached
    heard = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(work_dir)]
    song = load_song(work_dir / "lyrics_timed.json")

    lines = []
    for line in song.lines:
        if not line.words:
            continue
        lines.append(heard_text_near_line(line.words, heard) or "(nothing heard)")
    return lines


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
    try:  # the permanent record of redone songs (redo_log.py); a logging problem must never break a redo
        note_redo_started(work_dir, backup_dir)
    except Exception as e:
        print(f"WARNING: could not record this redo: {type(e).__name__}: {e}", file=sys.stderr)
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


def _build_audio_check(vocals_path: Path, work_dir: Path):
    """A function scoring candidate lyric lines against what Whisper hears in the
    vocal stem (lyric_audio_match.py), or None when the transcription can't run --
    then the lyrics fetch quietly falls back to the older text-only check, so a
    missing package or blocked model download never stops a song from being made."""
    print("Listening to the vocals to check the lyrics against what is sung (about a minute)...")
    try:
        heard = transcribe_vocals(vocals_path, work_dir)
    except Exception as e:
        print(
            f"WARNING: could not check the lyrics against the audio ({type(e).__name__}: {e}); "
            "using the text-only check instead.", file=sys.stderr,
        )
        return None
    print(f"Heard {len(heard.split())} words in the vocals; checking the lyric sources against them...")
    return lambda lines: score_lyrics_against_transcript(lines, heard)


def _build_reconcile(work_dir: Path, anthropic_client):
    """The lyric-repair step handed to the lyrics fetch: shows Claude the lyrics and the
    saved transcript and returns (suggested lines, changes), or None. The suggestion is saved
    for the owner to read (SUGGESTION_FILENAME) and is NEVER used for the video itself."""
    def reconcile(lines, match):
        segments = load_transcript_segments(work_dir)
        if not segments:
            return None
        print("No lyrics source matched the audio; asking Claude for a possible fix to review...")
        result = reconcile_lyrics(anthropic_client, lines, match, segments)
        if result is not None:
            (work_dir / SUGGESTION_FILENAME).write_text("\n".join(result[0]) + "\n", encoding="utf-8")
        return result

    return reconcile


def _build_arbiter(work_dir: Path, anthropic_client):
    """The AI judge handed to the lyrics fetch: shows Claude the lyrics and the saved transcript and
    gets a verdict per unmatched stretch (lyrics wrong vs recognizer failed)."""
    def judge(lines, match):
        segments = load_transcript_segments(work_dir)
        if not segments:
            return None
        print("No lyrics source matched the audio exactly; asking Claude to judge whether the lyrics or the "
              "speech recognition is at fault...")
        return arbitrate(anthropic_client, lines, match, segments)

    return judge


def _align_lyrics(vocals_path, work_dir, parsed_lines, flat_words, audio_duration, line_times=None, needed=None):
    """(per-word times, timing concern, real sync share [None if too little was heard to judge]). Uses Whisper's
    word times (when a transcript exists) to CHECK where the
    whole-song alignment put each line and, if it drifted, to redo the alignment one bounded window at a time
    (align.align_words_anchored). Both are computed from one model pass; sync.decide_alignment picks. A song whose
    timing still cannot be trusted comes back with a non-empty concern so it is set aside for the owner's review.
    Without word timings (Whisper unavailable) this is exactly the original single whole-song alignment."""
    heard = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(work_dir)]
    sung = list(heard)      # everything the recognizer heard: what the sync check (timing_gate) compares each line with
    line_words = [[w.word for w in line.words] for line in parsed_lines]

    def settled(candidates, preferred, earlier_concern=""):
        """The timing_gate verdict (owner, 2026-09-20: at least 90% of lines within half a second of the singing) has the
        final say on which alignment is used and whether the song is set aside."""
        result = settle_alignment(candidates, line_words, sung, preferred, earlier_concern, needed)
        if result.report.share is None:
            print("Sync check: too little of the singing was recognized to compare the lines with.")
        else:
            print(
                f"Sync check: {result.report.share:.0%} of {result.report.judged_lines} lines start within half a second "
                f"of where they are sung. Using the {result.method} alignment."
            )
        if result.concern:
            print(f"WARNING: {result.concern}", file=sys.stderr)
        # share (owner, 2026-09-23: "i want to know how close it is when actually creating a video") -- the
        # real achieved sync percentage, not just pass/fail; None when too little was heard to judge at all.
        return [(min(a, audio_duration), min(b, audio_duration)) for a, b in result.times], result.concern, result.report.share

    loudness: list[float] = []
    try:  # Whisper hallucinates words in silence; they must not anchor a line (a read failure just keeps them)
        loudness = vocal_loudness(vocals_path)
        heard = drop_words_in_silence(heard, loudness, hop=0.5)
    except Exception:
        pass
    line_texts = [" ".join(w.word for w in line.words) for line in parsed_lines]
    anchors = line_anchors(line_texts, heard) if heard else {}
    # lrclib's own timestamps settle which copy of a repeated chorus Whisper heard (see combine_anchors)
    anchors = combine_anchors(anchors, line_times, [len(line.words) for line in parsed_lines])
    if not anchors:
        return settled({"whole-song": align_words(vocals_path, flat_words)}, "whole-song")

    def line_starts(times):
        starts, i = [], 0
        for line in parsed_lines:
            starts.append(times[i][0] if line.words else 0.0)
            i += len(line.words)
        return starts

    prepared = prepare_alignment(vocals_path)
    whole = align_words(vocals_path, flat_words, prepared=prepared)
    anchored, _ = align_words_anchored(
        vocals_path, [[w.word for w in line.words] for line in parsed_lines], anchors, prepared=prepared,
    )
    # Per-WORD precision against what Whisper heard decides first (the owner sees half a second); the line-level check
    # below only judges a song too few of whose words were heard for that.
    line_of_word = [li for li, line in enumerate(parsed_lines) for _ in line.words]
    evidence = match_words(
        [w.word for line in parsed_lines for w in line.words], heard, near=[[a for a, _ in whole], [a for a, _ in anchored]],
    )
    choice = choose_alignment(
        {"whole-song": whole, "anchored": anchored}, line_of_word, evidence, len(parsed_lines), loudness=loudness, hop=0.5,
    )
    if choice is not None:
        print(
            f"Timing check: {choice.precision.share:.0%} of {choice.precision.matched} heard words start within half a "
            f"second of where they were sung; {choice.precision.off_lines} of {choice.precision.lines} lines are clearly "
            f"off. Using the {choice.method} alignment."
        )
        candidates = {"whole-song": whole, "anchored": anchored}
        mixed = blend(whole, anchored, line_of_word, evidence, len(parsed_lines))
        if mixed is not None and mixed not in (whole, anchored):
            candidates["blended"] = mixed
        return settled(candidates, choice.method, choice.concern)
    decision = decide_alignment(line_starts(whole), line_starts(anchored), anchors, source_times=line_times)
    whole_report = sync_agreement(line_starts(whole), anchors)
    print(
        f"Timing check: {whole_report.anchored} of {whole_report.total_lines} lines can be checked against what was "
        f"heard; the whole-song alignment agrees on {whole_report.agreement:.0%}. Using the {decision.method} "
        f"alignment ({decision.report.agreement:.0%} agree)."
    )
    return settled({"whole-song": whole, "anchored": anchored}, decision.method, decision.concern)


HELD_MARKER = "held_before_video.json"      # written when a song is stopped before its video; removed when the video is made


class HeldBeforeVideo(RuntimeError):
    """The sync check failed, so the song was held for review BEFORE chords, images and video (owner, 2026-09-21: a song that
    does not reach the pass mark is not worth an image bill and a 20-minute render). Not an error and not a finished video:
    callers report it separately. `Render Anyway` (start_stage="detect_chords") makes the video from the saved timing."""

    def __init__(self, concern: str):
        super().__init__(concern)
        self.concern = concern


def held_before_video(work_dir: Path) -> bool:
    return (Path(work_dir) / HELD_MARKER).exists()


def _record_finished(work_dir: Path, song: Song, share: float | None = None) -> None:
    """Completes the redo record started by backup_song_outputs (a no-op for a brand-new song) and updates the running list
    of cleared / removed songs (cleared_log.py). `share` (owner, 2026-09-23: "i want to know how close it is when
    actually creating a video") is the real achieved sync percentage from THIS run's own align stage -- None when
    align didn't run this invocation (a low-level --stage resume past it) or too little was heard to judge; either
    way the note falls back to the old plain text rather than claiming a number that isn't real. A REMOVED song's
    note is the full concern text, which already states its own percentage -- only the cleared branch needed one."""
    try:
        note_redo_finished(work_dir, concern=song.lyrics_accuracy_concern)
        try:
            slug = display_slug(work_dir)
            if song.lyrics_accuracy_concern:
                record_removed(slug, song.lyrics_accuracy_concern[:300])
            else:
                note = f"passed the lyric and timing checks at {percent_display(share)}" if share is not None else "passed the lyric and timing checks"
                record_cleared(slug, note)
        except Exception as e:
            print(f"WARNING: could not update the cleared-songs record ({type(e).__name__}: {e})", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: could not record this redo: {type(e).__name__}: {e}", file=sys.stderr)


def _hold_before_video(work_dir: Path, final_path: Path, song: Song, concern: str) -> None:
    """Stops the run here. A video left from before a Redo is moved aside (a redo backup of it already exists) so the old
    video is never mistaken for the new timing's."""
    if final_path.exists():
        final_path.replace(final_path.with_name(final_path.stem + ".previous.mp4"))
    (work_dir / HELD_MARKER).write_text(
        json.dumps({"reason": concern, "at": datetime.now().astimezone().isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )
    _record_finished(work_dir, song)
    raise HeldBeforeVideo(concern)


def run_pipeline(
    audio_path: Path,
    work_dir: Path,
    title: str | None = None,
    start_stage: str = "identify",
    font_path: str | None = None,
    settings: Settings | None = None,
    progress_callback: Callable[[str], None] | None = None,
    end_stage: str = "render",
    capo: int | None = None,
) -> Path | None:
    """`end_stage` (owner, 2026-09-22: vetting a candidate song's real timing-gate share -- deep_review-style,
    or the most-popular-songs picker -- must never reach detect_chords/images/render, which cost real
    Replicate/Claude money this kind of check has no business spending) stops the run right after the named
    stage; every later stage, including detect_chords, is never entered. Returns None instead of the (nonexistent)
    video path whenever the run stops before render actually happens; the default ("render") reproduces every
    existing caller's behavior exactly, always returning the finished mp4's path.

    `capo` (owner, 2026-09-23) is passed straight to assemble_video() -- None (the default) draws no badge at
    all; an EASY CHORD variant's own build passes its real capo fret so the CAPO N badge appears only there."""
    start_idx = STAGES.index(start_stage)
    end_idx = STAGES.index(end_stage)
    # None (the CLI's default, and every call before this feature existed) means
    # "use every one of Settings' own defaults" -- which are themselves exactly
    # today's hardcoded values, so this is a no-op for anyone not using the GUI's
    # new Settings panel.
    detect_kwargs: dict = {}
    assemble_kwargs: dict = {}
    if settings is not None:
        detect_kwargs = {
            "snap_chords_to_key": settings.snap_chords_to_key,
            "prefer_flats": settings.prefer_flats,
            "include_seventh_chords": settings.include_seventh_chords,
            "min_chord_seconds": settings.min_chord_seconds,
        }
        assemble_kwargs = {
            **settings.render_kwargs(),
            "fps": settings.fps,
            "encoder": settings.encoder,
            "crf": settings.crf,
            "countdown_beats": settings.countdown_beats,
        }

    def report(stage: str) -> None:
        if progress_callback is not None:
            progress_callback(stage)

    work_dir.mkdir(parents=True, exist_ok=True)
    # A local copy of the source audio, kept for load_redo_inputs() -- a batch
    # song's original audio_path points into a staging folder the owner
    # empties before the next batch, so it can be gone by the time a later
    # Redo needs it. Skipped once the copy already exists (a resume past an
    # earlier stage, or a Redo that's already reading from this same copy).
    audio_copy_path = work_dir / Path(audio_path).name
    if Path(audio_path).exists() and not audio_copy_path.exists():
        shutil.copy2(audio_path, audio_copy_path)
    # Every stage below reads from THIS copy from here on, not the original
    # external path -- real bug, 2026-09-15: fetch_lyrics' sidecar .lrc/.txt
    # lookup checks next to whatever audio_path currently points at, so a
    # sidecar the owner drops next to this work_dir copy (the file the
    # error message's own filename hint refers to) was never actually being
    # found while audio_path still meant the original location. Same fix
    # also helps the "batch staging folder gone by resume time" case this
    # copy already existed to solve for Redo (see load_redo_inputs below).
    if audio_copy_path.exists():
        audio_path = audio_copy_path

    # Matches separate_vocals()'s own output path convention, so resuming from a
    # later stage (skipping separation) still finds the file it already wrote.
    demucs_dir = work_dir / "htdemucs" / Path(audio_path).stem
    vocals_path = demucs_dir / "vocals.wav"
    instrumental_stem_path = demucs_dir / "no_vocals.wav"
    info_path = work_dir / "song_info.json"
    lyrics_path = work_dir / "lyric_lines.json"
    timed_path = work_dir / "lyrics_timed.json"
    images_dir = work_dir / "images"

    def run_identify() -> str:
        info = extract_metadata(audio_path)
        # A caller-supplied title overrides identify's own guess for DISPLAY/
        # filename purposes; artist/duration always come from identify -- there is
        # no separate artist-override field. Only relevant on a fresh run: a redo
        # resuming past this stage ignores this parameter entirely and reads the
        # ORIGINAL run's resolved title back from song_info.json below, so the
        # video's filename never changes between the original run and a redo.
        resolved = title.strip() if title and title.strip() else info.title
        info_path.write_text(
            json.dumps({
                "title": resolved, "artist": info.artist,
                "duration": info.duration, "alt_titles": info.alt_titles,
            }),
            encoding="utf-8",
        )
        return resolved

    if start_idx <= STAGES.index("identify"):
        report("identify")
        resolved_title = run_identify()
    elif info_path.exists():
        info_data = json.loads(info_path.read_text(encoding="utf-8"))
        resolved_title = info_data["title"]
    else:
        # A redo of a song created before this stage existed -- no song_info.json
        # was ever written for it. Bootstrap one now via a fresh identify call
        # (free: tags/filename/lrclib/MusicBrainz, no Claude/Replicate spend)
        # regardless of the requested start_stage, since every later stage needs
        # this file. Confirmed real: every pre-merge work/ song hit this.
        report("identify")
        resolved_title = run_identify()

    final_path = work_dir / f"{slugify(resolved_title)}.mp4"

    def song_seconds() -> float | None:
        try:
            return float(json.loads(info_path.read_text(encoding="utf-8"))["duration"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    if start_idx <= STAGES.index("separate") <= end_idx:
        report("separate")
        vocals_path = separate_vocals(audio_path, work_dir, expected_seconds=song_seconds())
    elif start_idx <= STAGES.index("detect_chords") and end_idx >= STAGES.index("align") and not stems_look_complete(
        vocals_path, instrumental_stem_path, song_seconds(),
    ):
        # Resuming past separation, but the stems the align/detect_chords
        # stages below read aren't on disk (an htdemucs/ folder deleted to
        # save space, or a batch "already done" song whose stems never made
        # it here) -- those stages would crash on the missing file. Demucs
        # is deterministic and costs no API spend, so just run it again --
        # the same self-heal the identify bootstrap above applies to a
        # missing song_info.json. A resume at images/render never reads the
        # stems, so it's left alone. Stems that exist but are TRUNCATED (real:
        # 'Ironic', 43 s of a 230 s song) get the same treatment.
        report("separate")
        if vocals_path.exists():
            print("The saved vocal stems are missing or shorter than the song; running Demucs again.")
        vocals_path = separate_vocals(audio_path, work_dir, expected_seconds=song_seconds())

    if start_idx <= STAGES.index("fetch_lyrics") <= end_idx:
        report("fetch_lyrics")
        info_data = json.loads(info_path.read_text(encoding="utf-8"))
        owner_lines = owner_lyrics_lines(work_dir)
        if owner_lines is not None:
            # The owner edited these lyrics by hand (Flagged for Lyrics Review > Edit Lyrics): their word is final,
            # so no online source, AI or audio check may override them. Whisper's word timings are still needed by
            # the aligner as anchors (cached), and the aligner and sync check still time them.
            print("Using the lyrics you edited (lyrics_owner.txt); no online lookup or AI check for these.")
            try:
                transcribe_vocals(vocals_path, work_dir)
            except Exception as e:
                print(f"WARNING: could not listen to the vocals ({type(e).__name__}: {e}); timing will use the "
                      "whole-song alignment.", file=sys.stderr)
            lines_text, lyrics_source, lyrics_concern, times_out = owner_lines, "owner", "", {}
        else:
            lyrics_anthropic_client = anthropic.Anthropic()
            times_out: dict = {}
            audio_check = _build_audio_check(vocals_path, work_dir)
            reconcile = _build_reconcile(work_dir, lyrics_anthropic_client) if audio_check is not None else None
            arbiter = _build_arbiter(work_dir, lyrics_anthropic_client) if audio_check is not None else None
            lines_text, lyrics_source, lyrics_concern = fetch_lyric_lines_verified(
                audio_path, info_data["title"], info_data["artist"], info_data["duration"],
                info_data.get("alt_titles"), lyrics_anthropic_client, audio_check=audio_check, reconcile=reconcile,
                arbiter=arbiter, times_out=times_out,
            )
            if audio_check is not None:
                if lyrics_concern:
                    print(
                        f"WARNING: the lyrics could not be confirmed against the audio "
                        f"({lyrics_source or 'no source'}): {lyrics_concern} This song is held in "
                        "Flagged for Lyrics Review instead of auto-uploading.", file=sys.stderr,
                    )
                elif lyrics_source.endswith("+ai-confirmed"):
                    print(
                        f"Lyrics accepted after AI review (source: {lyrics_source.split('+')[0]}): the stretches the "
                        "audio check could not match were judged speech-recognition errors, not lyric errors."
                    )
                else:
                    print(f"Lyrics verified against the audio (source: {lyrics_source}).")
        if owner_lines is None and times_out.get("line_times"):
            # Credits arrive as fake lyric lines stamped near 0:00; "if its not part of the audio, its a credit"
            try:
                heard_words = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(work_dir)]
                loudness_now = vocal_loudness(vocals_path)
                lines_text, times_out["line_times"], dropped_lines = drop_unsung_leading_lines(
                    lines_text, times_out["line_times"], heard_words, loudness_now, hop=0.5,
                )
                lines_text, times_out["line_times"], dropped_tail = drop_unsung_trailing_lines(
                    lines_text, times_out["line_times"], heard_words, loudness_now, hop=0.5,
                )
                dropped_lines = dropped_lines + dropped_tail
                if dropped_lines:
                    print("Removed lines that are not part of the song's audio (credits): " + "; ".join(dropped_lines))
            except Exception:
                pass                                        # no audio evidence: keep every line, as before
        lyrics_path.write_text(
            json.dumps({
                "lines": lines_text, "source": lyrics_source, "concern": lyrics_concern,
                "line_times": times_out.get("line_times"),
            }),
            encoding="utf-8",
        )

    timing_share = None       # real achieved sync %, only known once align actually runs this invocation
    if start_idx <= STAGES.index("align") <= end_idx:
        report("align")
        lyrics_data = json.loads(lyrics_path.read_text(encoding="utf-8"))
        if isinstance(lyrics_data, list):
            # Legacy format from before fetch_lyric_lines_verified() existed:
            # lyric_lines.json was a bare list of line strings, no source/concern.
            lines_text, lyrics_source, lyrics_concern = lyrics_data, "", ""
        else:
            lines_text = lyrics_data.get("lines", [])
            lyrics_source = lyrics_data.get("source", "")
            lyrics_concern = lyrics_data.get("concern", "")
        parsed_lines = [LyricLine(words=[Word(word=w) for w in text.split()]) for text in lines_text]
        if not any(line.words for line in parsed_lines):
            # Checked here (not in fetch_lyrics) so a --stage align resume
            # with an empty lyric_lines.json gets the same clear message.
            # Without it the stage died inside the aligner with "no words to
            # align", which says nothing about what to do next.
            raise RuntimeError(
                f"No lyrics were found for '{resolved_title}' (every online lookup came up "
                f"empty, or the track was flagged instrumental). Save the lyrics as "
                f"'{Path(audio_path).stem}.lrc' or '{Path(audio_path).stem}.txt' next to the "
                "audio file and run it again -- a play-along video can't be built without "
                "lyric text."
            )
        waveform, sample_rate = torchaudio.load(str(vocals_path))
        audio_duration = waveform.shape[1] / sample_rate
        flat_words = [w.word for line in parsed_lines for w in line.words]
        line_times = lyrics_data.get("line_times") if isinstance(lyrics_data, dict) else None
        word_times, timing_concern, timing_share = _align_lyrics(
            vocals_path, work_dir, parsed_lines, flat_words, audio_duration, line_times=line_times,
            needed=settings.timing_pass_percent / 100 if settings is not None else None,
        )
        if timing_concern:
            lyrics_concern = f"{lyrics_concern} {timing_concern}".strip()
        timed_lines = combine_alignment(parsed_lines, word_times, audio_duration)
        song = Song(
            title=resolved_title,
            audio_path=str(audio_path),
            vocal_stem_path=str(vocals_path),
            instrumental_stem_path=str(instrumental_stem_path),
            lines=timed_lines,
            lyrics_source=lyrics_source,
            lyrics_accuracy_concern=lyrics_concern,
        )
        save_song(song, timed_path)
        if timing_concern and is_gate_concern(timing_concern):
            _hold_before_video(work_dir, final_path, song, timing_concern)          # raises HeldBeforeVideo
    elif start_idx > STAGES.index("align"):
        song = load_song(timed_path)
    # else: end_stage stops before align even starts (e.g. a vetting-only "identify"/"separate"/"fetch_lyrics"
    # run) -- `song` is intentionally left unset here; every block below that would use it is itself gated by
    # end_idx and the function returns before any of them can run.

    if start_idx <= STAGES.index("detect_chords") <= end_idx:
        report("detect_chords")
        song.chord_track = detect_chords(instrumental_stem_path, **detect_kwargs)
        save_song(song, timed_path)

    if start_idx <= STAGES.index("images") <= end_idx:
        report("images")
        anthropic_client = anthropic.Anthropic()
        replicate_token = os.environ.get("REPLICATE_API_TOKEN", "")
        if not replicate_token:
            raise RuntimeError(
                "REPLICATE_API_TOKEN is not set -- add it to the .env file at the repo root "
                "(see .env.example). The images stage can't generate backgrounds without it."
            )
        full_lyrics = "\n".join(l.text for l in song.lines)
        song_gist = summarize_song_gist(anthropic_client, full_lyrics)
        images_dir.mkdir(exist_ok=True)
        # Reuse already-paid-for images from any prior images_backup_*/ archive
        # before spending on a new one (unchanged convention).
        backup_dirs = sorted(work_dir.glob("images_backup_*"))
        image_paths = []
        # last_real_image lets a failed generation immediately reuse the most
        # recent REAL image instead of ever writing a flat color to disk --
        # a content-filter rejection in particular repeats identically on
        # every retry, so waiting on substitute_fallback_images()'s later
        # pass to fix it up risked a plain-color frame reaching the finished
        # video if anything ever prevented that pass from running (owner
        # incident, 2026-09-18). substitute_fallback_images() still runs
        # afterward and can improve on this with a chronologically closer
        # neighbor once the whole song's images are known.
        last_real_image: Path | None = None
        for line in song.lines:
            path = get_or_generate_image(
                anthropic_client, replicate_token, song_gist, line.text, images_dir,
                extra_cache_dirs=backup_dirs, previous_image=last_real_image,
            )
            image_paths.append(path)
            if not is_fallback_image(path):
                last_real_image = path
        # Instrumental-gap images (2026-09-09 owner request): one per distinct
        # caption the render's own image timeline can look up, so the
        # background follows the chord instead of freezing on the last-sung
        # line's image. The caption list comes from the very same gap/segment
        # walk build_image_timeline() performs (layout.instrumental_image_
        # captions), never a separate approximation of it -- the earlier
        # midpoint-based rule here skipped chords that only overlapped a gap's
        # edge, leaving the render to show a flat placeholder for them.
        for caption in instrumental_image_captions(song.lines, song.chord_track, song_end_time(song)):
            path = get_or_generate_image(
                anthropic_client, replicate_token, song_gist, caption, images_dir,
                extra_cache_dirs=backup_dirs, previous_image=last_real_image,
            )
            image_paths.append(path)
            if not is_fallback_image(path):
                last_real_image = path
        # A flat placeholder color would visibly break the finished video even
        # though a generation failure never crashes the pipeline -- substitute
        # a real neighboring image in for any fallback, as an absolute last
        # resort only after every real generation attempt has already failed.
        substitute_fallback_images(image_paths)

    if start_idx <= STAGES.index("render") <= end_idx:
        report("render")
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            font_path or default_font(),
            chord_legend_labels=ordered_unique_chords(song.chord_track),
            capo=capo,
            **assemble_kwargs,
        )
        (work_dir / HELD_MARKER).unlink(missing_ok=True)        # the video exists now (Render Anyway, or a Redo that passes)

        # Owner, 2026-09-23: "have a easy chord setting, so if i have that checked it will convert to easy
        # chord?" -- "any video make." Every future Generate/Redo/Batch run that lands in a hard key also
        # gets its own EASY CHORD (capo) variant when this is on. Never for an already-easy key (nothing to
        # convert -- build_capo_variant would itself return None, but checking here too avoids even trying).
        # A problem building the variant must never make an otherwise-successful primary video look like it
        # failed (same reasoning as the existing YouTube-upload failure handling) -- caught and logged, not raised.
        if settings is not None and settings.generate_easy_chord_versions and not is_easy_key(song.chord_track.key):
            try:
                build_capo_variant(work_dir)
            except Exception as e:
                log.warning("Could not build the EASY CHORD (capo) variant for %s: %s", work_dir, e)

    if end_idx < STAGES.index("render"):
        # Stopped early (a vetting-only call): no video was made, so there is no redo/cleared-log entry to
        # write and nothing done-worthy to report -- returning None (rather than a nonexistent path) is the
        # caller's own signal that this call never intended to produce a video.
        return None

    _record_finished(work_dir, song, share=timing_share)
    report("done")
    return final_path


def build_capo_variant(work_dir: Path, audio_path_override: Path | str | None = None) -> Path | None:
    """Builds and renders a `<work_dir>/easychords` work dir NESTED inside the original song's own folder
    (owner, 2026-09-23: "i dont need twice the folder" -- was a sibling `<slug>-capo` folder next to it
    until this point): the same song's lyrics, timing, and images, converted to easy open-chord shapes via
    a capo -- see docs/superpowers/specs/2026-09-23-capo-easy-chord-videos-design.md ("EASY CHORD Play
    Along videos"). No new AI/Replicate spend: images are copied from the original, never regenerated, and
    only the render stage runs. Still tracked/listed/uploadable as its own distinct song everywhere else
    (list_rendered_songs()/list_uploadable_songs()/list_pending_uploads() all look one level deeper for
    this "easychords" subfolder) -- models.display_slug() gives it a unique slug ("<song>/easychords")
    wherever a bare directory name (always literally "easychords") would otherwise collide across songs.

    Returns None (nothing built) if `work_dir` has no finished `lyrics_timed.json` yet, or if the
    song's own key is already easy (capo_and_shape_key) -- there's nothing to convert. Idempotent:
    a second call just re-renders the same nested dir.

    `audio_path_override` is for the case load_redo_inputs() can't resolve on its own -- a song
    recorded before run_pipeline() kept its own local audio copy, whose original external
    audio_path has since been cleaned up from its batch-staging folder. The normal case (a local
    copy exists, or the original path is still there) needs no override."""
    work_dir = Path(work_dir)
    timed_path = work_dir / "lyrics_timed.json"
    if not timed_path.exists():
        return None
    song = load_song(timed_path)
    result = capo_and_shape_key(song.chord_track.key)
    if result is None:
        return None
    capo_fret, shape_key = result

    info_path = work_dir / "song_info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    original_title = info["title"]

    capo_work_dir = work_dir / "easychords"
    capo_work_dir.mkdir(parents=True, exist_ok=True)

    capo_info = {**info, "title": f"{original_title} EasyChords"}
    (capo_work_dir / "song_info.json").write_text(json.dumps(capo_info), encoding="utf-8")
    save_easy_chord_capo_marker(
        capo_work_dir, capo_fret=capo_fret, shape_key=shape_key,
        original_key=song.chord_track.key, original_title=original_title,
    )

    capo_song = Song(
        title=capo_info["title"],
        audio_path=song.audio_path,
        vocal_stem_path=song.vocal_stem_path,
        instrumental_stem_path=song.instrumental_stem_path,
        lines=song.lines,
        chord_track=transpose_chord_track(song.chord_track, capo_fret, shape_key),
        image_cache=song.image_cache,
        lyrics_source=song.lyrics_source,
        lyrics_accuracy_concern=song.lyrics_accuracy_concern,
    )
    save_song(capo_song, capo_work_dir / "lyrics_timed.json")

    images_dir = work_dir / "images"
    capo_images_dir = capo_work_dir / "images"
    if images_dir.exists() and not capo_images_dir.exists():
        shutil.copytree(images_dir, capo_images_dir)

    if audio_path_override is not None:
        audio_path = Path(audio_path_override)
    else:
        audio_path, _resolved_title = load_redo_inputs(work_dir)

    return run_pipeline(
        audio_path, capo_work_dir, title=capo_info["title"],
        start_stage="render", end_stage="render", capo=capo_fret,
    )


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

    try:
        out = run_pipeline(args.audio, args.work_dir, args.title, args.stage, args.font)
    except HeldBeforeVideo as held:
        print(f"Held for review -- no video was made: {held.concern}")
        return
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
