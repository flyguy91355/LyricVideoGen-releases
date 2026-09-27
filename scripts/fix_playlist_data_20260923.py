"""One-off cleanup, 2026-09-23, for two real bugs found by inspecting the owner's live channel:

1. "Crosby, Stills, Nash & Young" was split into three broken playlists ("Crosby", "Stills",
   "Nash & Young") because the old _split_artists() just split on every comma, and that band's own
   name happens to contain commas. Now fixed (a curated exception list, see youtube_playlists.py) --
   this script merges every video from the three broken playlists into one correct
   "Crosby, Stills, Nash & Young" playlist, and drops the three broken ids from the local cache so
   organize_video() never adds to them again. The three broken playlists are left in place on YouTube
   itself (not deleted) -- a deliberate, reversible choice; delete them by hand in Studio if wanted.

2. is_easy_key() was narrowed to the true open-chord keys (C, D, E, G, A major; A, D, E minor) after first
   counting every natural-tonic key, major or minor -- any song already added to the EASY CHORD (or
   3-/4-CHORD) playlist under the old rule needs to come back out. This script removes every song whose
   current detected key is NOT is_easy_key from those playlists. (Its first version only looked at F and B
   tonics and missed C minor and G minor -- issue #7 review, F148. scripts/backfill_channel_organization.py
   now reconciles this membership both ways for every upload, from the song's real key.)

Safe to re-run (idempotent): a video no longer a playlist member is skipped, not an error. --dry-run lists
what it would change without writing anything.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"

BROKEN_ARTIST_KEYS = ["artist:Crosby", "artist:Stills", "artist:Nash & Young"]
CORRECT_ARTIST_KEY = "artist:Crosby, Stills, Nash & Young"
CORRECT_ARTIST_TITLE = "Crosby, Stills, Nash & Young - Play Along Videos"
CORRECT_ARTIST_DESCRIPTION = "Every play-along lyrics & chords video on this channel by Crosby, Stills, Nash & Young."

EASY_CHORD_LIKE_KEYS = ["easy_chord", "three_chord", "four_chord"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="One-off 2026-09-23 playlist cleanup.")
    parser.add_argument("--dry-run", action="store_true", help="list what would change; write nothing")
    args = parser.parse_args(argv)

    from googleapiclient.discovery import build

    from lyricvideo.chord_theory import is_easy_key
    from lyricvideo.models import load_song
    from lyricvideo.youtube import remove_video_from_playlist
    from lyricvideo.youtube_auth import load_credentials
    from lyricvideo.youtube_playlist_state import load_playlist_ids, remove_playlist_ids
    from lyricvideo.youtube_playlists import add_video_to_playlist_with_retry, get_or_create_playlist
    from lyricvideo.youtube_state import load_youtube_state

    load_dotenv(PROJECT_ROOT / ".env")
    credentials = load_credentials()
    if credentials is None:
        raise SystemExit("Not connected to YouTube. Connect via the app's 'Connect to YouTube' button first.")
    youtube_client = build("youtube", "v3", credentials=credentials)

    channel_resp = youtube_client.channels().list(part="snippet", mine=True).execute()
    items = channel_resp.get("items", [])
    if not items:
        raise SystemExit("Connected, but no channel found for this account.")
    print(f"Connected channel: {items[0]['snippet']['title']}\n")

    playlist_ids = load_playlist_ids()

    # --- 1. merge the three broken Crosby/Stills/Nash & Young playlists into one correct one ---
    broken_ids = [playlist_ids[k] for k in BROKEN_ARTIST_KEYS if k in playlist_ids]
    if broken_ids and args.dry_run:
        print(f"Would merge {len(broken_ids)} broken Crosby/Stills/Nash & Young playlist(s) into '{CORRECT_ARTIST_TITLE}'.\n")
    elif broken_ids:
        correct_id = get_or_create_playlist(youtube_client, CORRECT_ARTIST_KEY, CORRECT_ARTIST_TITLE, CORRECT_ARTIST_DESCRIPTION)
        playlist_ids[CORRECT_ARTIST_KEY] = correct_id
        print(f"Merging into '{CORRECT_ARTIST_TITLE}' ({correct_id}):")
        moved = 0
        for broken_id in broken_ids:
            resp = youtube_client.playlistItems().list(part="contentDetails", playlistId=broken_id, maxResults=50).execute()
            for entry in resp.get("items", []):
                video_id = entry["contentDetails"]["videoId"]
                # A newly-created playlist can still 404 as playlistNotFound on the very next
                # playlistItems() call (real, documented propagation lag -- see
                # add_video_to_playlist_with_retry's own docstring); hit for real running this
                # script live, 2026-09-23, on this exact freshly-created merge-target playlist.
                add_video_to_playlist_with_retry(youtube_client, correct_id, video_id)
                moved += 1
        for key in BROKEN_ARTIST_KEYS:
            playlist_ids.pop(key, None)
        remove_playlist_ids(BROKEN_ARTIST_KEYS)     # only those keys -- every other cached id is left as it is
        print(f"  moved {moved} video(s); dropped {len(broken_ids)} broken playlist id(s) from the local cache "
              f"(the broken playlists themselves are left on YouTube, not deleted)\n")
    else:
        print("No broken Crosby/Stills/Nash & Young playlist ids found in the local cache -- nothing to merge.\n")

    # --- 2. remove any song whose key is not an easy (open-chord) key from the EASY CHORD / 3-/4-CHORD playlists ---
    easy_like_ids = {key: playlist_ids[key] for key in EASY_CHORD_LIKE_KEYS if key in playlist_ids}
    if not easy_like_ids:
        print("No EASY CHORD / 3-/4-CHORD playlist ids found -- nothing to clean up.")
        return

    removed, checked = 0, 0
    for work_dir in sorted(WORK_DIR.iterdir()):
        if not work_dir.is_dir():
            continue
        state = load_youtube_state(work_dir)
        if state is None:
            continue
        try:
            song = load_song(work_dir / "lyrics_timed.json")
        except Exception:
            continue
        key = song.chord_track.key or ""
        if is_easy_key(key):
            continue  # still qualifies (the old rule and the new one agree for every easy key)
        checked += 1
        for playlist_key, playlist_id in easy_like_ids.items():
            if args.dry_run:
                print(f"  {work_dir.name} ({key or 'no key'}): would be taken out of {playlist_key} if it is in it")
                continue
            if remove_video_from_playlist(youtube_client, playlist_id, state.video_id):
                print(f"  {work_dir.name} ({key}): removed from {playlist_key}")
                removed += 1

    print(f"\nChecked {checked} song(s) not in an easy key; removed {removed} playlist membership(s) that no longer qualify.")


if __name__ == "__main__":
    main()
