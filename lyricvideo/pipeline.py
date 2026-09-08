from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Callable

import anthropic
import torchaudio
from dotenv import load_dotenv

from .align import align_words
from .assemble import assemble_video
from .combine import combine_alignment
from .imagery import get_or_generate_image, summarize_song_gist
from .instrumental_chords import detect_chord_change_times
from .models import (
    ChordWord,
    InstrumentalBlock,
    InstrumentalChord,
    LyricLine,
    Song,
    load_song,
    save_song,
)
from .ocr_parse import parse_tab_pdf_via_ocr
from .pdf_parse import NoChordLyricPairsError, NoTextLayerError, parse_tab_pdf
from .plaintext_chords import parse_plaintext_chords
from .separate import separate_vocals
from .vision_parse import map_chords_with_vision, split_lyrics_text

STAGES = ["separate", "parse", "align", "images", "render"]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled-song"


def line_to_dict(line: LyricLine) -> dict:
    return {
        "words": [
            {"word": w.word, "chord": w.chord, "start_time": w.start_time, "end_time": w.end_time}
            for w in line.words
        ],
        "start_time": line.start_time,
        "end_time": line.end_time,
    }


def dict_to_line(d: dict) -> LyricLine:
    return LyricLine(
        words=[ChordWord(**w) for w in d["words"]],
        start_time=d.get("start_time"),
        end_time=d.get("end_time"),
    )


def _parsed_to_dict(lines: list[LyricLine], blocks: list[InstrumentalBlock]) -> dict:
    return {
        "lines": [line_to_dict(l) for l in lines],
        "instrumental_blocks": [
            {"chords": b.chords, "before_line_index": b.before_line_index} for b in blocks
        ],
    }


def _dict_to_parsed(d: dict) -> tuple[list[LyricLine], list[InstrumentalBlock]]:
    lines = [dict_to_line(ld) for ld in d["lines"]]
    blocks = [InstrumentalBlock(**bd) for bd in d.get("instrumental_blocks", [])]
    return lines, blocks


def _compute_instrumental_chords(
    blocks: list[InstrumentalBlock],
    timed_lines: list[LyricLine],
    instrumental_stem_path: Path,
    audio_duration: float,
) -> list[InstrumentalChord]:
    result: list[InstrumentalChord] = []
    for block in blocks:
        if not block.chords:
            continue
        gap_start = (
            0.0 if block.before_line_index == 0 else timed_lines[block.before_line_index - 1].end_time
        )
        gap_end = (
            timed_lines[block.before_line_index].start_time
            if block.before_line_index < len(timed_lines)
            else audio_duration
        )
        spans = detect_chord_change_times(
            instrumental_stem_path, gap_start, gap_end, len(block.chords)
        )
        for chord, (start, end) in zip(block.chords, spans):
            result.append(InstrumentalChord(chord=chord, start_time=start, end_time=end))
    return result


def _default_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    raise FileNotFoundError("No default font found; pass --font explicitly")


