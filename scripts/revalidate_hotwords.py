"""Re-checks a song's sync score with Whisper hotwords enabled (2026-09-27), WITHOUT touching any song's own saved
files, to prove the hotwords change (transcribe.py, pipeline.py) does not regress a song that already works.
Covers BOTH parts of the feature: an owner-edited song (Part 1 -- hint = the owner's own confirmed lyric lines)
and every OTHER song that has been through the align stage at least once (Part 2 -- hint = that song's own
accepted, already-saved lyric lines; the real pipeline decouples the audio-check's UNHINTED verification from a
separate, hinted re-transcription made only after a candidate is accepted, so the hint here is simply what got
saved, never a fresh network lookup). Read-only except for a scratch re-transcription.

  .venv/bin/python scripts/revalidate_hotwords.py             # every song under work/ with saved word timings
  .venv/bin/python scripts/revalidate_hotwords.py --only <song>

Exits 1 if any song's score gets WORSE with hotwords than without, or could not be re-scored at all after
scoring fine before (never a silent "no regression" for a song this could not actually check); 0 otherwise
(including "no songs found", printed but not counted as a failure). This checks ONE narrow thing -- the timing
sync share -- not whether a song is otherwise flawless (many already have unrelated imperfections this never
looks at, confirmed with the owner, 2026-09-27)."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.anchors import HeardWord
from lyricvideo.models import load_song
from lyricvideo.owner_whisper import add_corrections, corrected_heard_words
from lyricvideo.pipeline import load_redo_inputs
from lyricvideo.timing_gate import check_sync
from lyricvideo.transcribe import _language, lyric_hotwords

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def find_owner_lyrics_songs(work_root: Path) -> list[Path]:
    found = []
    for timed_path in sorted(Path(work_root).glob("*/lyrics_timed.json")):
        try:
            song = load_song(timed_path)
        except Exception:
            continue
        if song.lyrics_source == "owner":
            found.append(timed_path.parent)
    return found


def find_fetched_lyrics_songs(work_root: Path) -> list[Path]:
    """Every OTHER song dir under work_root that has been through the align stage at least once (a real
    lyrics_timed.json with at least one line that has words)."""
    found = []
    for timed_path in sorted(Path(work_root).glob("*/lyrics_timed.json")):
        try:
            song = load_song(timed_path)
        except Exception:
            continue
        if song.lyrics_source != "owner" and any(line.words for line in song.lines):
            found.append(timed_path.parent)
    return found


def _line_words_and_times(song) -> tuple[list[list[str]], list[tuple[float, float]]]:
    line_words = [[w.word for w in line.words] for line in song.lines]
    times = [(w.start_time or 0.0, w.end_time or (w.start_time or 0.0)) for line in song.lines for w in line.words]
    return line_words, times


def revalidate_one(song_dir: Path, model=None) -> dict:
    """Read-only except for a scratch re-transcription (never overwrites the real transcript.json). The hint is
    always the song's OWN currently-saved lyric lines -- the same text the real pipeline's decoupled design
    would hint a fresh anchor-transcription with, whether that text came from the owner or was fetched and
    accepted. Both scores apply the SAME saved owner_whisper.json corrections (add_corrections()), matching what
    a real Redo's own re-score does -- comparing a corrected "before" against an uncorrected "after" would be
    apples to oranges and could mask or manufacture a regression (bug found in review, 2026-09-27)."""
    song_dir = Path(song_dir)
    song = load_song(song_dir / "lyrics_timed.json")
    kind = "owner" if song.lyrics_source == "owner" else "fetched"
    line_words, times = _line_words_and_times(song)

    before_heard = corrected_heard_words(song_dir, song=song)
    before_share = check_sync(line_words, times, before_heard).share

    audio_path, _title = load_redo_inputs(song_dir)
    vocals_path = song_dir / "htdemucs" / Path(audio_path).stem / "vocals.wav"
    if not vocals_path.exists():
        return {"song": song_dir.name, "kind": kind, "before_share": before_share, "after_share": None,
                "regressed": False, "unscorable_after": False}

    from lyricvideo.transcribe import _load_model  # only if model is None; kept lazy on purpose

    real_model = model or _load_model()
    segments, _info = real_model.transcribe(
        str(vocals_path), language=_language(), vad_filter=False, temperature=0.0,
        condition_on_previous_text=False, beam_size=1, word_timestamps=True,
        hotwords=lyric_hotwords([line.text for line in song.lines]) or None,
    )
    after_raw = [
        HeardWord(w.word.strip(), float(w.start), float(w.end))
        for s in segments for w in (getattr(s, "words", None) or []) if w.word.strip()
    ]
    after_heard = add_corrections(song_dir, after_raw, line_words, times)
    after_share = check_sync(line_words, times, after_heard).share

    regressed = before_share is not None and after_share is not None and after_share < before_share
    unscorable_after = before_share is not None and after_share is None
    return {"song": song_dir.name, "kind": kind, "before_share": before_share, "after_share": after_share,
            "regressed": regressed, "unscorable_after": unscorable_after}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="just this song's work-folder name")
    args = parser.parse_args(argv)

    work_root = PROJECT_ROOT / "work"
    songs = find_owner_lyrics_songs(work_root) + find_fetched_lyrics_songs(work_root)
    if args.only:
        songs = [s for s in songs if s.name == args.only]
    if not songs:
        print("No songs with saved word timings found.")
        return 0

    any_bad = False
    for song_dir in songs:
        try:
            result = revalidate_one(song_dir)
        except Exception as e:
            print(f"  [ERROR  ] {song_dir.name:40} {type(e).__name__}: {e}", flush=True)
            any_bad = True
            continue
        before = f"{result['before_share']:.1%}" if result["before_share"] is not None else "?"
        after = f"{result['after_share']:.1%}" if result["after_share"] is not None else "?"
        flag = " REGRESSED" if result["regressed"] else (" COULD NOT RE-SCORE" if result["unscorable_after"] else "")
        print(f"  [{result['kind']:7}] {result['song']:40} {before:>6} -> {after:>6}{flag}", flush=True)
        any_bad = any_bad or result["regressed"] or result["unscorable_after"]
    return 1 if any_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
