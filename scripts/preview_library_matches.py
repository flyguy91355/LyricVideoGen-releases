"""Calibration contact sheet for the shared image library (the owner reviews it before the feature is switched on).

  .venv/bin/python scripts/preview_library_matches.py <song-folder-name-or-path> [...] [--threshold 0.28]

For each song: Claude writes each line's image prompt exactly as the images stage would (the same small call,
cached in reports/library_preview/<song>/prompts.json so re-running is free), the song's own pictures are hidden
from the library, and an HTML page shows, per line, the picture you bought next to the library's closest offer and
its score -- plus how many lines would be reused at each match score. Needs the CLIP model already downloaded
(run scripts/import_image_library.py first) and ANTHROPIC_API_KEY in .env."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from dotenv import load_dotenv

from lyricvideo.clip_embedder import ClipEmbedder, EmbedderUnavailable
from lyricvideo.image_library import ImageLibrary
from lyricvideo.imagery import build_image_prompt, summarize_song_gist
from lyricvideo.library_preview import THRESHOLDS, build_rows, cached_prompt_maker, reuse_counts, write_report
from lyricvideo.models import load_song
from lyricvideo.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("songs", nargs="+", help="a folder name under work/, or a path to a song's work folder")
    parser.add_argument("--threshold", type=float, default=Settings().image_library_min_score)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports" / "library_preview")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(f"ANTHROPIC_API_KEY not found in {PROJECT_ROOT / '.env'}.")
    embedder = ClipEmbedder(allow_download=False)
    try:
        embedder.check_available()
    except EmbedderUnavailable as e:
        raise SystemExit(f"{e}")
    library = ImageLibrary()
    if library.count() == 0:
        raise SystemExit("The library is empty -- run scripts/import_image_library.py first.")
    client = anthropic.Anthropic()

    total_calls = 0
    for name in args.songs:
        song_dir = Path(name) if Path(name).is_dir() else PROJECT_ROOT / "work" / name
        song = load_song(song_dir / "lyrics_timed.json")
        out_dir = args.out / song_dir.name
        gist_for = cached_prompt_maker(
            out_dir / "gist.json",
            lambda _key, song=song: summarize_song_gist(client, "\n".join(line.text for line in song.lines)),
        )
        gist = gist_for("gist")
        prompt_for = cached_prompt_maker(out_dir / "prompts.json", lambda text: build_image_prompt(client, gist, text))
        rows, own_ids = build_rows(song_dir, library, embedder, prompt_for)
        counts = reuse_counts(rows, library, own_ids, THRESHOLDS)
        index = write_report(out_dir, song.title, rows, counts, library, args.threshold)
        total_calls += gist_for.calls + prompt_for.calls
        print(f"{song.title}: {len(rows)} lines/captions -> {index}")
        print("  " + "  ".join(f"{t:.2f}: {n}" for t, n in counts))
    print(f"\nClaude calls made this run: {total_calls} (cached answers are free next time)")
    embedder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
