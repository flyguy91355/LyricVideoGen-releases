"""Lists finished videos whose picture stops before their audio (the cut-short renders of issue #7).

Issue #7: EASY CHORD videos with ~80 s of picture over a 5-6 minute song -- a render that died part-way still left a
playable mp4 whose container reports the full audio length. Dry run by default: it only lists them, changes nothing.

  .venv/bin/python scripts/find_truncated_videos.py              # list every cut-short video under work/
  .venv/bin/python scripts/find_truncated_videos.py --only some-song/easychords
  .venv/bin/python scripts/find_truncated_videos.py --set-aside  # also rename each one to <name>.truncated.mp4

Checks every mp4 in each song folder and each nested <song>/easychords folder (the folders the app itself makes
videos in), reading the picture and audio lengths by stream copy (no decoding; about a second per video; nothing is
sent anywhere). Skips *.previous.mp4 (set aside by a Redo hold or settle_keys.py), *.truncated*.mp4 (set aside by
this script), unfinished *.rendering files, and redo_backup_*/ copies.

  CUT SHORT     picture more than 1 s shorter than the audio. Uploads already refuse these
                (youtube_schedule.IncompleteVideo). Make the video again: Redo the song (tick Easy Chords to remake
                its EASY CHORD version too).
  ON YOUTUBE    the folder has a youtube_state.json: that video was uploaded. Delete it (or make it private) on YouTube
                -- a still-scheduled one will otherwise publish cut short -- then make it again and upload the new one.
  NO AUDIO / UNREADABLE   listed for a look by hand; never set aside.

--set-aside renames each CUT SHORT file to <name>.truncated.mp4 (never deletes anything) so the app no longer treats
it as a finished video."""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.assemble import rendered_stream_seconds
from lyricvideo.youtube_schedule import is_cut_short
from lyricvideo.youtube_state import STATE_FILENAME, load_youtube_state, song_dirs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"
SET_ASIDE_SUFFIX = ".truncated"


@dataclass
class VideoCheck:
    path: Path
    label: str                      # "<song>/<file>.mp4" or "<song>/easychords/<file>.mp4"
    picture: float | None = None
    audio: float | None = None
    error: str = ""                 # why it could not be read ("" when it was)
    youtube_video_id: str = ""      # set when the folder holds a youtube_state.json
    on_youtube: bool = False

    @property
    def cut_short(self) -> bool:
        return not self.error and self.picture is not None and is_cut_short(self.picture, self.audio)

    @property
    def no_audio(self) -> bool:
        return not self.error and self.audio is None


def _is_skipped(video: Path) -> bool:
    name = video.name
    return (
        name.endswith(".previous.mp4")
        or f"{SET_ASIDE_SUFFIX}." in name or f"{SET_ASIDE_SUFFIX}-" in name
        or ".rendering" in name
        or "TEMP_MPY" in name                   # an old moviepy temp audio file, not a video
    )


def find_videos(work_root: Path, only: str | None = None) -> list[tuple[str, Path]]:
    """(label, path) for every finished mp4 in work_root's song folders and nested EASY CHORD folders."""
    found = []
    wanted = only.replace("\\", "/").strip("/") if only else ""
    for slug, folder in song_dirs(work_root):
        if wanted and slug != wanted:
            continue
        for video in sorted(folder.glob("*.mp4")):
            if video.is_file() and not _is_skipped(video):
                found.append((f"{slug}/{video.name}", video))
    return found


def check_video(label: str, path: Path) -> VideoCheck:
    check = VideoCheck(path=path, label=label)
    if (path.parent / STATE_FILENAME).exists():
        check.on_youtube = True
        state = load_youtube_state(path.parent)
        check.youtube_video_id = state.video_id if state is not None else ""
    try:
        check.picture, check.audio = rendered_stream_seconds(path)
    except Exception as e:  # one unreadable file must not stop the rest
        check.error = f"{type(e).__name__}: {' '.join(str(e).split())[:160]}"
    return check


def set_aside(path: Path) -> Path:
    """Renames `path` to <stem>.truncated.mp4 (or .truncated-2.mp4, ... if that name is taken); never overwrites."""
    target = path.with_name(f"{path.stem}{SET_ASIDE_SUFFIX}{path.suffix}")
    n = 2
    while target.exists():
        target = path.with_name(f"{path.stem}{SET_ASIDE_SUFFIX}-{n}{path.suffix}")
        n += 1
    path.rename(target)
    return target


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _describe(check: VideoCheck) -> str:
    where = ""
    if check.on_youtube:
        where = f"  ON YOUTUBE ({check.youtube_video_id or 'video id unreadable'})"
    if check.error:
        return f"  UNREADABLE  {check.label}{where}\n              {check.error}"
    if check.no_audio:
        return f"  NO AUDIO    {check.label}  picture {_minutes(check.picture)}{where}"
    return f"  CUT SHORT   {check.label}  picture {_minutes(check.picture)} of audio {_minutes(check.audio)}{where}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--work-root", type=Path, default=WORK_DIR, help="the work folder (default: this repo's work/)")
    parser.add_argument("--only", help='just this folder, e.g. "some-song" or "some-song/easychords"')
    parser.add_argument("--set-aside", action="store_true",
                        help="rename each cut-short file to <name>.truncated.mp4 (default: dry run, nothing changes)")
    parser.add_argument("--jobs", type=int, default=2, help="videos read at once (default 2)")
    args = parser.parse_args(argv)

    if not args.work_root.is_dir():
        print(f"No work folder at {args.work_root} -- nothing checked.")
        return 1
    videos = find_videos(args.work_root, args.only)
    if args.only and not videos:
        print(f'No finished video found in "{args.only}" -- expected a song folder name, or "<song>/easychords".')
        return 1
    print(f"Checking {len(videos)} videos under {args.work_root} ...")
    problems: list[VideoCheck] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for n, check in enumerate(pool.map(lambda pair: check_video(*pair), videos), start=1):
            if check.cut_short or check.no_audio or check.error:
                problems.append(check)
                print(_describe(check), flush=True)
            if n % 50 == 0:
                print(f"  ... {n}/{len(videos)} checked", flush=True)

    cut = [c for c in problems if c.cut_short]
    uploaded = [c for c in cut if c.on_youtube]
    unreadable = [c for c in problems if c.error or c.no_audio]
    print(
        f"\n{len(videos)} videos checked: {len(cut)} cut short ({len(uploaded)} already on YouTube), "
        f"{len(unreadable)} without readable audio or unreadable."
    )
    if uploaded:
        print("Already on YouTube -- delete or make private there (a scheduled one publishes cut short), then make "
              "each again and upload the new one:")
        for c in uploaded:
            print(f"  {c.label}  (video {c.youtube_video_id or '?'})")
    if not cut:
        return 0
    if not args.set_aside:
        print("DRY RUN (nothing changed) -- run with --set-aside to rename the cut-short files to *.truncated.mp4.")
        return 0
    moved = 0
    for c in cut:
        try:
            target = set_aside(c.path)
        except OSError as e:  # e.g. open in a player on Windows
            print(f"  could not set aside {c.label}: {e}")
            continue
        moved += 1
        print(f"  set aside   {c.label} -> {target.name}")
    print(f"SET ASIDE: {moved} of {len(cut)} renamed. Make them again with Redo (tick Easy Chords to remake an EASY "
          "CHORD version).")
    return 0 if moved == len(cut) else 1


if __name__ == "__main__":
    raise SystemExit(main())
