"""Calibration contact sheet for the shared image library (spec 2026-09-25, section 5).

For a song the owner already knows: what would the library have offered instead of each picture he paid for?
Claude writes each prompt exactly as the images stage would (cached, so trying other thresholds is free); the
song's OWN pictures are hidden from the library for the run (leave-one-out), so the sheet shows what a different
song's picture would have been. The owner judges it, and the default match score is set from what he accepts."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
from PIL import Image

from .image_library import ImageLibrary, LibraryMatch, content_id
from .layout import instrumental_image_captions
from .models import Song, line_hash, load_song

THRESHOLDS = [round(0.16 + 0.02 * i, 2) for i in range(15)]     # 0.16 ... 0.44: the Settings slider's whole range
_THUMB_SIZE = (384, 216)


@dataclass
class PreviewRow:
    text: str
    prompt: str
    own_image: Path | None          # the picture actually bought for this line/caption, if it is still on disk
    query: np.ndarray               # the prompt's CLIP text embedding
    match: LibraryMatch | None      # the library's closest picture (this song's own pictures excluded), any score
    hidden_ids: frozenset[str] = frozenset()    # this song's own pictures, hidden from the library for the run


def production_offers(
    rows: Sequence[PreviewRow], library: ImageLibrary, own_ids: set[str], threshold: float,
) -> list[LibraryMatch | None]:
    """What the images stage would really hand each row at `threshold`: the same once-per-song rule production
    applies (LibrarySession.used_ids -- a picture serves the FIRST line that wants it; later lines get the runner-up
    or buy), so the contact sheet never shows an offer production would not make (issue #7 review)."""
    used = set(own_ids)
    offers: list[LibraryMatch | None] = []
    for row in rows:
        match = library.best_match(row.query, used, threshold)
        if match is not None:
            used.add(match.image_id)
        offers.append(match)
    return offers


def cached_prompt_maker(cache_path: Path, make_prompt: Callable[[str], str]) -> Callable[[str], str]:
    cache: dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def prompt_for(text: str) -> str:
        if text not in cache:
            prompt_for.calls += 1
            cache[text] = make_prompt(text)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        return cache[text]

    prompt_for.calls = 0
    return prompt_for


def song_keys(song: Song) -> list[str]:
    """The distinct lyric lines (first-seen order) then the instrumental captions the images stage would generate."""
    from .pipeline import song_end_time      # imported here: pipeline pulls in the whole heavy stack

    keys: list[str] = []
    for line in song.lines:
        if line.text not in keys:
            keys.append(line.text)
    for caption in instrumental_image_captions(song.lines, song.chord_track, song_end_time(song)):
        if caption not in keys:
            keys.append(caption)
    return keys


def build_rows(
    song_dir: Path, library: ImageLibrary, embedder, prompt_for: Callable[[str], str],
) -> tuple[list[PreviewRow], set[str]]:
    song = load_song(song_dir / "lyrics_timed.json")
    images_dir = song_dir / "images"
    own_ids = {content_id(p) for p in images_dir.glob("*.png")} if images_dir.is_dir() else set()
    keys = song_keys(song)
    prompts = [prompt_for(key) for key in keys]
    queries = embedder.embed_text(prompts) if prompts else []
    rows = []
    hidden = frozenset(own_ids)
    for key, prompt, query in zip(keys, prompts, queries):
        own = images_dir / f"{line_hash(key)}.png"
        rows.append(PreviewRow(
            text=key, prompt=prompt, own_image=own if own.exists() else None, query=query,
            match=library.best_match(query, own_ids, -1.0), hidden_ids=hidden,
        ))
    return rows, own_ids


def reuse_counts(
    rows: Sequence[PreviewRow], library: ImageLibrary, own_ids: set[str], thresholds: Sequence[float],
) -> list[tuple[float, int]]:
    """How many lines the images stage would reuse a library picture for at each threshold, with the same
    once-per-song rule production applies (a picture serves one line only)."""
    return [
        (threshold, sum(offer is not None for offer in production_offers(rows, library, own_ids, threshold)))
        for threshold in thresholds
    ]


def _thumb(source: Path, dest: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail(_THUMB_SIZE)
        image.save(dest, "JPEG", quality=80)


def write_report(
    out_dir: Path, title: str, rows: Sequence[PreviewRow], counts: Sequence[tuple[float, int]],
    library: ImageLibrary, current_threshold: float,
) -> Path:
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    esc = html.escape
    table = "".join(
        f"<tr><td>{threshold:.2f}</td><td>{reused} of {len(rows)}</td></tr>" for threshold, reused in counts
    )
    # The verdicts follow production exactly (once per song), so they always agree with the table above.
    hidden = set().union(*(row.hidden_ids for row in rows)) if rows else set()
    offers = production_offers(rows, library, hidden, current_threshold)
    body = []
    for n, (row, offer) in enumerate(zip(rows, offers)):
        own_cell = "<em>(file not on disk)</em>"
        if row.own_image is not None:
            _thumb(row.own_image, thumbs / f"own{n}.jpg")
            own_cell = f'<img src="thumbs/own{n}.jpg">'
        offer_cell = "<em>(library empty)</em>"
        verdict = ""
        shown = offer if offer is not None else row.match
        if shown is not None:
            _thumb(library.image_path(shown.image_id), thumbs / f"lib{n}.jpg")
            offer_cell = f'<img src="thumbs/lib{n}.jpg"><br>score {shown.score:.2f}'
            verdict = "reused" if offer is not None else "would buy"
            if row.match is not None and offer is not None and offer.image_id != row.match.image_id:
                offer_cell += (
                    f"<div class='note'>its closest picture (score {row.match.score:.2f}) already went to an "
                    "earlier line, so this runner-up is used</div>"
                )
            elif offer is None and row.match.score >= current_threshold:
                offer_cell += "<div class='note'>already used by an earlier line (a picture serves one line)</div>"
        body.append(
            f"<tr class='{verdict.replace(' ', '-')}'><td>{esc(row.text)}<div class='prompt'>{esc(row.prompt)}</div></td>"
            f"<td>{own_cell}</td><td>{offer_cell}</td><td>{esc(verdict)}</td></tr>"
        )
    page = f"""<!doctype html><meta charset="utf-8"><title>Library preview: {esc(title)}</title>
<style>
body {{ font: 14px system-ui, sans-serif; margin: 24px; background: #111; color: #eee; }}
table {{ border-collapse: collapse; }} td, th {{ border: 1px solid #333; padding: 6px 10px; vertical-align: top; }}
img {{ display: block; }} .prompt {{ color: #9aa; font-size: 12px; max-width: 420px; margin-top: 4px; }}
.note {{ color: #9aa; font-size: 12px; max-width: 384px; margin-top: 4px; }}
tr.reused td:last-child {{ color: #7ddc7d; }} tr.would-buy td:last-child {{ color: #f0a339; }}
</style>
<h1>{esc(title)}</h1>
<p>Left: the picture actually bought for that line. Right: the picture the library would really hand that line
(this song's own pictures hidden). Current match score: <b>{current_threshold:.2f}</b> -- a line is "reused" when a
picture scores at least that AND no earlier line already took it (one library picture serves one line per song,
exactly as when a video is made); otherwise the closest picture is shown and the line "would buy".</p>
<h2>How many lines would reuse a library picture at each match score</h2>
<table><tr><th>match score</th><th>lines reused</th></tr>{table}</table>
<h2>Line by line</h2>
<table><tr><th>Line / prompt</th><th>You bought</th><th>Library offers</th><th></th></tr>{"".join(body)}</table>
"""
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    return index
