"""Makes a song folder's thumbnail when it has none (owner, 2026-10-04) -- the one place that knows where a folder's title,
artist and lyrics live, shared by the render step (pipeline.py), the upload (youtube_schedule.py) and
scripts/backfill_thumbnails.py. An EASY CHORD folder reuses its song's picture with an EASY CHORDS tag (no new picture)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import load_song, original_song_dir
from .thumbnail import (
    ARTIST_MAX_WIDTH, ARTIST_SIZE, THUMBNAIL_FILE, THUMBNAIL_OWN_FILE, THUMBNAIL_SET_FILE, _bold_font, compose_from_saved_background,
    compose_thumbnail, generate_thumbnail, pick_song_image, THUMBNAIL_BG_FILE,
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
                (work_dir / THUMBNAIL_OWN_FILE).write_text("1", encoding="utf-8")
                return compose_thumbnail(data, title, artist, work_dir / THUMBNAIL_FILE, font_path=font_path,
                                         chord_labels=_chord_labels(work_dir) if show_chords else None, reframe=False)
        kwargs = {"http_client": http_client} if http_client is not None else {}
        return generate_thumbnail(
            work_dir, anthropic_client, replicate_token, title=title, artist=artist, lyrics=_lyrics_of(work_dir),
            font_path=font_path, chord_labels=_chord_labels(work_dir) if show_chords else None, **kwargs,
        ).path
    except Exception as e:
        log.warning("Could not make a thumbnail for %s: %s: %s", work_dir.name, type(e).__name__, e)
        return None


def _shown_title_and_artist(work_dir: Path) -> tuple[str, str]:
    """What the folder's thumbnail shows: an EASY CHORD folder its song's title, and its own artist else its song's."""
    title, artist = _title_and_artist(work_dir)
    song_dir = Path(original_song_dir(work_dir))
    if song_dir != Path(work_dir):
        song_title, song_artist = _title_and_artist(song_dir)
        title, artist = song_title or title, artist or song_artist
    return title, artist


def redraw_thumbnail(work_dir: Path, *, font_path: str | None = None, show_chords: bool = True) -> Path | None:
    """Draws the folder's thumbnail.jpg AGAIN from the picture it already has (thumbnail_bg.png; an EASY CHORD folder: its
    song's) with the current lettering -- no new picture, no Claude call, no cost. None when there is no saved picture or
    no title. Used to give videos already on YouTube a layout fix (owner, 2026-11-15: a long artist on two lines)."""
    work_dir = Path(work_dir)
    title, artist = _shown_title_and_artist(work_dir)
    if not title:
        return None
    song_dir = Path(original_song_dir(work_dir))
    sub_tag = _easy_badge(work_dir) if song_dir != work_dir else None
    return compose_from_saved_background(work_dir, song_dir, title=title, artist=artist, font_path=font_path,
                                         chord_labels=_chord_labels(work_dir) if show_chords else None, sub_tag=sub_tag)


_REDRAWN_KEY = "artist_two_lines"


def needs_artist_redraw(work_dir: Path, font_path: str | None = None) -> bool:
    """True when the folder's thumbnail was made with its artist shrunk onto one line (too wide at ARTIST_SIZE) and has
    not been redrawn and set again yet (thumbnail_set.json records it, so a re-run continues where it stopped)."""
    from PIL import Image, ImageDraw
    _title, artist = _shown_title_and_artist(Path(work_dir))
    text = (artist or "").upper().strip()
    if not text:
        return False
    if ImageDraw.Draw(Image.new("RGB", (8, 8))).textlength(text, font=_bold_font(ARTIST_SIZE, font_path)) <= ARTIST_MAX_WIDTH:
        return False
    try:
        return not json.loads((Path(work_dir) / THUMBNAIL_SET_FILE).read_text(encoding="utf-8")).get(_REDRAWN_KEY)
    except (OSError, ValueError, AttributeError):
        return True


def mark_artist_redrawn(work_dir: Path, video_id: str) -> None:
    (Path(work_dir) / THUMBNAIL_SET_FILE).write_text(json.dumps({"video_id": video_id, _REDRAWN_KEY: True}), encoding="utf-8")
