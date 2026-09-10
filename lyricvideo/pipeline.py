from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
from .fetch_lyrics import fetch_lyric_lines
from .identify import extract_metadata
from .imagery import get_or_generate_image, summarize_song_gist
from .layout import _in_a_line
from .models import ChordTrack, LyricLine, Song, Word, load_song, save_song
from .separate import separate_vocals
from .settings import Settings

STAGES = ["identify", "separate", "fetch_lyrics", "align", "detect_chords", "images", "render"]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled-song"


def _instrumental_chord_labels(lines: list[LyricLine], chord_track: ChordTrack) -> list[str]:
    """Distinct chord labels that need their own instrumental-gap background image:
    every ChordEvent whose midpoint doesn't fall inside any sung line's window,
    in first-seen order (so a repeated chord anywhere in the song reuses one
    image, the same dedup-by-content philosophy the per-line images already use)."""
    labels: list[str] = []
    for event in chord_track.events:
        mid = (event.start + event.end) / 2
        if not _in_a_line(lines, mid) and event.label not in labels:
            labels.append(event.label)
    return labels


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


def load_redo_inputs(song_dir: Path) -> tuple[Path, str]:
    """Reads back the (audio_path, title) a prior run saved onto its own
    Song, so a redo never needs the owner to re-browse for the original
    audio file."""
    song = load_song(song_dir / "lyrics_timed.json")
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
        }

    def report(stage: str) -> None:
        if progress_callback is not None:
            progress_callback(stage)

    work_dir.mkdir(parents=True, exist_ok=True)
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

    if start_idx <= STAGES.index("fetch_lyrics"):
        report("fetch_lyrics")
        info_data = json.loads(info_path.read_text(encoding="utf-8"))
        lines_text = fetch_lyric_lines(
            audio_path, info_data["title"], info_data["artist"], info_data["duration"],
            info_data.get("alt_titles"),
        )
        lyrics_path.write_text(json.dumps(lines_text), encoding="utf-8")

    if start_idx <= STAGES.index("align"):
        report("align")
        lines_text = json.loads(lyrics_path.read_text(encoding="utf-8"))
        parsed_lines = [LyricLine(words=[Word(word=w) for w in text.split()]) for text in lines_text]
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
        replicate_token = os.environ["REPLICATE_API_TOKEN"]
        full_lyrics = "\n".join(l.text for l in song.lines)
        song_gist = summarize_song_gist(anthropic_client, full_lyrics)
        images_dir.mkdir(exist_ok=True)
        # Reuse already-paid-for images from any prior images_backup_*/ archive
        # before spending on a new one (unchanged convention).
        backup_dirs = sorted(work_dir.glob("images_backup_*"))
        for line in song.lines:
            get_or_generate_image(
                anthropic_client, replicate_token, song_gist, line.text, images_dir,
                extra_cache_dirs=backup_dirs,
            )
        # Instrumental-gap images (2026-09-09 owner request): one per distinct
        # chord label that actually occurs during a gap, so the background
        # follows the chord instead of freezing on the last-sung line's image.
        for label in _instrumental_chord_labels(song.lines, song.chord_track):
            caption = f"[Instrumental — chord: {label}]"
            get_or_generate_image(
                anthropic_client, replicate_token, song_gist, caption, images_dir,
                extra_cache_dirs=backup_dirs,
            )

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
