from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

# Every import below is kept light (issue #7: the GUI imports this module for the song lists, and used to wait for torch,
# torchaudio and the Anthropic SDK before its window could appear): align.py loads torch/torchaudio only inside the
# functions that run the model, the align stage reads the stem's length without torchaudio (_vocal_stem_seconds), and the
# Anthropic SDK is loaded on first use (`anthropic`, below the imports).
from .align import align_words, align_words_anchored, prepare_alignment, vocal_loudness
from .anchors import HeardWord, combine_anchors, drop_words_in_silence, line_anchors
from .assemble import assemble_video
from .combine import combine_alignment
from .detect_chords import detect_chords
from .key_decision import apply_saved_owner_key, key_needs_attention, load_decision, load_owner_key, settle_song_key
from .fetch_lyrics import fetch_lyric_lines_verified
from .identify import extract_metadata
from .imagery import get_or_generate_image, is_fallback_image, substitute_fallback_images, summarize_song_gist
from .layout import instrumental_image_captions
from .library_session import open_library_session
from .lyric_arbiter import arbitrate
from .lyric_audio_match import drop_unsung_leading_lines, drop_unsung_trailing_lines, score_lyrics_against_transcript
from .lyric_reconcile import SUGGESTION_FILENAME, reconcile_lyrics
from .owner_lyrics import owner_lyrics_lines
from .chord_theory import (  # noqa: F401 -- ordered_unique_chords re-exported: every existing `pipeline.ordered_unique_chords` caller keeps working unchanged
    capo_and_shape_key, capo_track_matches, is_easy_key, load_easy_chord_capo_marker, ordered_unique_chords, save_easy_chord_capo_marker, transpose_chord_track,
)
from .cleared_log import record_cleared, record_removed
from .redo_log import note_redo_finished, note_redo_started
from .models import ChordTrack, LyricLine, Song, Word, display_slug, load_song, save_song
from .separate import separate_vocals, stems_look_complete
from .precision import blend, choose_alignment, match_words
from .sync import decide_alignment, sync_agreement
from .settings import Settings
from .owner_verified import FILENAME as OWNER_VERIFIED_FILE, verification
from .timing_gate import (
    check_saved_song, check_sync, file_signature, heard_text_near_line, is_gate_concern, percent_display, settle_alignment,
    timing_verdict,
)
from .owner_whisper import add_corrections, corrected_heard_words
from .transcribe import (
    load_transcript_segments, load_transcript_text, load_transcript_words, lyric_hotwords, transcribe_vocals,
)
from .youtube_state import STATE_FILENAME


class _LazyModule:
    """Stands in for a heavy third-party module until the first attribute is used: `anthropic.Anthropic()` in the stages below
    imports the real SDK at that moment (in the worker thread that runs the pipeline), never when the GUI merely imports this
    module. A test can still replace the whole name with monkeypatch."""

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attribute: str):
        return getattr(importlib.import_module(self._name), attribute)


anthropic = _LazyModule("anthropic")

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


@dataclass(frozen=True)
class _SongFacts:
    """What the song lists need from one folder's lyrics_timed.json (plus whether the owner verified that version)."""
    title: str
    concern: str
    key: str
    verified: bool


# Issue #7 ("the pages opens very slowly"): every song list used to parse each song's ~80 KB lyrics_timed.json three or
# four times, plus its transcript and a full sync check, on every open. _SongFacts is now parsed once per VERSION of the
# file -- keyed by the folder and remembered with the (inode, mtime_ns, size) of lyrics_timed.json and owner_verified.json
# -- and the sync verdict is cached the same way in timing_gate.check_saved_song. Only small facts are kept, never the
# parsed Song. Guarded by a lock: the lists are built on the Tk thread and on background threads (the 20-minute tick).
_FACTS: dict[str, tuple[tuple, _SongFacts | None]] = {}
_FACTS_LOCK = threading.Lock()
_FACTS_LIMIT = 5000


def _song_facts(song_dir: Path) -> _SongFacts | None:
    """song_dir's facts, parsed at most once per version of its files. None when it has no lyrics_timed.json, or one that
    cannot be read -- that song is left out of every list (with one warning naming the file) instead of one bad file
    breaking the Upload, Pending and Flagged lists for every song (issue #7 review). The same parse also primes the timing
    check's cached verdict (timing_gate.check_saved_song), so a list never parses the file twice."""
    song_dir = Path(song_dir)
    timed_path = song_dir / "lyrics_timed.json"
    signature = (file_signature(timed_path), file_signature(song_dir / OWNER_VERIFIED_FILE))
    if signature[0] is None:
        return None
    key = os.path.abspath(song_dir)
    with _FACTS_LOCK:
        cached = _FACTS.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        song = load_song(timed_path)
        facts = _SongFacts(
            title=song.title, concern=song.lyrics_accuracy_concern or "", key=song.chord_track.key or "",
            verified=verification(song_dir, song=song) is not None,
        )
    except OSError as e:                            # e.g. a sharing violation right now: try again next time, no cache
        print(f"WARNING: could not read {timed_path} ({type(e).__name__}: {e}); left out of the song lists.", file=sys.stderr)
        return None
    except Exception as e:                          # damaged contents: remembered until the file changes, warned once
        print(f"WARNING: {timed_path} could not be read ({type(e).__name__}: {e}); that song is left out of the song lists "
              "until it is fixed or redone.", file=sys.stderr)
        facts = None
    else:
        if not facts.verified and not (facts.concern and not is_gate_concern(facts.concern)):
            try:                                    # the lists that need the timing verdict get it without a second parse
                check_saved_song(song_dir, song=song, timed_signature=signature[0])
            except Exception:
                pass
        del song                                    # only the small facts are kept
    with _FACTS_LOCK:
        if len(_FACTS) >= _FACTS_LIMIT:
            _FACTS.clear()
        _FACTS[key] = (signature, facts)
    return facts


def _songs_where(work_root: Path, keep: Callable[[Path], bool]) -> list[str]:
    """The slugs of work_root's candidate song folders (see _candidate_song_dirs) that `keep` accepts. A song whose check
    raises anyway is skipped with a warning -- one song never breaks a whole list."""
    kept = []
    for slug, path in _candidate_song_dirs(work_root):
        try:
            if keep(path):
                kept.append(slug)
        except Exception as e:
            print(f"WARNING: {slug} left out of the song list ({type(e).__name__}: {e})", file=sys.stderr)
    return kept


def song_video_path(work_dir: Path) -> Path | None:
    """The rendered mp4 for work_dir, if it exists -- None if the song has
    no lyrics_timed.json yet (or one that cannot be read), or hasn't been rendered yet. Shared by
    list_rendered_songs()/list_pending_uploads() below and the GUI's Watch
    button (owner request, 2026-09-15)."""
    facts = _song_facts(work_dir)
    return None if facts is None else _video_of(work_dir, facts)


def _video_of(work_dir: Path, facts: _SongFacts) -> Path | None:
    video_path = Path(work_dir) / f"{slugify(facts.title)}.mp4"
    return video_path if video_path.exists() else None


def list_rendered_songs(work_root: Path) -> list[str]:
    """Names (or "<song>/easychords" slugs -- see _candidate_song_dirs) of work_root's songs that have a
    rendered video, whether or not it's ever been uploaded to YouTube -- backs the GUI's single-song Upload
    dropdown, which (unlike list_pending_uploads()) must also offer an already-uploaded song so the owner
    can force a fresh re-upload (a correction/re-post) for any past song, not just the one from the current
    session."""
    if not work_root.exists():
        return []
    return _songs_where(work_root, lambda path: song_video_path(path) is not None)


def list_uploadable_songs(work_root: Path) -> list[str]:
    """The Upload to YouTube list (owner, 2026-09-20: only good videos): rendered songs, uploaded or not, that positively
    PASS the timing check at the current pass mark and carry no other concern. A song that fails, or cannot be checked
    (no transcript), is left out; an EASY CHORD variant is judged against its song's transcript. Read-only."""
    root = Path(work_root)
    if not root.exists():
        return []
    return _songs_where(root, lambda path: song_video_path(path) is not None and _passes_for_upload(path))


# --- EASY CHORD (capo) versions: which songs can have one built, and whether an existing one still matches its song ----
# Issue #7 review ("check easy chords for proper capo and chords"): the backfill list offered songs build_capo_variant then
# refused (key never settled, audio gone) and hid every song with an `easychords` FOLDER, even one whose render was killed
# (no video) or whose song was redone since (old lyrics, timing, chords or key). These helpers answer both questions
# read-only and cheaply (cached per version of the files involved) -- the lists run them for every song.

EASY_DIR = "easychords"
EASY_STALE_PREFIX = "EASY CHORD version out of date:"
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
_EASY_CACHE: dict[str, tuple[tuple, object]] = {}
_EASY_CACHE_LOCK = threading.Lock()