def run_pipeline(
    audio_path: Path,
    tab_pdf_path: Path | None,
    work_dir: Path,
    title: str,
    start_stage: str = "separate",
    font_path: str | None = None,
    lyrics_file: Path | None = None,
    chords_text_file: Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    if tab_pdf_path is None and chords_text_file is None:
        raise ValueError("supply either tab_pdf_path or chords_text_file")

    def report(stage: str) -> None:
        if progress_callback is not None:
            progress_callback(stage)

    work_dir.mkdir(parents=True, exist_ok=True)
    # Matches separate_vocals()'s own output path convention, so resuming from a
    # later stage (skipping separation) still finds the file it already wrote.
    demucs_dir = work_dir / "htdemucs" / Path(audio_path).stem
    vocals_path = demucs_dir / "vocals.wav"
    instrumental_stem_path = demucs_dir / "no_vocals.wav"
    parsed_path = work_dir / "parsed_tab.json"
    timed_path = work_dir / "lyrics_timed.json"
    images_dir = work_dir / "images"
    final_path = work_dir / f"{slugify(title)}.mp4"

    start_idx = STAGES.index(start_stage)

    if start_idx <= STAGES.index("separate"):
        report("separate")
        vocals_path = separate_vocals(audio_path, work_dir)

    if start_idx <= STAGES.index("parse"):
        report("parse")
        if chords_text_file is not None:
            # Owner-typed chord-over-lyric plain text: pure deterministic text
            # parsing, no Claude/vision call at all -- the guaranteed-safe path
            # for songs whose vision-based chord mapping hits Anthropic's
            # content-filtering policy (confirmed real, non-deterministic, and
            # for some songs' most iconic lines persistently high-failure-rate).
            parsed_lines, instrumental_blocks = parse_plaintext_chords(
                chords_text_file.read_text(encoding="utf-8")
            )
        else:
            try:
                parsed_lines, instrumental_blocks = parse_tab_pdf(tab_pdf_path)
            except NoTextLayerError:
                # Scanned/image-only PDF: pdfplumber found no real text layer to
                # extract. Fall back to vision-based chord-position mapping,
                # which requires the owner's own plain lyric text as input
                # (Claude never generates lyric text itself, only [word_index,
                # chord] pairs -- see vision_parse.py).
                if lyrics_file is None:
                    raise NoTextLayerError(
                        f"{tab_pdf_path} has no extractable text layer, and no --lyrics-file "
                        "was given to map chords onto. Supply a plain text file with the "
                        "song's lyrics via --lyrics-file to use the vision fallback, or a "
                        "--chords-text-file to skip vision entirely."
                    )
                lyric_lines = split_lyrics_text(lyrics_file.read_text(encoding="utf-8"))
                parsed_lines, instrumental_blocks = map_chords_with_vision(
                    anthropic.Anthropic(), tab_pdf_path, lyric_lines
                )
        parsed_path.write_text(
            json.dumps(_parsed_to_dict(parsed_lines, instrumental_blocks), indent=2),
            encoding="utf-8",
        )

    if start_idx <= STAGES.index("align"):
        report("align")
        if start_idx > STAGES.index("parse"):
            parsed_lines, instrumental_blocks = _dict_to_parsed(
                json.loads(parsed_path.read_text(encoding="utf-8"))
            )
        waveform, sample_rate = torchaudio.load(str(vocals_path))
        audio_duration = waveform.shape[1] / sample_rate
        flat_words = [w.word for line in parsed_lines for w in line.words]
        word_times = align_words(vocals_path, flat_words)
        timed_lines = combine_alignment(parsed_lines, word_times, audio_duration)
        instrumental_chords = _compute_instrumental_chords(
            instrumental_blocks, timed_lines, instrumental_stem_path, audio_duration
        )
        song = Song(
            title=title,
            audio_path=str(audio_path),
            vocal_stem_path=str(vocals_path),
            instrumental_stem_path=str(instrumental_stem_path),
            lines=timed_lines,
            instrumental_chords=instrumental_chords,
        )
        save_song(song, timed_path)
    else:
        song = load_song(timed_path)

    if start_idx <= STAGES.index("images"):
        report("images")
        anthropic_client = anthropic.Anthropic()
        replicate_token = os.environ["REPLICATE_API_TOKEN"]
        full_lyrics = "\n".join(l.text for l in song.lines)
        song_gist = summarize_song_gist(anthropic_client, full_lyrics)
        images_dir.mkdir(exist_ok=True)
        # Reuse already-paid-for images from any prior images_backup_*/ archive
        # (see run_pipeline callers' convention of backing up rather than deleting
        # an images/ dir before regenerating) before spending on a new one.
        backup_dirs = sorted(work_dir.glob("images_backup_*"))
        for line in song.lines:
            get_or_generate_image(
                anthropic_client, replicate_token, song_gist, line.text, images_dir,
                extra_cache_dirs=backup_dirs,
            )

    if start_idx <= STAGES.index("render"):
        report("render")
        assemble_video(
            song.lines,
            images_dir,
            audio_path,
            final_path,
            font_path or _default_font(),
            instrumental_chords=song.instrumental_chords,
        )

    report("done")
    return final_path


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Generate a synced lyric+chord video from a tab PDF and audio file."
    )
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument(
        "--tab-pdf", type=Path, default=None,
        help="Tab/chord-sheet PDF. Required unless --chords-text-file is given instead.",
    )
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--stage", choices=STAGES, default="separate")
    parser.add_argument("--font", default=None)
    parser.add_argument(
        "--lyrics-file",
        type=Path,
        default=None,
        help="Plain text file with the song's lyrics, required only if --tab-pdf turns "
        "out to be a scanned/image-only PDF (used for vision-based chord mapping).",
    )
    parser.add_argument(
        "--chords-text-file",
        type=Path,
        default=None,
        help="Owner-typed chord-over-lyric plain text file (standard tab-site format). "
        "When given, skips --tab-pdf entirely -- pure text parsing, no Claude/vision call.",
    )
    args = parser.parse_args()

    out = run_pipeline(
        args.audio, args.tab_pdf, args.work_dir, args.title, args.stage, args.font,
        args.lyrics_file, args.chords_text_file,
    )
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
