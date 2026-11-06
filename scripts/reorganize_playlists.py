"""Reworks the channel's artist and genre playlists to the owner's 2026-11-05 rules:

- an artist playlist only for an artist with 2+ uploaded songs (a song and its EASY CHORD version are one song);
- genre playlists from the one fixed list (youtube_metadata.GENRES), every song -- real or AI -- in each genre it fits
  (up to 3, plus Rock for a rock-family song), a genre playlist made only once a video goes in it;
- the All, EASY CHORD, 3 CHORD and 4 CHORD playlists are kept as they are.

  .venv/bin/python scripts/reorganize_playlists.py                    # the plan: what would change (YouTube read-only)
  .venv/bin/python scripts/reorganize_playlists.py --apply            # do it, up to --max-units of quota this run
  .venv/bin/python scripts/reorganize_playlists.py --apply --max-units 6000

The plan asks Claude for the genres of every song that has none saved yet and saves them in its song_info.json
("genres") -- the only thing it writes -- so the plan shows the real sort. --apply then: (1) deletes every artist playlist
whose artist has fewer than 2 uploaded songs (the videos stay on the channel); (2) puts every uploaded video in its
playlists (organize_video), remembering which are done in ~/.playalongvideoproduction/playlist_rework_done.json;
(3) once every video is done, deletes the old genre playlists whose names are not on the fixed list. It stops at the
--max-units budget (default 2500: uploads need ~1,600 units each out of YouTube's 10,000 a day) or at the quota; run
it again the next day to carry on where it stopped.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backfill_channel_organization import DEFAULT_KEY_NOTES, load_key_overrides  # noqa: E402

from lyricvideo.youtube import delete_playlist, is_quota_exceeded_error, list_own_playlists, video_exists  # noqa: E402
from lyricvideo.youtube_metadata import GENRES  # noqa: E402
from lyricvideo.youtube_playlist_state import (  # noqa: E402
    CREDENTIALS_DIR, load_genres, load_playlist_ids, remove_playlist_ids,
)
from lyricvideo.youtube_playlists import (  # noqa: E402
    MIN_SONGS_FOR_ARTIST_PLAYLIST, PLAYLIST_TITLE_SUFFIX, genres_for, organize_video, song_folder,
    uploaded_videos_by_artist,
)
from lyricvideo.youtube_state import atomic_write_text, uploaded_song_dirs  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"
PROGRESS_FILE = CREDENTIALS_DIR / "playlist_rework_done.json"
DEFAULT_MAX_UNITS = 2500
_READ_METHODS = {"list"}            # 1 unit; every insert/update/delete costs 50


class BudgetSpent(Exception):
    pass


class QuotaBudget:
    """Wraps a YouTube API client and counts the quota units of the calls made through it, raising BudgetSpent BEFORE a
    call that would go over max_units. read_only=True refuses every call that would change anything."""

    def __init__(self, client, max_units: int, read_only: bool = False):
        self._client = client
        self.max_units = max_units
        self.read_only = read_only
        self.used = 0

    def charge(self, method: str) -> None:
        if self.read_only and method not in _READ_METHODS:
            raise RuntimeError(f"the plan makes no YouTube changes (refused a {method} call)")
        units = 1 if method in _READ_METHODS else 50
        if self.used + units > self.max_units:
            raise BudgetSpent(f"{self.used} of the {self.max_units} quota units allowed this run are used")
        self.used += units

    def __getattr__(self, resource_name):
        make_resource = getattr(self._client, resource_name)
        return lambda *a, **k: _BudgetedResource(self, make_resource(*a, **k))


class _BudgetedResource:
    def __init__(self, budget: QuotaBudget, resource):
        self._budget = budget
        self._resource = resource

    def __getattr__(self, method_name):
        method = getattr(self._resource, method_name)

        def call(*a, **k):
            self._budget.charge(method_name)
            return method(*a, **k)
        return call


def sort_playlists(playlists: list[dict], songs_per_artist: dict[str, int], old_genre_names: set[str]) -> dict:
    """Splits the channel's own playlists into what the rework does with each. Only "<name> - Play Along Videos"
    playlists (this app's artist and genre ones) are ever touched; everything else is "other"."""
    out = {"delete_artist": [], "keep_artist": [], "fixed_genre": [], "old_genre": [], "other": []}
    for playlist in playlists:
        title = playlist["title"]
        if not title.endswith(PLAYLIST_TITLE_SUFFIX):
            out["other"].append(playlist)
            continue
        name = title[: -len(PLAYLIST_TITLE_SUFFIX)]
        if name in GENRES:
            out["fixed_genre"].append(playlist)
        elif name in old_genre_names:
            out["old_genre"].append(playlist)
        elif songs_per_artist.get(name, 0) >= MIN_SONGS_FOR_ARTIST_PLAYLIST:
            out["keep_artist"].append(playlist)
        else:
            out["delete_artist"].append(playlist)
    return out


def _load_done(path: Path) -> set[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _save_done(path: Path, done: set[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(Path(path), json.dumps(sorted(done)))


def _forget_playlist_ids(playlist_ids: set[str]) -> None:
    """Drops the app's cached ids that point at these (deleted) playlists."""
    stale_keys = [key for key, pid in load_playlist_ids().items() if pid in playlist_ids]
    if stale_keys:
        remove_playlist_ids(stale_keys)


def _delete_playlists(youtube_client, playlists: list[dict], label: str) -> int:
    for playlist in playlists:
        delete_playlist(youtube_client, playlist["id"])
        _forget_playlist_ids({playlist["id"]})          # at once: a budget stop must not leave the cache pointing at it
        print(f"  deleted {label}: {playlist['title']}")
    return len(playlists)


def _print_plan(sorted_: dict, by_artist: dict, genres_by_song: dict[str, list[str]], videos: list) -> None:
    print(f"\nArtist playlists to DELETE (artist has fewer than {MIN_SONGS_FOR_ARTIST_PLAYLIST} songs): "
          f"{len(sorted_['delete_artist'])}")
    for p in sorted_["delete_artist"]:
        print(f"    {p['title']}  ({p['count']} video(s))")
    have = {p["title"][: -len(PLAYLIST_TITLE_SUFFIX)] for p in sorted_["keep_artist"]}
    wanted = sorted(a for a, songs in by_artist.items() if len(songs) >= MIN_SONGS_FOR_ARTIST_PLAYLIST)
    print(f"\nArtist playlists KEPT: {len(have)}; to CREATE: {', '.join(a for a in wanted if a not in have) or 'none'}")

    per_genre: dict[str, int] = {}
    for _slug, path, _state in videos:
        for genre in genres_by_song.get(song_folder(path).name, []):
            per_genre[genre] = per_genre.get(genre, 0) + 1
    existing = {p["title"][: -len(PLAYLIST_TITLE_SUFFIX)] for p in sorted_["fixed_genre"]}
    print("\nGenre playlists (videos in each):")
    for genre in GENRES:
        if per_genre.get(genre):
            print(f"    {genre:<15} {per_genre[genre]:4d}  {'(exists)' if genre in existing else '(new)'}")
    empty = [g for g in GENRES if not per_genre.get(g)]
    if empty:
        print(f"    no songs, so no playlist: {', '.join(empty)}")
    unsorted = sorted(s for s, g in genres_by_song.items() if not g)
    if unsorted:
        print(f"    songs with no genre answer (left out of genre playlists): {', '.join(unsorted)}")
    print(f"\nOld genre playlists to DELETE once every video is sorted: {len(sorted_['old_genre'])}")
    for p in sorted_["old_genre"]:
        print(f"    {p['title']}  ({p['count']} video(s))")
    memberships = sum(per_genre.values())
    estimate = 50 * (len(sorted_["delete_artist"]) + len(sorted_["old_genre"]) + memberships) + 30 * len(videos)
    print(f"\nRough quota for all of it: up to ~{estimate:,} units (YouTube allows 10,000 a day, uploads included).")


def run(youtube_client, anthropic_client, work_root: Path, apply: bool, progress_path: Path = PROGRESS_FILE) -> dict:
    """The plan, and with apply=True the changes. Returns {"sorted": ..., "finished": bool}."""
    videos = uploaded_song_dirs(work_root)
    by_artist = uploaded_videos_by_artist(work_root)
    songs_per_artist = {artist: len(songs) for artist, songs in by_artist.items()}
    old_genres = set(load_genres()) | {k[len("genre:"):] for k in load_playlist_ids() if k.startswith("genre:")}
    sorted_ = sort_playlists(list_own_playlists(youtube_client), songs_per_artist, old_genres)

    genres_by_song: dict[str, list[str]] = {}
    for _slug, path, _state in videos:
        song_dir = song_folder(path)
        if song_dir.name not in genres_by_song:
            genres_by_song[song_dir.name] = genres_for(anthropic_client, song_dir)
            print(f"  {song_dir.name}: {', '.join(genres_by_song[song_dir.name]) or '(no answer)'}")
    _print_plan(sorted_, by_artist, genres_by_song, videos)
    result = {"sorted": sorted_, "finished": False}
    if not apply:
        print("\nPlan only -- nothing changed on YouTube. Run with --apply to do it.")
        return result

    key_overrides = load_key_overrides(DEFAULT_KEY_NOTES)
    done = _load_done(progress_path)
    try:
        print("\nStep 1: deleting the one-song artist playlists...")
        _delete_playlists(youtube_client, sorted_["delete_artist"], "artist playlist")
        print("\nStep 2: putting every video in its playlists...")
        for slug, path, state in videos:
            if state.video_id in done:
                continue
            if not video_exists(youtube_client, state.video_id):
                print(f"  {slug}: video {state.video_id} is gone from YouTube -- skipped")
            else:
                removed = organize_video(youtube_client, anthropic_client, path, key_override=key_overrides.get(slug),
                                         include_artist_catalog=False) or []
                print(f"  {slug}: sorted" + (f" (taken out of: {', '.join(removed)})" if removed else ""))
            done.add(state.video_id)
            _save_done(progress_path, done)
        print("\nStep 3: deleting the old genre playlists...")
        _delete_playlists(youtube_client, sorted_["old_genre"], "old genre playlist")
    except BudgetSpent as e:
        print(f"\nStopped: {e}. Run this again (e.g. tomorrow) to carry on where it stopped.")
        return result
    except Exception as e:
        if is_quota_exceeded_error(e):
            print("\nStopped: YouTube's daily quota ran out. Run this again after midnight Pacific to carry on.")
            return result
        raise
    result["finished"] = True
    print("\nDone: the playlists follow the new rules.")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Rework the channel's artist and genre playlists (plan by default).")
    parser.add_argument("--apply", action="store_true", help="make the changes on YouTube")
    parser.add_argument("--max-units", type=int, default=DEFAULT_MAX_UNITS,
                        help=f"quota units this run may spend (default {DEFAULT_MAX_UNITS})")
    args = parser.parse_args(argv)

    import anthropic
    from dotenv import load_dotenv
    from googleapiclient.discovery import build

    from lyricvideo.youtube_auth import load_credentials

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(f"ANTHROPIC_API_KEY not found in {PROJECT_ROOT / '.env'}.")
    credentials = load_credentials()
    if credentials is None:
        raise SystemExit("Not connected to YouTube. Connect via the app's 'Connect to YouTube' button first.")
    youtube_client = QuotaBudget(build("youtube", "v3", credentials=credentials),
                                 args.max_units if args.apply else 500, read_only=not args.apply)
    run(youtube_client, anthropic.Anthropic(), WORK_DIR, apply=args.apply)
    print(f"(YouTube quota used this run: {youtube_client.used} units)")


if __name__ == "__main__":
    main()