def _easy_cached(kind: str, path: Path, signature: tuple, compute: Callable[[], object]):
    key = f"{kind}:{os.path.abspath(path)}"
    with _EASY_CACHE_LOCK:
        cached = _EASY_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    value = compute()
    with _EASY_CACHE_LOCK:
        if len(_EASY_CACHE) >= _FACTS_LIMIT:
            _EASY_CACHE.clear()
        _EASY_CACHE[key] = (signature, value)
    return value


def _song_info_title(song_dir: Path) -> str:
    try:
        info = json.loads((Path(song_dir) / "song_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(info.get("title") or "") if isinstance(info, dict) else ""


def _available_audio(song_dir: Path) -> Path | None:
    """The audio an EASY CHORD version would be rendered with, when it exists (load_redo_inputs' rule: the song folder's own
    copy, else the file the song was made from); None when it is gone. The folder's own copy answers without reading
    lyrics_timed.json; a song made before that copy existed has its saved audio_path read once per version of the file."""
    song_dir = Path(song_dir)
    try:
        with os.scandir(song_dir) as entries:
            for entry in entries:
                if os.path.splitext(entry.name)[1].lower() in _AUDIO_SUFFIXES and entry.is_file():
                    return Path(entry.path)
    except OSError:
        return None
    timed = song_dir / "lyrics_timed.json"

    def saved_audio_path() -> str:
        try:
            return str(load_song(timed).audio_path or "")
        except Exception:
            return ""

    saved = _easy_cached("audio", song_dir, (file_signature(timed),), saved_audio_path)
    return Path(saved) if saved and Path(saved).is_file() else None


def easy_chord_build_problem(
    work_dir: Path, audio_path_override: Path | str | None = None, *, check_audio: bool = True,
) -> str:
    """Why build_capo_variant(work_dir) would build nothing -- "" when it can build the song's EASY CHORD version: the song
    must be made, its key settled (key_decision.py -- the capo and the stated key come from it), that key hard (an easy key
    needs no capo), and its audio still on disk (not checked with `audio_path_override`, or `check_audio=False`). Read-only;
    the backfill list, the build itself and the GUI's report of a song that was not built all use it, so the list never
    offers a song the build will refuse."""
    from .key_decision import load_decision

    work_dir = Path(work_dir)
    if work_dir.name == EASY_DIR:
        return "it is itself an EASY CHORD version -- it is made from its song, never converted again."
    facts = _song_facts(work_dir)
    if facts is None:
        return "the song has no readable lyrics_timed.json -- make the song first."
    decision = load_decision(work_dir)
    if decision is None:
        return "its key has not been checked yet -- settle it first (Set Key, or scripts/settle_keys.py)."
    if not decision.confirmed:
        return "its key is waiting for you to confirm it (Set Key in Flagged for Lyrics Review)."
    if capo_and_shape_key(facts.key) is None:
        if is_easy_key(facts.key):
            return f"its key ({facts.key}) is already easy -- it needs no capo."
        return f"its key ({facts.key or 'unknown'}) is not one a capo can turn into open chords."
    if not _song_info_title(work_dir):
        return "its song_info.json is missing or has no title."
    if check_audio and audio_path_override is None and _available_audio(work_dir) is None:
        return "its audio file is gone (no copy in the song's folder, and the file it was made from is missing)."
    return ""


def easy_variant_problem(variant_dir: Path) -> str:
    """Why the EASY CHORD version in `variant_dir` (`<song>/easychords`) no longer matches its song and has to be made
    again before it may upload: made for another key or capo, its chords no longer the song's own shifted by the capo, or
    its lyrics / their timing no longer the song's (the song was redone since). "" when it still matches and for any other
    folder. The key checks are youtube_schedule.easy_chord_upload_problem's -- the same ones the upload itself runs -- so
    the lists and the upload never disagree. Read-only; cached per version of the four files it reads."""
    from .key_decision import KEY_DECISION_FILE
    from .chord_theory import EASY_CHORD_MARKER_FILENAME

    variant_dir = Path(variant_dir)
    if variant_dir.name != EASY_DIR:
        return ""
    song_dir = variant_dir.parent
    signature = tuple(file_signature(path) for path in (
        variant_dir / "lyrics_timed.json", variant_dir / EASY_CHORD_MARKER_FILENAME,
        song_dir / "lyrics_timed.json", song_dir / KEY_DECISION_FILE,
    ))
    if signature[0] is None:
        return ""
    return _easy_cached("stale", variant_dir, signature, lambda: _easy_variant_problem_now(variant_dir))


def _easy_variant_problem_now(variant_dir: Path) -> str:
    from .key_decision import load_decision
    from .owner_verified import timing_fingerprint
    from .youtube_schedule import easy_chord_upload_problem      # here, not at the top: youtube_schedule imports this module

    try:
        problem = easy_chord_upload_problem(variant_dir)
    except Exception as e:
        return f"it could not be checked against its song ({type(e).__name__}: {e}) -- make the EASY CHORD version again."
    if problem:
        return problem
    marker = load_easy_chord_capo_marker(variant_dir)
    capo_fret = marker.get("capo_fret") if isinstance(marker, dict) else None
    if not isinstance(capo_fret, int) or isinstance(capo_fret, bool):
        return "its EASY CHORD marker (easy_chord_capo.json) is missing or unreadable -- make the EASY CHORD version again."
    song_dir = variant_dir.parent
    try:
        song = load_song(song_dir / "lyrics_timed.json")
        easy = load_song(variant_dir / "lyrics_timed.json")
    except Exception as e:
        return f"its song could not be read to check it ({type(e).__name__}) -- make the EASY CHORD version again."
    if timing_fingerprint(easy) != timing_fingerprint(song):
        return ("its lyrics or their timing are no longer the song's own (the song was redone since) -- make the EASY "
                "CHORD version again before uploading it.")
    if not capo_track_matches(song.chord_track, easy.chord_track, capo_fret):
        return (f"its chords are no longer the song's own chords shifted by capo {capo_fret} (the song was redone since) "
                "-- make the EASY CHORD version again before uploading it.")
    decision = load_decision(song_dir)
    if decision is not None and decision.confirmed:
        wanted = capo_and_shape_key(decision.key or "")
        if wanted is None:
            return f"the song's key is now {decision.key}, which needs no capo -- this EASY CHORD version no longer applies."
        if wanted != (capo_fret, marker.get("shape_key")):
            return (f"it was made with capo {capo_fret} ({marker.get('shape_key')} shapes) but the song's key {decision.key} "
                    f"needs capo {wanted[0]} ({wanted[1]} shapes) -- make the EASY CHORD version again.")
    return ""


@dataclass(frozen=True)
class EasyChordBackfill:
    """The "Generate EASY CHORD Versions" list: the songs, a row label for a song whose EASY version exists but is out of
    date or has no video, and how many hard-key songs are left out only because their key is not settled yet."""
    songs: list[str]
    labels: dict[str, str]
    waiting_for_key: int


def easy_chord_backfill_listing(work_root: Path) -> EasyChordBackfill:
    """Passing songs (list_uploadable_songs) that build_capo_variant WILL build (easy_chord_build_problem is "") and that
    have no current EASY CHORD version: no `<song>/easychords` video, or one that no longer matches its song
    (easy_variant_problem). A folder alone no longer counts -- a killed render leaves one with no video. An EASY version
    itself is never offered (it is never converted again). Read-only."""
    from .key_decision import key_state

    root = Path(work_root)
    songs: list[str] = []
    labels: dict[str, str] = {}
    waiting = 0
    for name in list_uploadable_songs(root):
        if Path(name).name == EASY_DIR:
            continue
        song_dir = root / name
        if easy_chord_build_problem(song_dir):
            facts = _song_facts(song_dir)
            if facts is not None and capo_and_shape_key(facts.key) is not None and key_state(song_dir) != "confirmed":
                waiting += 1
            continue
        variant = song_dir / EASY_DIR
        if song_video_path(variant) is not None:
            if not easy_variant_problem(variant):
                continue                            # it already has a current EASY CHORD version
            labels[name] = f"{name}   (EASY version out of date)"
        elif variant.is_dir():
            labels[name] = f"{name}   (EASY version has no video)"
        songs.append(name)
    return EasyChordBackfill(songs, labels, waiting)


def list_easy_chord_backfill_candidates(work_root: Path) -> list[str]:
    """Names of work_root's already-passing songs that can have an EASY CHORD (capo) version built and have no current
    one (easy_chord_backfill_listing) -- backs the "Generate EASY CHORD Versions" catch-up action (owner, 2026-09-23:
    "this idea will give me hundreds of new songs"; trigger 3 of 3 from the design spec)."""
    return easy_chord_backfill_listing(work_root).songs


def _passes_for_upload(song_dir: Path) -> bool:
    facts = _song_facts(song_dir)
    if facts is None:
        return False
    if facts.verified:
        return True                                 # "if i decide its a good video its a good video"
    if facts.concern and not is_gate_concern(facts.concern):
        return False
    report = check_saved_song(song_dir)
    return report is not None and report.passes


def _timing_concern_now(song_dir: Path, facts: _SongFacts) -> str:
    """The timing check's concern for this song at the current bar, computed READ-ONLY (timing_gate.timing_verdict): the
    lists never write a hold into lyrics_timed.json any more (issue #7 review -- they did, non-atomically, from the Tk thread
    and the 20-minute tick at once). `facts` must carry no other check's concern and not be owner-verified."""
    return timing_verdict(facts.concern, check_saved_song(song_dir))


def _held_reason(song_dir: Path) -> str:
    """The reason recorded in song_dir's held_before_video.json ("" when it has none or cannot be read)."""
    try:
        reason = json.loads((Path(song_dir) / HELD_MARKER).read_text(encoding="utf-8")).get("reason")
    except (OSError, ValueError, AttributeError):
        return ""
    return reason.strip() if isinstance(reason, str) else ""


def review_concern(song_dir: Path) -> str:
    """The reason to show for a song in Flagged for Lyrics Review (and the lyrics editor), as it stands at the CURRENT pass
    mark: another check's concern as stored, else the timing check's verdict now (a stored timing concern can be out of date
    once the bar moves, since the lists no longer rewrite it). A song held before its video keeps the reason it was held
    for when the bar alone would now let it through (it still needs Render Anyway). "" when there is nothing to say or
    the file cannot be read."""
    facts = _song_facts(song_dir)
    if facts is None:
        return ""
    stale = easy_variant_problem(Path(song_dir))     # "" for anything but an out-of-date EASY CHORD version
    if stale:
        return f"{EASY_STALE_PREFIX} {stale} Use Rebuild EASY version."
    if (facts.concern and not is_gate_concern(facts.concern)) or facts.verified:
        return facts.concern
    now = _timing_concern_now(Path(song_dir), facts)
    if now or not held_before_video(song_dir):
        return now
    if facts.concern:
        return facts.concern
    # Held with nothing in the song itself to say why (a Redo that stopped before its new video, a key corrected by
    # key_rollout): the hold's own reason -- else the Flagged row showed a blank reason. A song waiting for its key gets
    # that key's reason from the GUI instead (Set Key), so an older key-hold reason is not repeated here.
    return "" if key_needs_attention(song_dir) else _held_reason(song_dir)


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
    return _songs_where(
        work_root,
        lambda path: not (path / STATE_FILENAME).exists() and song_video_path(path) is not None and not _held_for_review(path),
    )


def needs_review(song_dir: Path) -> bool:
    """True when the song is held back from upload (a concern, or timing that fails the pass mark) and the owner has not
    approved this version -- the songs Flagged for Lyrics Review lists."""
    return _held_for_review(Path(song_dir))


def _held_for_review(song_dir: Path) -> bool:
    """A song with any lyrics/timing concern is not CLEARED (cleared_log.py): it is offered in Flagged for Lyrics
    Review, never as a pending-upload checkbox that Select All + Upload Selected could send out. Read-only: the timing
    check's verdict at the current bar is computed, never written (see _timing_concern_now). A song whose lyrics_timed.json
    cannot be read counts as held (fail closed: it must never be offered for upload)."""
    song_dir = Path(song_dir)
    timed_path = song_dir / "lyrics_timed.json"
    if not timed_path.exists():
        return False
    if key_needs_attention(song_dir):
        return True                                 # its key is unchecked or waiting for the owner: never offered for upload
    facts = _song_facts(song_dir)
    if facts is None:
        return True
    if easy_variant_problem(song_dir):
        return True                                 # an EASY CHORD version its song has outgrown: Rebuild EASY version
    if facts.verified:
        return False                                # the owner watched this version and approved it
    if facts.concern and not is_gate_concern(facts.concern):
        return True
    return bool(_timing_concern_now(song_dir, facts))   # the timing hold follows the bar (holds, or releases)


def easy_version_waits_on_key(song_dir: Path) -> bool:
    """True for a song already on YouTube whose EASY CHORD version (`<song>/easychords`, not uploaded yet) is held because
    this song's key is unchecked or waiting for the owner -- the version reads its song's key, so only a Set Key on the
    SONG releases it. Issue #7 review: an uploaded song is never flagged for its own key, so it had no Set Key row and its
    EASY version could never upload; list_flagged_songs now lists the song for it."""
    song_dir = Path(song_dir)
    variant = song_dir / "easychords"
    return (song_dir / STATE_FILENAME).exists() and variant.is_dir() and key_needs_attention(variant)


def list_flagged_songs(work_root: Path, include_uploaded: bool = False) -> list[str]:
    """Names (or "<song>/easychords" slugs) of work_root's songs whose lyrics were never confirmed
    accurate by check_lyric_accuracy() across every source tried (Song.lyrics_accuracy_concern non-empty)
    and that haven't been uploaded yet -- backs the GUI's "Flagged for Lyrics Review" list. Same
    not-yet-uploaded convention as list_pending_uploads() above, so a song naturally drops off this list
    once the owner uploads it anyway (via the review panel's own Upload Anyway) without needing a separate
    "dismiss" action -- and once a later Redo's fresh fetch clears the concern, it drops off too. The one uploaded
    song always listed is one whose EASY CHORD version waits for its key (easy_version_waits_on_key)."""
    if not work_root.exists():
        return []

    def is_flagged(entry: Path) -> bool:
        uploaded = (entry / STATE_FILENAME).exists()
        if uploaded and easy_version_waits_on_key(entry):
            return True                               # its EASY CHORD version waits for THIS song's key: Set Key here
        if not include_uploaded and uploaded:
            return False
        facts = _song_facts(entry)
        if facts is None:
            return False                              # no lyrics_timed.json, or an unreadable one (warned once)
        if held_before_video(entry):
            return True                               # no video yet: Edit Lyrics / Redo / Render Anyway (whatever the mark is now)
        if _video_of(entry, facts) is None:
            return False
        if not uploaded and easy_variant_problem(entry):
            return True                               # an out-of-date EASY CHORD version: Rebuild EASY version
        if key_needs_attention(entry):
            return True                               # a video whose key is unchecked or waiting for the owner: Set Key
        if facts.verified:
            return False                              # approved by the owner: not up for review any more
        if facts.concern and not is_gate_concern(facts.concern):
            return True
        return bool(_timing_concern_now(entry, facts))

    return _songs_where(work_root, is_flagged)


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
    song = load_song(work_dir / "lyrics_timed.json")
    heard = corrected_heard_words(work_dir, song=song)  # song is already loaded here -- never parsed a second time

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
    name. Returns the new path, or None if there was no images_dir to move.

    The moved pictures are recorded right away as rejected for this song in the shared image library's list
    (library_session.rejected_image_ids), not only the next time a library session opens for it -- deleting
    images_prior_* before ever turning the library on would otherwise lose the owner's "not these" for good."""
    if not images_dir.exists():
        return None

    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    moved_to = images_dir.parent / f"images_prior_{timestamp}"
    images_dir.rename(moved_to)
    try:
        from .library_session import rejected_image_ids

        rejected_image_ids(images_dir.parent)
    except Exception as e:          # a bookkeeping problem must never stop the Redo
        print(f"WARNING: could not record the replaced pictures ({type(e).__name__}: {e})", file=sys.stderr)
    return moved_to


def _build_audio_check(vocals_path: Path, work_dir: Path):
    """A function scoring candidate lyric lines against what Whisper hears in the
    vocal stem (lyric_audio_match.py), or None when the transcription can't run --
    then the lyrics fetch quietly falls back to the older text-only check, so a
    missing package or blocked model download never stops a song from being made.
    Deliberately UNHINTED (owner + review, 2026-09-27): this is the independent check deciding whether a fetched
    candidate matches what is sung, so it must never be hinted with the very candidate it is judging -- doing so
    would make a wrong/mismatched candidate more likely to falsely pass (see lyric_hotwords() for where a hint
    IS used, on a separate re-transcription made only after a candidate is accepted)."""
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
        # NOT result.report.concern: Settled.concern can differ from its own report's (e.g. an earlier concern that
        # survives even though the sync gate itself accepts the alignment) -- start from exactly what settle_alignment
        # decided, and only override it below when a correction genuinely improves the picture.
        concern, share, judged_lines = result.concern, result.report.share, result.report.judged_lines
        # A saved Whisper correction (owner_whisper.py, 2026-09-27) re-scores the WINNING candidate's own final
        # times -- never influences which candidate wins, only whether an already-placed, correctly-timed line is
        # excused from a mismatch that was really Whisper's own mishearing. Real incident: without this, a Redo
        # re-ran this same function from scratch and reproduced the identical "out of sync" concern even after the
        # owner saved a correction, since only a READ-ONLY re-judge of an already-rendered song ever consulted one.
        corrected_sung = add_corrections(work_dir, sung, line_words, result.times)
        if len(corrected_sung) != len(sung):  # a still-valid correction actually added something
            corrected_report = check_sync(line_words, result.times, corrected_sung, needed)
            if corrected_report.share is not None and (share is None or corrected_report.share >= share):
                concern, share, judged_lines = corrected_report.concern, corrected_report.share, corrected_report.judged_lines
        if share is None:
            print("Sync check: too little of the singing was recognized to compare the lines with.")
        else:
            print(
                f"Sync check: {share:.0%} of {judged_lines} lines start within half a second "
                f"of where they are sung. Using the {result.method} alignment."
            )
        if concern:
            print(f"WARNING: {concern}", file=sys.stderr)
        # share (owner, 2026-09-23: "i want to know how close it is when actually creating a video") -- the
        # real achieved sync percentage, not just pass/fail; None when too little was heard to judge at all.
        return [(min(a, audio_duration), min(b, audio_duration)) for a, b in result.times], concern, share

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


def _retire_previous_video(final_path: Path) -> bool:
    """Moves an earlier run's video aside to `<name>.previous.mp4` (a redo backup of it already exists), so it is never
    mistaken for -- or uploaded as -- the video of timing it was not made from. True when there was one."""
    if not final_path.exists():
        return False
    final_path.replace(final_path.with_name(final_path.stem + ".previous.mp4"))
    return True


def _write_held_marker(work_dir: Path, reason: str) -> None:
    (Path(work_dir) / HELD_MARKER).write_text(
        json.dumps({"reason": reason, "at": datetime.now().astimezone().isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )


def _hold_before_video(work_dir: Path, final_path: Path, song: Song, concern: str) -> None:
    """Stops the run here. A video left from before a Redo is moved aside (a redo backup of it already exists) so the old
    video is never mistaken for the new timing's."""
    _retire_previous_video(final_path)
    _write_held_marker(work_dir, concern)
    _record_finished(work_dir, song)
    raise HeldBeforeVideo(concern)


def _hold_for_key(work_dir: Path, final_path: Path, concern: str) -> None:
    """Stops the run at the key check (owner, 2026-09-26: the key in the video and the description must be the song's real
    one). Held BEFORE the images (no image bill) and the video. Unlike a timing hold this is NOT recorded as finished/cleared:
    the song is simply waiting for the owner's key. The video of an earlier run is moved aside, as for a timing hold."""
    _retire_previous_video(final_path)
    _write_held_marker(work_dir, concern)
    raise HeldBeforeVideo(concern)


def _make_thumbnail_after_render(work_dir: Path, settings) -> None:
    """The video's custom thumbnail (thumbnail.py), made once beside it (a Redo or re-render keeps the one it has) when
    Settings.generate_thumbnails is on and the keys are there. Never raises: a missing thumbnail is made at upload."""
    if settings is None or not getattr(settings, "generate_thumbnails", True):
        return
    try:
        from .thumbnail_job import ensure_thumbnail
        path = ensure_thumbnail(
            work_dir, anthropic.Anthropic(), os.environ.get("REPLICATE_API_TOKEN", ""),
            font_path=getattr(settings, "font_path", None) or None,
            show_chords=getattr(settings, "thumbnail_show_chords", True),
        )
        if path is not None:
            print(f"Thumbnail ready: {path.name}")
    except Exception as e:
        print(f"WARNING: no thumbnail made ({type(e).__name__}: {e}); one is made when the video is uploaded.")


def _key_client():
    """A Claude client for the key's second opinion; None when there is no API key (the key then waits for the owner)."""
    try:
        return anthropic.Anthropic()
    except Exception:
        return None


def _vocal_stem_seconds(vocals_path: Path) -> float:
    """The vocal stem's length in seconds, for the align stage. Read from the WAV header (soundfile, as separate.py already
    does for these stems) instead of decoding the whole stem: the align stage used to decode it as float32 only for its
    length and then kept that ~85 MB waveform alive through chords, images and the render. torchaudio (imported only here,
    never at module import) is the fallback for a file soundfile cannot read; its waveform is freed on return."""
    try:
        import soundfile

        return float(soundfile.info(str(vocals_path)).duration)
    except Exception:
        import torchaudio

        waveform, sample_rate = torchaudio.load(str(vocals_path))
        return waveform.shape[1] / sample_rate


def _song_artist(info_path: Path) -> str:
    try:
        return str(json.loads(info_path.read_text(encoding="utf-8")).get("artist") or "")
    except (OSError, ValueError):
        return ""


def resolve_font(chosen: str | None = None) -> str:
    """The font a video is drawn with: `chosen` (the CLI's --font, else Settings.font_path) when it names a file that
    exists, else default_font(). A chosen font that has since been moved or deleted is reported and replaced by the
    default -- never a failed render. Issue #7 review: the Settings Font choice reached the live preview but no real video;
    the preview and run_pipeline now both resolve it here, so they cannot disagree."""
    if chosen:
        if Path(chosen).is_file():
            return str(chosen)
        print(f"WARNING: the chosen font {chosen} was not found; using the default font instead.", file=sys.stderr)
    return default_font()


def _saved_title(info_path: Path) -> str | None:
    """song_info.json's title, or None when the file is missing, unreadable or has no usable title -- a resume then
    identifies the song again instead of dying on a KeyError (or naming the video "untitled-song")."""
    try:
        value = json.loads(info_path.read_text(encoding="utf-8")).get("title")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def _audio_copy_is_stale(source: Path, copy: Path) -> bool:
    """True when the owner has replaced the source audio since it was copied into the song's folder (issue #7 review: a
    damaged download the app told the owner to replace kept being separated from the old copy forever). copy2 keeps the
    time stamp, so the very file copied before matches on size and time; a new time stamp with the same size is compared
    byte for byte, so merely touching or re-copying the same file never costs a Demucs run. The copy itself (a Redo reads
    from it) is never stale."""
    import filecmp

    try:
        if os.path.samefile(source, copy):
            return False
        source_stat, copy_stat = source.stat(), copy.stat()
    except OSError:
        return False
    if source_stat.st_size != copy_stat.st_size:
        return True
    if abs(source_stat.st_mtime - copy_stat.st_mtime) < 2.0:      # 2 s: FAT/network shares keep coarse time stamps
        return False
    try:
        return not filecmp.cmp(source, copy, shallow=False)
    except OSError:
        return False


def _copy_audio_atomically(source: Path, copy: Path) -> None:
    """copy2 (time stamp kept, see _audio_copy_is_stale) through a temporary name, so a kill mid-copy never leaves a half
    file under the copy's real name."""
    partial = copy.with_name(copy.name + ".copying")
    shutil.copy2(source, partial)
    os.replace(partial, copy)


def _replace_audio_copy(source: Path, copy: Path, work_dir: Path) -> None:
    """Refreshes the song's audio copy from a replaced source, and forgets everything made from the old audio: its Demucs
    stems (re-separated by the resume self-heal) and Whisper transcript (keyed only on the stem's byte size, which a same-
    length replacement would match)."""
    _copy_audio_atomically(source, copy)
    shutil.rmtree(work_dir / "htdemucs" / copy.stem, ignore_errors=True)
    (work_dir / "transcript.json").unlink(missing_ok=True)       # transcribe.py's cache file


def _length_may_be_a_header_guess(audio_path: Path) -> bool:
    """True for an MP3 with no Xing/VBRI/LAME header (mutagen BitrateMode.UNKNOWN) -- the only kind whose recorded length
    identify used to take from a first-frame bitrate guess (identify._length_is_a_header_guess's rule). False for every
    other file, and for one mutagen cannot read."""
    try:
        from mutagen import File as MutagenFile  # type: ignore
        from mutagen.mp3 import BitrateMode, MPEGInfo  # type: ignore

        audio = MutagenFile(str(audio_path))
    except Exception:
        return False
    info = getattr(audio, "info", None)
    return isinstance(info, MPEGInfo) and getattr(info, "bitrate_mode", None) == BitrateMode.UNKNOWN


def _stems_are_complete(
    vocals_path: Path, instrumental_path: Path, audio_path: Path, info_path: Path, expected_seconds: float | None,
) -> bool:
    """separate.stems_look_complete, with one backstop (issue #7 review, VBR MP3s): a song identified before identify
    measured a header-less VBR MP3 by decoding it can have a song_info.json duration minutes off, so its complete stems
    looked truncated -- and a Batch resume or a Redo never re-identifies. When readable stems disagree with song_info.json,
    the audio's decoded length decides: if the stems match THAT (same tolerance), they are accepted and song_info.json's
    duration is corrected. Only for an MP3 whose header length IS a guess (_length_may_be_a_header_guess): an incomplete
    download keeps its full-length header while decoding to only the part that arrived, so there the decoded length
    agrees with its truncated stems -- accepting them would bring back the cut-short 'Ironic' video this check exists
    to stop."""
    if stems_look_complete(vocals_path, instrumental_path, expected_seconds):
        return True
    if not expected_seconds or not stems_look_complete(vocals_path, instrumental_path, None):
        return False                                    # stems missing or unreadable: nothing to reconsider
    if not _length_may_be_a_header_guess(Path(audio_path)):
        return False                                    # the recorded length is the file's own: the stems are short
    from .identify import decoded_duration

    real_seconds = decoded_duration(Path(audio_path))
    if not real_seconds or not stems_look_complete(vocals_path, instrumental_path, real_seconds):
        return False
    print(f"The audio really lasts {real_seconds:.0f} s, not the {expected_seconds:.0f} s song_info.json recorded "
          "(an MP3 whose length was misread); its stems are complete, and song_info.json is corrected.")
    try:
        from .models import atomic_write_text

        info = json.loads(info_path.read_text(encoding="utf-8"))
        info["duration"] = real_seconds
        atomic_write_text(info_path, json.dumps(info))
    except (OSError, ValueError, TypeError) as e:
        print(f"WARNING: could not correct {info_path.name} ({type(e).__name__}: {e})", file=sys.stderr)
    return True


def _build_unsung_trim(vocals_path: Path, work_dir: Path):
    """(lines, times) -> (lines, times, dropped lines): drops a lyric candidate's leading and trailing lines that are not
    part of the audio -- provider credits stamped before the first or after the last singing ("if its not part of the
    audio, its a credit"), judged from the source's own line times, what Whisper heard and the vocal loudness. Handed to the
    lyrics fetch so each candidate is trimmed BEFORE it is scored against the audio (issue #7 review L01: credits were
    scored as unsung lyric lines, failing correct lyrics, and the concern named line numbers deleted a moment later).
    Lines without their own times, or without audio evidence (the loudness cannot be read), come back unchanged."""
    evidence: dict = {}

    def trim(lines, times):
        if not times or len(times) != len(lines):
            return lines, times, []
        try:
            if not evidence:        # read once per song, on the first candidate that has its own line times
                heard = [HeardWord(w["word"], w["start"], w["end"]) for w in load_transcript_words(work_dir)]
                evidence.update(heard=heard, loudness=vocal_loudness(vocals_path))
            lines_kept, times_kept, leading = drop_unsung_leading_lines(
                lines, times, evidence["heard"], evidence["loudness"], hop=0.5,
            )
            lines_kept, times_kept, trailing = drop_unsung_trailing_lines(
                lines_kept, times_kept, evidence["heard"], evidence["loudness"], hop=0.5,
            )
        except Exception:
            return lines, times, []                     # no audio evidence: keep every line, as before
        return lines_kept, times_kept, leading + trailing

    return trim


def _accepts_keyword(function, name: str) -> bool:
    """Whether `function` takes a `name` keyword -- lets this module hand the lyrics fetch its credit trim as soon as
    fetch_lyric_lines_verified accepts one, without breaking while it does not."""
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters


def _concern_after_trim(audio_check, lines_before: list[str], lines_after: list[str], concern: str) -> str:
    """The audio check's concern for the lines actually kept, when credit lines were dropped only AFTER the lyrics fetch
    scored its candidate (a fetch that cannot trim candidates itself): the kept lines are judged again, so a song failed
    only by its credits passes, and the concern's line numbers are the saved lines' own. Any further text the fetch added
    after its own mismatch sentence (the AI judgement, a suggested fix) is kept."""
    from .lyric_audio_match import audio_match_passes, describe_mismatch

    after = audio_check(lines_after)
    if audio_match_passes(after):
        return ""
    before_text = describe_mismatch(audio_check(lines_before))
    rest = concern[len(before_text):] if concern.startswith(before_text) else ""
    return (describe_mismatch(after) + rest).strip()


def _usable_picture(path: Path) -> bool:
    """A complete picture file that is not the plain-colour placeholder -- what get_or_generate_image takes as a cache hit.
    verify() reads every chunk (a file cut short fails) without decoding the pixels."""
    if not path.is_file():
        return False
    try:
        from PIL import Image

        with Image.open(path) as picture:
            picture.verify()
    except Exception:
        return False
    return not is_fallback_image(path)


def _missing_images(texts: list[str], images_dir: Path, backup_dirs: list[Path]) -> list[str]:
    """The lines/captions (first-seen order, no repeats) with no usable picture yet in images/ or any images_backup_*/
    archive. Issue #7 review: the images stage used to build a Claude client, demand the Replicate token and pay for the
    song-gist call on every Redo, Batch regenerate and Render Anyway, even when every picture already existed."""
    from .models import line_hash

    missing = []
    for text in dict.fromkeys(texts):
        name = f"{line_hash(text)}.png"
        if not any(_usable_picture(folder / name) for folder in (images_dir, *backup_dirs)):
            missing.append(text)
    return missing


REDO_STOPPED_REASON = (
    "This song's lyrics and timing were made again, but the run stopped before its new video was made (the app was "
    "closed, or a later step failed). The old video was set aside as .previous.mp4. Use Render Anyway to make the video "
    "from the new timing, or Redo the song."
)


def _carry_respelled_chord_pictures(images_dir: Path, song: Song, source_dirs: list[Path] = ()) -> None:
    """Gives each instrumental chord caption a picture already bought under the chord's other spelling (a chord respelled
    since, by the key check, Set Key or key_rollout: "A#" is now "Bb"), copied locally -- nothing is bought again, and a
    render-only resume shows that chord's own picture instead of an unrelated one (layout.fill_missing_instrumental_images).
    Never raises: a caption left without a picture is bought by the images stage, or covered by the render's fallback."""
    from .layout import fill_missing_instrumental_images

    try:
        filled = fill_missing_instrumental_images(
            images_dir, song.lines, song.chord_track, song_end_time(song), source_dirs=list(source_dirs),
        )
    except Exception as e:
        print(f"WARNING: could not reuse the pictures of respelled chords ({type(e).__name__}: {e}).", file=sys.stderr)
        return
    if filled:
        print(f"Reused {len(filled)} chord picture(s) bought under the chord's other spelling.")


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
    fresh_images: bool = False,
) -> Path | None:
    """`end_stage` (owner, 2026-09-22: vetting a candidate song's real timing-gate share -- deep_review-style,
    or the most-popular-songs picker -- must never reach detect_chords/images/render, which cost real
    Replicate/Claude money this kind of check has no business spending) stops the run right after the named
    stage; every later stage, including detect_chords, is never entered. Returns None instead of the (nonexistent)
    video path whenever the run stops before render actually happens; the default ("render") reproduces every
    existing caller's behavior exactly, always returning the finished mp4's path.

    `capo` (owner, 2026-09-23) is passed straight to assemble_video() -- None (the default) draws no badge at
    all; an EASY CHORD variant's own build passes its real capo fret so the CAPO N badge appears only there.

    `fresh_images` (owner, 2026-09-25) is Redo's "Generate new images": the shared image library is not consulted
    for this run (it would hand back the very pictures being replaced), though every picture bought is still
    filed into it.

    An EASY CHORD version's folder (`<song>/easychords`) is only ever rendered from its song's saved lyrics, timing and
    capo-shifted chords (build_capo_variant): a start at detect_chords or earlier is refused, and it never gets the key
    check, the owner's key or an EASY version of its own (issue #7 review: run through the pipeline, it lost its CAPO
    badge and labelled shape chords with the original key)."""
    start_idx = STAGES.index(start_stage)
    end_idx = STAGES.index(end_stage)
    easy_variant = Path(work_dir).name == "easychords"
    if easy_variant and start_idx <= STAGES.index("detect_chords"):
        raise RuntimeError(
            f"{work_dir} is an EASY CHORD version: it is made from its song's own lyrics, timing and chords, never run "
            f"through the pipeline from the '{start_stage}' stage. Run that on the song itself "
            f"({Path(work_dir).parent.name}), then rebuild its EASY version."
        )
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
    # The font: the caller's (the CLI's --font) first, else the owner's Settings choice (resolved at render time by
    # resolve_font, which falls back to the default font when the chosen file is gone). render_kwargs may carry it too.
    kwargs_font = assemble_kwargs.pop("font_path", None)        # always popped: it is passed to assemble_video once, below
    chosen_font = font_path or kwargs_font or getattr(settings, "font_path", None)
    if easy_variant:
        # Its render settings are unpacked above; nothing else from Settings applies to an EASY CHORD version -- its
        # pictures are its song's (no image-library lookup) and it never gets an EASY version of itself.
        settings = None
        if capo is None:
            capo = (load_easy_chord_capo_marker(work_dir) or {}).get("capo_fret")

    def report(stage: str) -> None:
        if progress_callback is not None:
            progress_callback(stage)

    work_dir.mkdir(parents=True, exist_ok=True)
    # A local copy of the source audio, kept for load_redo_inputs() -- a batch
    # song's original audio_path points into a staging folder the owner
    # empties before the next batch, so it can be gone by the time a later
    # Redo needs it. Skipped once the copy already exists (a resume past an
    # earlier stage, or a Redo that's already reading from this same copy) --
    # unless the owner has since REPLACED the source file (a damaged download
    # swapped for a good one, as the truncated-stems error tells them to): then
    # the copy is refreshed and its old stems/transcript are forgotten. A resume
    # past align keeps the audio its saved timing was made from.
    audio_copy_path = work_dir / Path(audio_path).name
    if Path(audio_path).exists() and not audio_copy_path.exists():
        _copy_audio_atomically(Path(audio_path), audio_copy_path)
    elif Path(audio_path).exists() and _audio_copy_is_stale(Path(audio_path), audio_copy_path):
        if start_idx <= STAGES.index("align"):
            print(f"{Path(audio_path).name} has changed since this song was last made; using the new file (its vocals "
                  "are separated again).")
            _replace_audio_copy(Path(audio_path), audio_copy_path, work_dir)
        else:
            print(f"NOTE: {Path(audio_path).name} has changed since this song's timing was made; this run keeps the "
                  "audio that timing was made from. Redo the song to use the new file.")
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
        data = {"title": resolved, "artist": info.artist, "duration": info.duration, "alt_titles": info.alt_titles}
        artists = getattr(info, "artists", None)
        if artists:
            # MusicBrainz's individually credited names (identify.py): the artist playlists use them instead of splitting
            # the credit string, which cannot tell a band with a comma in its name from two artists.
            data["artists"] = list(artists)
        from .models import atomic_write_text

        atomic_write_text(info_path, json.dumps(data))      # read from other threads (the song lists); never half-written
        return resolved

    if start_idx <= STAGES.index("identify"):
        report("identify")
        resolved_title = run_identify()
    elif (saved_title := _saved_title(info_path)) is not None:
        resolved_title = saved_title
    else:
        # A redo of a song created before this stage existed -- no song_info.json
        # was ever written for it -- or one whose song_info.json is damaged or has
        # no title. Bootstrap one now via a fresh identify call
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

    def stems_complete() -> bool:
        return _stems_are_complete(vocals_path, instrumental_stem_path, audio_path, info_path, song_seconds())

    def separate() -> Path:
        from .separate import SeparationError

        try:
            return separate_vocals(audio_path, work_dir, expected_seconds=song_seconds())
        except SeparationError:
            if stems_complete():            # only song_info.json's length was wrong (see _stems_are_complete)
                return vocals_path
            raise

    if start_idx <= STAGES.index("separate") <= end_idx:
        report("separate")
        vocals_path = separate()
    elif start_idx <= STAGES.index("detect_chords") and end_idx >= STAGES.index("align") and not stems_complete():
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
        vocals_path = separate()

    if start_idx <= STAGES.index("fetch_lyrics") <= end_idx:
        report("fetch_lyrics")
        info_data = json.loads(info_path.read_text(encoding="utf-8"))
        owner_lines = owner_lyrics_lines(work_dir)
        trim_unsung = _build_unsung_trim(vocals_path, work_dir)
        trim_kwargs: dict = {}
        audio_check = None
        if owner_lines is not None:
            # The owner edited these lyrics by hand (Flagged for Lyrics Review > Edit Lyrics): their word is final,
            # so no online source, AI or audio check may override them. Whisper's word timings are still needed by
            # the aligner as anchors (cached), and the aligner and sync check still time them.
            print("Using the lyrics you edited (lyrics_owner.txt); no online lookup or AI check for these.")
            try:
                transcribe_vocals(vocals_path, work_dir, hotwords=lyric_hotwords(owner_lines))
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
            # Each candidate's credit lines are dropped BEFORE it is scored against the audio (L01), once the fetch
            # takes the trim; until then they are dropped below and the kept lines judged again (_concern_after_trim).
            trim_kwargs = {"trim": trim_unsung} if _accepts_keyword(fetch_lyric_lines_verified, "trim") else {}
            lines_text, lyrics_source, lyrics_concern = fetch_lyric_lines_verified(
                audio_path, info_data["title"], info_data["artist"], info_data["duration"],
                info_data.get("alt_titles"), lyrics_anthropic_client, audio_check=audio_check, reconcile=reconcile,
                arbiter=arbiter, times_out=times_out, **trim_kwargs,
            )
        if owner_lines is None and not trim_kwargs and times_out.get("line_times"):
            # Credits arrive as fake lyric lines stamped before the first or after the last singing; "if its not part
            # of the audio, its a credit". Only for a fetch that could not trim its candidates itself: trimming the
            # fetch's own result again could drop a further line after its concern numbered the lines.
            lines_before = lines_text
            lines_text, times_out["line_times"], dropped_lines = trim_unsung(lines_text, times_out["line_times"])
            if dropped_lines:
                print("Removed lines that are not part of the song's audio (credits): " + "; ".join(dropped_lines))
                if audio_check is not None and lyrics_concern:
                    lyrics_concern = _concern_after_trim(audio_check, lines_before, lines_text, lyrics_concern)
        if owner_lines is None and lines_text:
            # The accepted candidate's own text hints a SECOND, separate transcription for the aligner's anchors
            # only (owner + review, 2026-09-27) -- audio_check above stayed unhinted throughout, so the
            # independent lyrics-vs-audio check was never primed with the very text it judged. This is what the
            # align stage's anchors (load_transcript_words) read; it overwrites the unhinted transcript.json
            # written above via _build_audio_check, at the cost of one more ~70 s Whisper call for a
            # fetched-lyrics song (the owner-edited branch above needs only its own one call).
            try:
                transcribe_vocals(vocals_path, work_dir, hotwords=lyric_hotwords(lines_text))
            except Exception as e:
                print(f"WARNING: could not re-listen with hints ({type(e).__name__}: {e}); the aligner will use "
                      "the unhinted transcript.", file=sys.stderr)
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
        audio_duration = _vocal_stem_seconds(vocals_path)
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
        # The new timing replaces the old one right here, so a video made from the old timing must not stay the song's
        # current video: if this run stopped before its render (the app closed mid-render, an OOM kill, a failed image or
        # ffmpeg step), every list and upload check would judge the OLD video by the NEW timing (issue #7 review). It is
        # set aside and the song is held (Flagged: Render Anyway / Redo) until a render replaces it; the render removes
        # the hold, and a timing or key hold below writes its own reason over this one.
        if _retire_previous_video(final_path):
            _write_held_marker(work_dir, REDO_STOPPED_REASON)
        save_song(song, timed_path)
        if timing_concern and is_gate_concern(timing_concern):
            _hold_before_video(work_dir, final_path, song, timing_concern)          # raises HeldBeforeVideo
    elif start_idx > STAGES.index("align"):
        song = load_song(timed_path)
    # else: end_stage stops before align even starts (e.g. a vetting-only "identify"/"separate"/"fetch_lyrics"
    # run) -- `song` is intentionally left unset here; every block below that would use it is itself gated by
    # end_idx and the function returns before any of them can run.

    # Settings.prefer_flats ("Use flats in flat keys") also spells the chords the key check respells (key_decision.py).
    prefer_flats = settings.prefer_flats if settings is not None else True
    chords_idx = STAGES.index("detect_chords")
    detect_now = start_idx <= chords_idx <= end_idx
    if not detect_now and not easy_variant and start_idx > chords_idx and end_idx >= start_idx and not song.chord_track.events:
        # Resumed past the chords, but the saved song has none: a Redo re-times the song with no chords and saves it
        # before a timing hold stops it, while an earlier run's key_decision.json stays on disk -- so Render Anyway / Set
        # Key resumed at images and made a video with no chord bar, no legend, a blank Key badge and a 120 BPM count-in
        # (issue #7 review). The chords (and the key check) come first, whatever stage was asked for.
        print("The saved song has no chords yet (it was held before they were detected); detecting them now.")
        if not stems_complete():
            report("separate")
            vocals_path = separate()
        detect_now = True
    if detect_now:
        report("detect_chords")
        song.chord_track = detect_chords(instrumental_stem_path, **detect_kwargs)
        # The key comes from the chords AND a second opinion that must agree, else the owner's own answer (key_decision.py);
        # a song whose key is in doubt stops here, before the images are bought and the video is made.
        key_decision, song.chord_track = settle_song_key(
            work_dir, song.chord_track, song.title, _song_artist(info_path), _key_client(), prefer_flats=prefer_flats,
        )
        save_song(song, timed_path)
        if not key_decision.confirmed:
            _hold_for_key(work_dir, final_path, key_decision.concern())
    elif start_idx > chords_idx and end_idx >= start_idx and not easy_variant:
        # Resumed past the chords (Set Key -> make the video, or a render-only run): the owner's key, if any, goes onto the
        # saved chords; a song still waiting for its key stays held. A song with no key decision on file (made before the
        # key check existed) is left exactly as it is. (An EASY CHORD version's chords are its song's, shifted by the
        # capo: they never take a key of their own.)
        # apply_saved_owner_key also records the owner's key as the CONFIRMED decision (key_decision.confirm_owner_key),
        # whether or not the chords needed respelling for it.
        if apply_saved_owner_key(work_dir, song, prefer_flats=prefer_flats):
            save_song(song, timed_path)
        saved = load_decision(work_dir)
        if saved is not None and not saved.confirmed and load_owner_key(work_dir) is None and not (saved.research_key or saved.research_notes):
            # A song held for its key before web research existed (key_research.py): look the key up now instead of
            # leaving it for Set Key. A song research already looked at is not searched again (no repeat spend).
            saved, song.chord_track = settle_song_key(
                work_dir, song.chord_track, song.title, _song_artist(info_path), _key_client(), prefer_flats=prefer_flats,
            )
            save_song(song, timed_path)
        if saved is not None and not saved.confirmed:
            _hold_for_key(work_dir, final_path, saved.concern())

    if start_idx <= STAGES.index("images") <= end_idx:
        report("images")
        # Reuse already-paid-for images from any prior images_backup_*/ archive
        # before spending on a new one (unchanged convention).
        backup_dirs = sorted(work_dir.glob("images_backup_*"))
        _carry_respelled_chord_pictures(images_dir, song, backup_dirs)
        captions = instrumental_image_captions(song.lines, song.chord_track, song_end_time(song))
        missing = _missing_images([line.text for line in song.lines] + captions, images_dir, backup_dirs)
        images_dir.mkdir(exist_ok=True)
        library = None
        if missing:
            anthropic_client = anthropic.Anthropic()
            replicate_token = os.environ.get("REPLICATE_API_TOKEN", "")
            if not replicate_token:
                raise RuntimeError(
                    "REPLICATE_API_TOKEN is not set -- add it to the .env file at the repo root "
                    "(see .env.example). The images stage can't generate backgrounds without it."
                )
            full_lyrics = "\n".join(l.text for l in song.lines)
            song_gist = summarize_song_gist(anthropic_client, full_lyrics)
            # The shared library of already-bought images (Settings.use_image_library; None whenever it is off or
            # unavailable, in which case every line below behaves exactly as before). Not wrapped in try/finally: on
            # an error the session is simply dropped and freed with the frame, like every other per-run object.
            library = open_library_session(
                settings, song_slug=work_dir.name, song_title=song.title, images_dir=images_dir,
                fresh_images=fresh_images,
            )
        else:
            # Every picture already exists (a Redo, Batch regenerate or Render Anyway that reuses its images): no Claude
            # client, no Replicate token and no song-gist call are needed -- each lookup below is a cache hit.
            print("Every background image for this song already exists; none are bought.")
            anthropic_client, replicate_token, song_gist = None, "", ""
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
                extra_cache_dirs=backup_dirs, previous_image=last_real_image, library=library,
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
        for caption in captions:
            path = get_or_generate_image(
                anthropic_client, replicate_token, song_gist, caption, images_dir,
                extra_cache_dirs=backup_dirs, previous_image=last_real_image, library=library,
            )
            image_paths.append(path)
            if not is_fallback_image(path):
                last_real_image = path
        # A flat placeholder color would visibly break the finished video even
        # though a generation failure never crashes the pipeline -- substitute
        # a real neighboring image in for any fallback, as an absolute last
        # resort only after every real generation attempt has already failed.
        substitute_fallback_images(image_paths)
        if library is not None:
            print(library.summary_line())
            library.close()
        if image_paths and all(is_fallback_image(path) for path in image_paths):
            # Nothing real to substitute: the video would be flat colour from start to end (and could auto-upload).
            raise RuntimeError(
                "Every background image failed to generate (check REPLICATE_API_TOKEN and the Replicate account's "
                "credit), so the video was not rendered. Run the song again once image generation works -- the "
                "plain-colour placeholders are generated again then."
            )

    if start_idx <= STAGES.index("render") <= end_idx:
        report("render")
        # A capo never changes the song's real key (owner, 2026-09-23): an EASY CHORD variant's stored key is
        # the shape key (the EASY playlists key off it), so the Key badge reads the original key from its marker.
        capo_marker = load_easy_chord_capo_marker(work_dir) if capo is not None else None
        if not easy_variant:        # an EASY CHORD version's pictures are carried over by build_capo_variant itself
            _carry_respelled_chord_pictures(images_dir, song)
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            resolve_font(chosen_font),
            chord_legend_labels=ordered_unique_chords(song.chord_track),
            capo=capo,
            key_label=capo_marker["original_key"] if capo_marker else None,
            **assemble_kwargs,
        )
        (work_dir / HELD_MARKER).unlink(missing_ok=True)        # the video exists now (Render Anyway, or a Redo that passes)

        # Owner, 2026-09-23: "have a easy chord setting, so if i have that checked it will convert to easy
        # chord?" -- "any video make." Every future Generate/Redo/Batch run that lands in a hard key also
        # gets its own EASY CHORD (capo) variant when this is on, rendered with the same Settings as this video; an
        # EASY version this song already has is remade, or set aside when it no longer matches the song (issue #7
        # review). Never raises: a problem there must never make this finished video look like it failed.
        _make_thumbnail_after_render(work_dir, settings)
        _update_easy_version_after_render(work_dir, song, settings, font_path)

    if end_idx < STAGES.index("render"):
        # Stopped early (a vetting-only call): no video was made, so there is no redo/cleared-log entry to
        # write and nothing done-worthy to report -- returning None (rather than a nonexistent path) is the
        # caller's own signal that this call never intended to produce a video.
        return None

    _record_finished(work_dir, song, share=timing_share)
    report("done")
    return final_path


def _set_aside_videos(folder: Path) -> list[str]:
    """Renames every finished video in `folder` to <name>.previous.mp4 (the convention a Redo hold and key_rollout use), so
    it is no longer that folder's current video: it leaves the Upload and Pending lists, nothing is deleted."""
    moved = []
    for video in sorted(Path(folder).glob("*.mp4")):
        if video.name.endswith(".previous.mp4") or ".truncated" in video.stem:
            continue                                # already set aside (here, or by scripts/find_truncated_videos.py)
        video.replace(video.with_name(video.stem + ".previous.mp4"))
        moved.append(video.name)
    return moved


def _hold_easy_version(variant_dir: Path, reason: str) -> None:
    """Marks an EASY CHORD version as waiting to be made again (held before its video, pipeline.HELD_MARKER): it shows in
    Flagged for Lyrics Review, where Rebuild EASY version remakes it; its render removes the mark."""
    from .models import atomic_write_text

    atomic_write_text(
        Path(variant_dir) / HELD_MARKER,
        json.dumps({"reason": reason, "at": datetime.now().astimezone().isoformat(timespec="seconds")}, indent=2),
    )


def _set_aside_easy_version(variant_dir: Path, problem: str, *, flag: bool) -> None:
    """An EASY CHORD version that no longer matches its song: its video is set aside so it can never be uploaded as it is,
    and (`flag`) it is held with the reason -- in Flagged for Lyrics Review, where Rebuild EASY version remakes it. Not
    flagged when no EASY version applies any more (the song's key is now easy)."""
    variant_dir = Path(variant_dir)
    moved = _set_aside_videos(variant_dir)
    slug = display_slug(variant_dir)
    if not flag:
        print(f"The EASY CHORD version {slug} was set aside: {problem}")
        return
    reason = f"{EASY_STALE_PREFIX} {problem} Use Rebuild EASY version."
    _hold_easy_version(variant_dir, reason)
    try:                                            # the reason the Flagged list shows for it
        easy = load_song(variant_dir / "lyrics_timed.json")
        easy.lyrics_accuracy_concern = reason
        save_song(easy, variant_dir / "lyrics_timed.json")
    except Exception as e:
        print(f"WARNING: could not note why {slug} was set aside ({type(e).__name__}: {e})", file=sys.stderr)
    print(f"The EASY CHORD version {slug} {'was set aside' if moved else 'is held'} until it is made again: {problem}")


def _update_easy_version_after_render(work_dir: Path, song: Song, settings: Settings | None, font_path: str | None) -> None:
    """After the song's own video is rendered: builds its EASY CHORD version when Settings.generate_easy_chord_versions is
    on (or Redo's Easy Chords box) and the key is hard -- with the SAME Settings, so it looks like this video -- and
    otherwise checks the EASY version the song already has: one that no longer matches the song (redone lyrics or timing,
    changed chords or key) is set aside and held for Rebuild EASY version instead of staying uploadable with the old
    content. Never raises."""
    work_dir = Path(work_dir)
    if work_dir.name == EASY_DIR:
        return
    variant_dir = work_dir / EASY_DIR
    try:
        if settings is not None and settings.generate_easy_chord_versions and not is_easy_key(song.chord_track.key or ""):
            try:
                if build_capo_variant(work_dir, settings=settings, font_path=font_path) is not None:
                    return
            except Exception as e:
                log.warning("Could not build the EASY CHORD (capo) variant for %s: %s", work_dir, e)
                print(f"WARNING: the EASY CHORD version of {display_slug(work_dir)} was NOT made: {type(e).__name__}: {e}",
                      file=sys.stderr)
        if not (variant_dir / "lyrics_timed.json").exists():
            return
        problem = easy_variant_problem(variant_dir)
        if problem:
            # Held for Rebuild EASY version -- unless the song's key is now easy: then no EASY version applies at all.
            _set_aside_easy_version(variant_dir, problem, flag=not is_easy_key(song.chord_track.key or ""))
    except Exception as e:
        log.warning("Could not check the EASY CHORD version of %s: %s", work_dir, e)


def _easy_version_unchanged(capo_work_dir: Path, capo_song: Song, marker: dict) -> bool:
    """True when the EASY CHORD version already in `capo_work_dir` was made from exactly this content (same capo, shapes and
    key, same chords, same lyric words and timing): its video, if any, is still right while a new render runs."""
    from .owner_verified import timing_fingerprint

    old_marker = load_easy_chord_capo_marker(capo_work_dir)
    if not isinstance(old_marker, dict) or any(old_marker.get(k) != v for k, v in marker.items()):
        return False
    try:
        old = load_song(capo_work_dir / "lyrics_timed.json")
    except Exception:
        return False
    same_chords = [(e.start, e.end, e.label) for e in old.chord_track.events] == [
        (e.start, e.end, e.label) for e in capo_song.chord_track.events
    ]
    return same_chords and timing_fingerprint(old) == timing_fingerprint(capo_song)


def _copy_with_times(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _mirror_song_images(images_dir: Path, capo_images_dir: Path) -> None:
    """Makes the EASY CHORD version's images/ an exact copy of its song's, EVERY build (issue #7 review: it was copied once
    and never again, so after a Redo with new pictures or changed lines the EASY video kept the old pictures, or showed an
    unrelated one for a changed line). Unchanged files (same size and modification time) are not copied again."""
    capo_images_dir.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, Path] = {}
    if images_dir.is_dir():
        for entry in images_dir.iterdir():
            if entry.is_file() and not entry.name.startswith("."):
                wanted[entry.name] = entry
    for entry in list(capo_images_dir.iterdir()):
        if entry.is_file() and entry.name not in wanted:
            entry.unlink()
    for name, source in wanted.items():
        target = capo_images_dir / name
        source_stat = source.stat()
        try:
            target_stat = target.stat()
        except OSError:
            target_stat = None
        if (target_stat is not None and target_stat.st_size == source_stat.st_size
                and target_stat.st_mtime_ns == source_stat.st_mtime_ns):
            continue
        _copy_with_times(source, target)


def _carry_owner_verification(song_dir: Path, song: Song, capo_work_dir: Path, capo_song: Song) -> None:
    """The owner's approval of the song (owner_verified.py) also covers its EASY CHORD version: same audio, lyrics and timing,
    only the chords are shifted. Without it a Mark-Verified song's EASY version was held for the timing check the owner had
    overridden. An EASY version the owner verified itself keeps that record when its song is not verified."""
    import hashlib

    from .models import atomic_write_text
    from .owner_verified import timing_fingerprint

    record = verification(song_dir, song=song)
    if record is None:
        return
    carried = dict(record)
    carried["timing_fingerprint"] = timing_fingerprint(capo_song)
    try:
        carried["fingerprint"] = hashlib.sha256((capo_work_dir / "lyrics_timed.json").read_bytes()).hexdigest()
    except OSError:
        carried.pop("fingerprint", None)
    carried["carried_from"] = display_slug(song_dir)
    atomic_write_text(capo_work_dir / OWNER_VERIFIED_FILE, json.dumps(carried, indent=2))


def build_capo_variant(
    work_dir: Path,
    audio_path_override: Path | str | None = None,
    settings: Settings | None = None,
    font_path: str | None = None,
) -> Path | None:
    """Builds and renders a `<work_dir>/easychords` work dir NESTED inside the original song's own folder
    (owner, 2026-09-23: "i dont need twice the folder" -- was a sibling `<slug>-capo` folder next to it
    until this point): the same song's lyrics, timing, and images, converted to easy open-chord shapes via
    a capo -- see docs/superpowers/specs/2026-09-23-capo-easy-chord-videos-design.md ("EASY CHORD Play
    Along videos"). No new AI/Replicate spend: images are copied from the original, never regenerated, and
    only the render stage runs. Still tracked/listed/uploadable as its own distinct song everywhere else
    (list_rendered_songs()/list_uploadable_songs()/list_pending_uploads() all look one level deeper for
    this "easychords" subfolder) -- models.display_slug() gives it a unique slug ("<song>/easychords")
    wherever a bare directory name (always literally "easychords") would otherwise collide across songs.

    Returns None (nothing built, the reason printed -- easy_chord_build_problem) when the song is not made yet, its key is
    not settled, the key is already easy, or its audio is gone. Every call remakes the version from the song AS IT IS NOW
    (issue #7 review): lyrics, timing, capo-shifted chords, the owner's verification of the song, and a fresh copy of the
    song's images, with each instrumental chord's picture carried over to its capo name (layout.
    fill_missing_instrumental_images -- the pictures are filed under the chord's name, and a capo renames every chord).
    When the content changed, the old EASY video is set aside first, so a render that does not finish never leaves the
    old video uploadable as if it were the new one.

    `settings`: the owner's Settings the render uses (resolution, fps, encoder, crf, colors, sizes, countdown, support
    overlay, image pacing) -- the SAME ones as the song's own video; the saved settings.json when not given.
    `font_path`: a font given on the command line (else the Settings' own).

    `audio_path_override` is for the case load_redo_inputs() can't resolve on its own -- a song
    recorded before run_pipeline() kept its own local audio copy, whose original external
    audio_path has since been cleaned up from its batch-staging folder. The normal case (a local
    copy exists, or the original path is still there) needs no override."""
    from dataclasses import replace

    from .layout import fill_missing_instrumental_images, instrumental_caption_sources

    work_dir = Path(work_dir)
    # Owner, 2026-09-26: the EASY version's capo and its stated original key come from the song's key, so it is only ever
    # built from a key that was settled (key_decision.py). A song never checked, or still waiting for its key, gets none.
    problem = easy_chord_build_problem(work_dir, check_audio=False)       # the audio is checked below, exactly
    if problem:
        print(f"EASY CHORD version not built for {display_slug(work_dir)}: {problem}")
        return None
    song = load_song(work_dir / "lyrics_timed.json")
    capo_fret, shape_key = capo_and_shape_key(song.chord_track.key)
    base_settings = settings if settings is not None else Settings.load()
    capo_chords = transpose_chord_track(song.chord_track, capo_fret, shape_key, prefer_flats=base_settings.prefer_flats)
    if not capo_track_matches(song.chord_track, capo_chords, capo_fret):
        raise RuntimeError(f"the EASY CHORD chords for {work_dir.name} are not the song's own chords shifted by capo {capo_fret}")

    if audio_path_override is not None:
        audio_path = Path(audio_path_override)
    else:
        audio_path, _resolved_title = load_redo_inputs(work_dir)
        if not Path(audio_path).is_file():
            print(f"EASY CHORD version not built for {display_slug(work_dir)}: its audio file is gone ({audio_path}).")
            return None

    info = json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
    original_title = info["title"]
    capo_work_dir = work_dir / EASY_DIR
    capo_work_dir.mkdir(parents=True, exist_ok=True)
    capo_info = {**info, "title": f"{original_title} EasyChords"}
    marker = {"capo_fret": capo_fret, "shape_key": shape_key, "original_key": song.chord_track.key,
              "original_title": original_title}
    capo_song = Song(
        title=capo_info["title"],
        audio_path=song.audio_path,
        vocal_stem_path=song.vocal_stem_path,
        instrumental_stem_path=song.instrumental_stem_path,
        lines=song.lines,
        chord_track=capo_chords,
        image_cache=song.image_cache,
        lyrics_source=song.lyrics_source,
        lyrics_accuracy_concern=song.lyrics_accuracy_concern,
    )
    if not _easy_version_unchanged(capo_work_dir, capo_song, marker):
        # The old EASY video no longer matches: out of the upload lists before anything is rewritten; held (Flagged,
        # Rebuild EASY version) until the new render finishes, which removes the mark.
        _set_aside_videos(capo_work_dir)
        _hold_easy_version(
            capo_work_dir,
            f"{EASY_STALE_PREFIX} it is being made again from its song; if this stays, that render did not finish. "
            "Use Rebuild EASY version.",
        )

    (capo_work_dir / "song_info.json").write_text(json.dumps(capo_info), encoding="utf-8")
    save_easy_chord_capo_marker(capo_work_dir, **marker)
    save_song(capo_song, capo_work_dir / "lyrics_timed.json")
    _carry_owner_verification(work_dir, song, capo_work_dir, capo_song)

    images_dir = work_dir / "images"
    capo_images_dir = capo_work_dir / "images"
    _mirror_song_images(images_dir, capo_images_dir)
    fill_missing_instrumental_images(
        capo_images_dir, capo_song.lines, capo_chords, song_end_time(capo_song),
        source_dirs=[images_dir], source_captions=instrumental_caption_sources(song.chord_track, capo_chords),
        replace_from_sources=True,
    )

    variant_settings = replace(base_settings, generate_easy_chord_versions=False)
    return run_pipeline(
        audio_path, capo_work_dir, title=capo_info["title"],
        start_stage="render", end_stage="render", capo=capo_fret,
        settings=variant_settings, font_path=font_path,
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
