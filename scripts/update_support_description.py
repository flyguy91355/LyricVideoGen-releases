"""Re-renders the YouTube description of videos ALREADY uploaded into the current layout: the song's own
description first, then a key-correction note if one applies, then the owner's support template (tip line, then a
thank-you) -- see youtube_schedule.assemble_description. Owner, 2026-09-29 (reversing a 2026-09-26 layout that put
the tip/key block on top): "detailed description of the song first... then the key if you need one, not all do if
there correct... then the donation part... then the thank you for watching part."

A description whose own song-text body is already reasonably long (READY_CHARS or more) is just REORDERED -- its
wording is kept exactly as written. A body shorter than that (the old prompt only asked for "2-4 sentences") gets a
fresh Claude call instead, using the song's real lyrics from its work folder, so every video ends up with a
description that clears YouTube's "...more" cutoff on its own, not just a short paragraph pushed to the front.

  .venv/bin/python scripts/update_support_description.py --only blackbird            # one song
  .venv/bin/python scripts/update_support_description.py --status scheduled --dry-run # preview the scheduled ones
  .venv/bin/python scripts/update_support_description.py --status scheduled          # every scheduled (private + publish date) video
  .venv/bin/python scripts/update_support_description.py                             # every uploaded video

--status: scheduled = private with a publish date, public = already public, all = everything (default).
A "📌 Song key: ..." correction note (scripts/add_key_note.py) always stays the paragraph right after the
description, before the support template. Safe to re-run: a video already in the new layout AND already long
enough is skipped. Before changing anything, the current snippet of every video about to change is saved to
reports/support_text_backup_<timestamp>.json so it can be put back. Stops cleanly if YouTube's daily quota runs
out (each update costs 50 units, regenerating a description ALSO costs one Claude call) -- run it again later to
finish. Only the description changes: title, tags, category and the private/scheduled status are left exactly as
they are."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyricvideo.chord_theory import load_easy_chord_capo_marker
from lyricvideo.key_note import split_key_note, strip_song_key_line
from lyricvideo.models import load_song
from lyricvideo.settings import Settings
from lyricvideo.youtube import is_quota_exceeded_error
from lyricvideo.youtube_auth import load_credentials
from lyricvideo.youtube_metadata import generate_video_metadata
from lyricvideo.youtube_schedule import _load_artist, assemble_description, description_body
from lyricvideo.youtube_state import uploaded_song_dirs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"
DEFAULT_OLD_TEXT = "Support: https://ko-fi.com/playalongvideos"
READY_CHARS = 200   # a song-description body at least this long is left exactly as written, just reordered


def find_uploaded_videos(work_dir: Path | None = None) -> dict[str, tuple[str, Path]]:
    """video_id -> (label, its work folder), e.g. "blackbird" -> (work/blackbird)."""
    return {state.video_id: (slug, path) for slug, path, state in uploaded_song_dirs(work_dir or WORK_DIR)}


def _song_facts(work_dir: Path) -> tuple[str, str, str] | None:
    """(title, artist, full lyrics) for a regeneration call, from the song's own local files -- an EASY CHORD
    folder carries its own lyrics_timed.json (same lyrics, capo-shifted chords) but the CLEAN original title, same
    as schedule_upload's own prompt. None when the song's own files are gone or unreadable -- that song is skipped
    for regeneration (reordering what's already there still happens), never blocked or faked."""
    try:
        song = load_song(work_dir / "lyrics_timed.json")
    except (OSError, ValueError):
        return None
    full_lyrics = "\n".join(line.text for line in song.lines)
    capo_info = load_easy_chord_capo_marker(work_dir)
    title = capo_info["original_title"] if capo_info else song.title
    return title, _load_artist(work_dir), full_lyrics


def new_description(
    current: str, template: str, old_texts: list[str], *,
    anthropic_client=None, work_dir: Path | None = None, ready_chars: int = READY_CHARS,
) -> tuple[str, bool]:
    """(the description this script would write, whether it regenerated the body). The body is regenerated only
    when `anthropic_client` is given, it is shorter than `ready_chars`, and the song's own local files are still
    readable; otherwise the existing body is kept exactly as written, just reordered. Any OLD unconditional
    "🎸 Song key: X" line (every upload used to open with one, before 2026-09-29) is stripped outright, never
    repositioned -- unlike a 📌 correction note, it carries no information the video's own badge doesn't already
    show."""
    note, rest = split_key_note(current)
    rest = strip_song_key_line(rest)
    body = description_body(rest, old_texts, template)
    regenerated = False
    if anthropic_client is not None and len(body) < ready_chars and work_dir is not None:
        facts = _song_facts(work_dir)
        if facts is not None:
            title, artist, full_lyrics = facts
            _title, body, _tags = generate_video_metadata(anthropic_client, title, artist, full_lyrics)
            regenerated = True
    return assemble_description(template, body, note), regenerated


def looks_clean(description: str) -> bool:
    """False when `description` still carries duplicated boilerplate -- the sign that an older, unrecognized
    description format wasn't fully stripped (its own tip/key text left behind alongside the newly added block).
    The ko-fi link and the words "Song key" are present in every format this project has ever written, old or
    new, so more than one copy of either is never legitimate."""
    return description.count("ko-fi.com/playalongvideos") <= 1 and description.count("Song key") <= 1


def video_kind(status: dict) -> str:
    if status.get("privacyStatus") == "private" and status.get("publishAt"):
        return "scheduled"
    return "public" if status.get("privacyStatus") == "public" else "other"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="just this song (its work-folder name) or this YouTube video id")
    parser.add_argument("--status", choices=("all", "scheduled", "public"), default="all")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    parser.add_argument("--old-text", action="append", default=[],
                        help="an extra piece of OLD support text to remove first (repeatable), e.g. the previous "
                             f"sign-off sentence when changing its wording. {DEFAULT_OLD_TEXT!r} is always removed.")
    parser.add_argument("--no-regenerate", action="store_true",
                        help="reorder only -- never call Claude for a short description, even if one is found")
    parser.add_argument("--template-file", type=Path,
                        help="use this file's text as the template instead of Settings.support_description_text "
                             "(e.g. when the saved Settings value hasn't been updated to the new layout yet)")
    args = parser.parse_args(argv)

    template = args.template_file.read_text(encoding="utf-8").strip() if args.template_file \
        else Settings.load().support_description_text.strip()
    old_texts = [DEFAULT_OLD_TEXT, *args.old_text]
    if not template:
        raise SystemExit("Settings.support_description_text is blank -- set it first (Settings popup, or the saved settings file).")
    credentials = load_credentials()
    if credentials is None:
        raise SystemExit("Not connected to YouTube (no valid stored token). Use the app's 'Connect to YouTube' button first.")
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    anthropic_client = None
    if not args.no_regenerate:
        import anthropic

        anthropic_client = anthropic.Anthropic()

    videos = find_uploaded_videos()
    if args.only:
        videos = {vid: (label, path) for vid, (label, path) in videos.items() if args.only in (vid, label)}
        if not videos:
            raise SystemExit(f"No uploaded video matches {args.only!r}.")

    live: dict[str, dict] = {}
    ids = list(videos)
    for start in range(0, len(ids), 50):
        response = youtube.videos().list(part="snippet,status", id=",".join(ids[start:start + 50])).execute()
        for item in response.get("items", []):
            live[item["id"]] = item
    missing = [videos[v][0] for v in ids if v not in live]

    changes: list[tuple[str, str, str, dict, str, bool]] = []     # (video_id, label, kind, snippet, new_description, regenerated)
    messy: list[tuple[str, str]] = []                              # (video_id, label) skipped -- would have duplicated
    up_to_date = skipped_status = 0
    for video_id in ids:
        item = live.get(video_id)
        if item is None:
            continue
        kind = video_kind(item["status"])
        if args.status != "all" and kind != args.status:
            skipped_status += 1
            continue
        label, work_dir = videos[video_id]
        snippet = item["snippet"]
        current = snippet.get("description", "")
        updated, regenerated = new_description(current, template, old_texts, anthropic_client=anthropic_client, work_dir=work_dir)
        if updated == current.strip():
            up_to_date += 1
        elif not looks_clean(updated):
            messy.append((video_id, label))
        else:
            changes.append((video_id, label, kind, snippet, updated, regenerated))

    if messy:
        print(f"{len(messy)} SKIPPED (an older description format this script doesn't fully recognize -- would "
              f"have duplicated the tip/key text, so nothing was changed): {', '.join(l for _v, l in messy)}")
    regen_count = sum(1 for *_ignore, regenerated in changes if regenerated)
    print(f"{len(ids)} uploaded videos: {len(changes)} to change ({regen_count} with a freshly written description), "
          f"{up_to_date} already up to date, {skipped_status} skipped by --status {args.status}, "
          f"{len(missing)} no longer on YouTube.")
    for video_id, label, kind, _snippet, updated, regenerated in changes[:3]:
        tag = "REGENERATED" if regenerated else "reordered"
        print(f"\n--- e.g. {label} ({video_id}, {kind}, {tag}) would become ---\n{updated}\n---")
    if args.dry_run or not changes:
        return 0

    backup = PROJECT_ROOT / "reports" / f"support_text_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(
        json.dumps({vid: {"label": label, "snippet": snip} for vid, label, _kind, snip, _upd, _regen in changes},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Backed up the current snippets to {backup}")

    done = failed = 0
    for video_id, label, _kind, snippet, updated, regenerated in changes:
        snippet = dict(snippet, description=updated)
        try:
            youtube.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()
        except Exception as e:
            if is_quota_exceeded_error(e):
                print(f"\nYouTube's daily quota ran out after {done} update(s). Run this again later to finish the rest.")
                return 0
            failed += 1
            print(f"  FAILED {label} ({video_id}): {type(e).__name__}: {e}")
            continue
        done += 1
        print(f"  updated {label} ({video_id}){' (regenerated)' if regenerated else ''}")
    print(f"\nDone. Updated {done}, failed {failed}, already up to date {up_to_date}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
