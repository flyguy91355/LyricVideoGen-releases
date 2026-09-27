"""Re-checks the key of a song that is already made but NOT on YouTube (owner, 2026-09-26): the videos made before the key
check existed carry the old average-pitch guess, which was wrong for about a third of the songs checked. The saved chords
are re-read (no audio, no Demucs), the chord-based estimate and the second opinion are compared, and the result is recorded
exactly as a fresh run would (key_decision.py). A song already on YouTube is never touched, and a song whose key is already
confirmed is never asked about again (issue #7 review, F055: a re-run re-asked Claude for every settled song, and a
different answer demoted a confirmed song to "review"). Used by scripts/settle_keys.py."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .key_decision import load_decision, load_owner_key, settle_song_key
from .key_estimate import estimate_key_from_chords, parse_key, respell_chord_track
from .models import atomic_write_text, load_song, save_song
from .pipeline import HELD_MARKER
from .youtube_state import STATE_FILENAME


@dataclass
class RolloutResult:
    slug: str
    action: str            # skipped-uploaded | no-chords | already-settled | already-right | corrected | review
    old_key: str = ""
    new_key: str = ""
    detail: str = ""


def _set_aside(folder: Path) -> list[str]:
    """Renames every finished video in `folder` to <name>.previous.mp4 (the same convention a Redo hold uses), so the
    old video, with its wrong key burned in, is no longer a song's current video."""
    moved = []
    for video in sorted(folder.glob("*.mp4")):
        if video.name.endswith(".previous.mp4") or ".truncated" in video.stem:
            continue                                # already set aside (here, or by scripts/find_truncated_videos.py)
        video.replace(video.with_name(video.stem + ".previous.mp4"))
        moved.append(video.name)
    return moved


def _set_easy_version_aside(work_dir: Path) -> tuple[bool, str]:
    """Moves the song's whole `easychords/` folder (video, capo marker, shape chords) aside to easychords_prior_<time>/
    after its key changed: that EASY CHORD version was made for the old key (issue #7 review, F124: only its video used to
    be renamed, and the folder left behind kept the song off the "Generate EASY CHORD Versions" list for good). Once the
    song's video is made again it is offered there, and a rebuild copies the images already bought. An EASY version
    already on YouTube is left exactly where it is (its upload record lives in that folder). Returns (moved?, what
    happened -- for the report; "" when there is no EASY version)."""
    easy = work_dir / "easychords"
    if not easy.is_dir():
        return False, ""
    if (easy / STATE_FILENAME).exists():
        return False, "its EASY CHORD version is on YouTube and was left alone"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = work_dir / f"easychords_prior_{stamp}"
    n = 1
    while target.exists():
        n += 1
        target = work_dir / f"easychords_prior_{stamp}_{n}"
    try:
        easy.rename(target)
    except OSError as e:   # a file open in a player (Windows): the song must still be held with its reason, never half-done
        return False, (f"its EASY CHORD version (made for the old key) could not be moved aside ({type(e).__name__}: {e}); "
                       "it is set aside when the video is made again")
    return True, f"its EASY CHORD version (made for the old key) was moved to {target.name}; make it again after the video"


def settle_saved_song(work_dir: Path, anthropic_client, *, apply: bool, prefer_flats: bool = True) -> RolloutResult:
    """Settles one made-but-not-uploaded song's key. A key already confirmed (agreed at generation, by an earlier --apply,
    or the owner's Set Key) is used as it is, with no Claude call; only a song with no key decision, or one still in
    review, is asked about. `prefer_flats` is Settings.prefer_flats, for respelling a corrected song's chords."""
    work_dir = Path(work_dir)
    slug = work_dir.name
    if (work_dir / STATE_FILENAME).exists():
        return RolloutResult(slug, "skipped-uploaded")
    timed_path = work_dir / "lyrics_timed.json"
    song = load_song(timed_path)
    if estimate_key_from_chords(song.chord_track) is None:
        return RolloutResult(slug, "no-chords")
    old_key = song.chord_track.key
    owner = load_owner_key(work_dir)
    existing = load_decision(work_dir)
    settled = existing is not None and existing.confirmed and parse_key(existing.key) is not None
    if settled and (owner is None or parse_key(owner) == parse_key(existing.key)):
        if parse_key(old_key) == parse_key(existing.key):
            return RolloutResult(slug, "already-settled", old_key, existing.key)
        decision, track = existing, respell_chord_track(song.chord_track, existing.key, prefer_flats)
    else:
        artist = ""
        try:
            info = json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))
            artist = str(info.get("artist") or "")
        except (OSError, ValueError):
            pass
        decision, track = settle_song_key(
            work_dir, song.chord_track, song.title, artist, anthropic_client, save=apply, prefer_flats=prefer_flats,
        )
    if not decision.confirmed:
        return RolloutResult(slug, "review", old_key, decision.chord_key, decision.concern())
    if parse_key(old_key) == parse_key(decision.key):
        return RolloutResult(slug, "already-right", old_key, decision.key)
    detail = ""
    if apply:
        song.chord_track = track
        save_song(song, timed_path)
        _set_aside(work_dir)
        easy_moved, detail = _set_easy_version_aside(work_dir)
        atomic_write_text(work_dir / HELD_MARKER, json.dumps({
            "reason": f"Key check: the key was corrected from {old_key} to {decision.key}; the video has to be made again "
                      "(Render Anyway -- the images already bought are reused)."
                      + (" Then make the EASY CHORD version again (Generate EASY CHORD Versions)." if easy_moved else ""),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, indent=2))
    return RolloutResult(slug, "corrected", old_key, decision.key, detail)
