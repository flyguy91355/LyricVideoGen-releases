from __future__ import annotations

import argparse
import json
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

from .align import align_words
from .assemble import assemble_video
from .combine import combine_alignment
from .detect_chords import detect_chords
from .fetch_lyrics import fetch_lyric_lines_verified
from .identify import extract_metadata
from .imagery import get_or_generate_image, is_fallback_image, substitute_fallback_images, summarize_song_gist
from .layout import instrumental_image_captions
from .lyric_audio_match import score_lyrics_against_transcript
from .lyric_reconcile import SUGGESTION_FILENAME, reconcile_lyrics
from .models import ChordTrack, LyricLine, Song, Word, load_song, save_song
from .separate import separate_vocals
from .settings import Settings
from .transcribe import load_transcript_segments, transcribe_vocals
from .youtube_state import STATE_FILENAME

STAGES = ["identify", "separate", "fetch_lyrics", "align", "detect_chords", "images", "render"]


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


def ordered_unique_chords(chord_track: ChordTrack) -> list[str]:
    """Every distinct chord label in the song, in first-seen order across ALL
    events (sung or instrumental) -- backs the chord fingering legend, which
    shows one diagram per chord regardless of whether it happens during vocals.
    'N' (no-chord) is excluded -- there's nothing to finger."""
    labels: list[str] = []
    for event in chord_track.events:
        if event.label != "N" and event.label not in labels:
            labels.append(event.label)
    return labels


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
    """Names of work_root's immediate subdirectories that have a rendered
    video, whether or not it's ever been uploaded to YouTube -- backs the
    GUI's single-song Upload dropdown, which (unlike list_pending_uploads())
    must also offer an already-uploaded song so the owner can force a fresh
    re-upload (a correction/re-post) for any past song, not just the one
    from the current session."""
    if not work_root.exists():
        return []
    return sorted(
        entry.name
        for entry in work_root.iterdir()
        if entry.is_dir() and song_video_path(entry) is not None
    )


def list_pending_uploads(work_root: Path) -> list[str]:
    """Names of work_root's immediate subdirectories that have a rendered
    video but no recorded YouTube upload yet -- backs the GUI's retry-upload
    dropdown for a failed upload (e.g. YouTube's daily uploadLimitExceeded
    cap) on a song that isn't self._last_work_dir (only set by Generate/Redo
    in the same session, not Batch). Filesystem-only, no live YouTube call:
    schedule_upload() only ever writes youtube_state.json AFTER a successful
    upload, so a missing one is already the right signal that nothing
    succeeded -- no need for a network round trip per song just to build
    this list."""
    if not work_root.exists():
        return []
    return sorted(
        entry.name
        for entry in work_root.iterdir()
        if entry.is_dir()
        and not (entry / STATE_FILENAME).exists()
        and song_video_path(entry) is not None
    )


def list_flagged_songs(work_root: Path) -> list[str]:
    """Names of work_root's immediate subdirectories whose lyrics were
    never confirmed accurate by check_lyric_accuracy() across every source
    tried (Song.lyrics_accuracy_concern non-empty) and that haven't been
    uploaded yet -- backs the GUI's "Flagged for Lyrics Review" list. Same
    not-yet-uploaded convention as list_pending_uploads() above, so a song
    naturally drops off this list once the owner uploads it anyway (via
    the review panel's own Upload Anyway) without needing a separate
    "dismiss" action -- and once a later Redo's fresh fetch clears the
    concern, it drops off too."""
    if not work_root.exists():
        return []
    flagged = []
    for entry in sorted(work_root.iterdir(), key=lambda e: e.name):
        if not entry.is_dir() or (entry / STATE_FILENAME).exists() or song_video_path(entry) is None:
            continue
        timed_path = entry / "lyrics_timed.json"
        if not timed_path.exists():
            continue
        try:
            song = load_song(timed_path)
        except Exception:
            continue
        if song.lyrics_accuracy_concern:
            flagged.append(entry.name)
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


def run_pipeline(
    audio_path: Path,
    work_dir: Path,
    title: str | None = None,
    start_stage: str = "identify",
    font_path: str | None = None,
    settings: Settings | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    start_idx = STAGES.index(start_stage)
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

    if start_idx <= STAGES.index("separate"):
        report("separate")
        vocals_path = separate_vocals(audio_path, work_dir)
    elif start_idx <= STAGES.index("detect_chords") and not (
        vocals_path.exists() and instrumental_stem_path.exists()
    ):
        # Resuming past separation, but the stems the align/detect_chords
        # stages below read aren't on disk (an htdemucs/ folder deleted to
        # save space, or a batch "already done" song whose stems never made
        # it here) -- those stages would crash on the missing file. Demucs
        # is deterministic and costs no API spend, so just run it again --
        # the same self-heal the identify bootstrap above applies to a
        # missing song_info.json. A resume at images/render never reads the
        # stems, so it's left alone.
        report("separate")
        vocals_path = separate_vocals(audio_path, work_dir)

    if start_idx <= STAGES.index("fetch_lyrics"):
        report("fetch_lyrics")
        info_data = json.loads(info_path.read_text(encoding="utf-8"))
        lyrics_anthropic_client = anthropic.Anthropic()
        audio_check = _build_audio_check(vocals_path, work_dir)
        reconcile = _build_reconcile(work_dir, lyrics_anthropic_client) if audio_check is not None else None
        lines_text, lyrics_source, lyrics_concern = fetch_lyric_lines_verified(
            audio_path, info_data["title"], info_data["artist"], info_data["duration"],
            info_data.get("alt_titles"), lyrics_anthropic_client, audio_check=audio_check, reconcile=reconcile,
        )
        if audio_check is not None:
            if lyrics_concern:
                print(
                    f"WARNING: the lyrics could not be confirmed against the audio "
                    f"({lyrics_source or 'no source'}): {lyrics_concern} This song is held in "
                    "Flagged for Lyrics Review instead of auto-uploading.", file=sys.stderr,
                )
            else:
                print(f"Lyrics verified against the audio (source: {lyrics_source}).")
        lyrics_path.write_text(
            json.dumps({"lines": lines_text, "source": lyrics_source, "concern": lyrics_concern}),
            encoding="utf-8",
        )

    if start_idx <= STAGES.index("align"):
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
        word_times = align_words(vocals_path, flat_words)
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
    else:
        song = load_song(timed_path)

    if start_idx <= STAGES.index("detect_chords"):
        report("detect_chords")
        song.chord_track = detect_chords(instrumental_stem_path, **detect_kwargs)
        save_song(song, timed_path)

    if start_idx <= STAGES.index("images"):
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

    if start_idx <= STAGES.index("render"):
        report("render")
        assemble_video(
            song.lines, song.chord_track, images_dir, audio_path, final_path,
            font_path or default_font(),
            chord_legend_labels=ordered_unique_chords(song.chord_track),
            **assemble_kwargs,
        )

    report("done")
    return final_path


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

    out = run_pipeline(args.audio, args.work_dir, args.title, args.stage, args.font)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
