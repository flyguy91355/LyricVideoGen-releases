"""Makes a song folder's thumbnail when it has none (owner, 2026-10-04) -- the one place that knows where a folder's title,
artist and lyrics live, shared by the render step (pipeline.py), the upload (youtube_schedule.py) and
scripts/backfill_thumbnails.py. An EASY CHORD folder reuses its song's picture with an EASY CHORDS tag (no new picture)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import load_song, original_song_dir
from .thumbnail import (
    THUMBNAIL_FILE, compose_from_saved_background, compose_thumbnail, generate_thumbnail, pick_song_image, THUMBNAIL_BG_FILE,
)

log = logging.getLogger("playalongvideoproduction")


def _title_and_artist(work_dir: Path) -> tuple[str, str]:
    info = {}
    try:
        info = json.loads((Path(work_dir) / "song_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    title = str(info.get("title") or "").strip()
    artist = str(info.get("artist") or "").strip()
    if not title:
        try:
            title = load_song(Path(work_dir) / "lyrics_timed.json").title
        except Exception:
            title = ""
    return title, artist


def _lyrics_of(work_dir: Path) -> str:
    try:
        return "\n".join(line.text for line in load_song(Path(work_dir) / "lyrics_timed.json").lines)
    except Exception:
        return ""


def _easy_badge(work_dir: Path) -> str:
    """"EASY CHORDS", or "EASY CHORDS · CAPO 2" when the folder's marker records the capo fret."""
    from .chord_theory import load_easy_chord_capo_marker
    fret = (load_easy_chord_capo_marker(work_dir) or {}).get("capo_fret")
    return f"EASY CHORDS · CAPO {int(fret)}" if isinstance(fret, (int, float)) and fret else "EASY CHORDS"


def _chord_labels(work_dir: Path) -> list[str]:
    """Every chord of the folder's own saved chord track (an EASY folder: its capo shapes), in order of first appearance."""
    try:
        from .pipeline import ordered_unique_chords
        return list(ordered_unique_chords(load_song(Path(work_dir) / "lyrics_timed.json").chord_track))
    except Exception:
        return []


def ensure_thumbnail(
    work_dir: Path, anthropic_client, replicate_token: str, *, font_path: str | None = None, http_client=None,
    show_chords: bool = True, use_song_images: bool = True,
) -> Path | None:
    """The folder's thumbnail.jpg, made now if it has none; None when nothing could be made (missing title, no API keys, a
    failed image). Never raises. A folder that already has one keeps it (a Redo does not buy another)."""
    work_dir = Path(work_dir)
    existing = work_dir / THUMBNAIL_FILE
    if existing.exists():
        return existing
    try:
        title, artist = _title_and_artist(work_dir)
        if not title:
            return None
        song_dir = original_song_dir(work_dir)
        if Path(song_dir) != work_dir:                      # an EASY CHORD version: its song's picture, its own tag
            labels = _chord_labels(work_dir) if show_chords else None
            made = compose_from_saved_background(work_dir, song_dir, title=_title_and_artist(song_dir)[0] or title,
                                                 artist=artist or _title_and_artist(song_dir)[1],
                                                 font_path=font_path, chord_labels=labels, sub_tag=_easy_badge(work_dir))
            if made is not None:
                return made
            if ensure_thumbnail(song_dir, anthropic_client, replicate_token, font_path=font_path, http_client=http_client,
                                show_chords=show_chords, use_song_images=use_song_images) is None:
                return None
            return compose_from_saved_background(work_dir, song_dir, title=title, artist=artist,
                                                 font_path=font_path, chord_labels=labels, sub_tag=_easy_badge(work_dir))
        if anthropic_client is None or not replicate_token:
            return None
        if use_song_images:
            picked, _cost = pick_song_image(anthropic_client, title, artist, _lyrics_of(work_dir), work_dir / "images")
            if picked is not None:
                from PIL import Image
                with Image.open(picked) as img:
                    data = img.convert("RGB")
                    data.load()
                data.save(work_dir / THUMBNAIL_BG_FILE, "PNG")
                return compose_thumbnail(data, title, artist, work_dir / THUMBNAIL_FILE, font_path=font_path,
                                         chord_labels=_chord_labels(work_dir) if show_chords else None)
        kwargs = {"http_client": http_client} if http_client is not None else {}
        return generate_thumbnail(
            work_dir, anthropic_client, replicate_token, title=title, artist=artist, lyrics=_lyrics_of(work_dir),
            font_path=font_path, chord_labels=_chord_labels(work_dir) if show_chords else None, **kwargs,
        ).path
    except Exception as e:
        log.warning("Could not make a thumbnail for %s: %s: %s", work_dir.name, type(e).__name__, e)
        return None
