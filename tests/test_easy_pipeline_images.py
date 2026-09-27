"""Issue #7 review (F053/F056/F068-F071/F133): an instrumental stretch's background picture is filed under its chord's NAME
(layout.instrumental_caption), so a chord renamed without changing -- shifted by a capo in an EASY CHORD version, or respelled
to the settled key's sharp/flat convention -- looked up a picture nobody made, and every such stretch showed one arbitrary
picture; and the EASY version's copy of its song's pictures was made once and never refreshed. Song words are invented."""

import json
from pathlib import Path

import pytest

from lyricvideo import pipeline
from lyricvideo.chord_theory import transpose_chord_track
from lyricvideo.key_decision import KeyDecision, save_decision
from lyricvideo.layout import (
    build_image_timeline, enharmonic_instrumental_captions, fill_missing_instrumental_images, instrumental_caption,
    instrumental_caption_sources, instrumental_image_captions,
)
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, line_hash, load_song, save_song

WORDS = ["amber", "lanterns", "drift", "over", "quiet", "water"]


def _line(words, start):
    return LyricLine(
        words=[Word(w, start + 0.6 * i, start + 0.6 * i + 0.5) for i, w in enumerate(words)],
        start_time=start, end_time=start + 0.6 * len(words),
    )


def _track(labels, key, seconds=4.0):
    return ChordTrack(events=[ChordEvent(i * seconds, (i + 1) * seconds, label) for i, label in enumerate(labels)],
                      key=key, bpm=100.0)


def _picture(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def _make_song(work_root: Path, name="paper-lanterns", key="Eb major",
               labels=("Eb", "Ab", "Bb", "Cm", "Eb", "Eb", "Ab", "Bb", "Cm", "Eb")) -> Path:
    """A rendered song with a settled key: an intro, one sung line at 16-19.6 s, an outro; a picture for the line and for
    every instrumental caption its own chords need (each picture's bytes name its caption)."""
    song_dir = work_root / name
    song_dir.mkdir(parents=True)
    title = "Paper Lanterns"
    song = Song(title=title, audio_path=str(song_dir / "song.mp3"), lines=[_line(WORDS, 16.0)], chord_track=_track(labels, key))
    save_song(song, song_dir / "lyrics_timed.json")
    (song_dir / "song_info.json").write_text(json.dumps({"title": title, "artist": "Nobody", "duration": 40.0}), encoding="utf-8")
    (song_dir / "song.mp3").write_bytes(b"fake audio")
    save_decision(song_dir, KeyDecision(status="confirmed", key=key, source="agreed", chord_key=key))
    _picture(song_dir / "images" / f"{line_hash(song.lines[0].text)}.png", "line picture")
    for caption in instrumental_image_captions(song.lines, song.chord_track, 40.0):
        _picture(song_dir / "images" / f"{line_hash(caption)}.png", caption)
    return song_dir


@pytest.fixture
def renders(monkeypatch):
    """No real render: every assemble_video call is recorded (and writes its video, as the real one does)."""
    calls = []

    def fake_assemble(lines, chord_track, image_dir, audio_path, out_path, font_path, *args, **kwargs):
        calls.append({"lines": lines, "chord_track": chord_track, "image_dir": Path(image_dir), "out_path": Path(out_path),
                      **kwargs})
        Path(out_path).write_bytes(b"rendered video")

    monkeypatch.setattr(pipeline, "assemble_video", fake_assemble)
    monkeypatch.setattr(pipeline, "default_font", lambda: "font.ttf")
    return calls


# --- the caption helpers ---------------------------------------------------------------------------------------------

def test_a_sharp_or_flat_chord_caption_has_its_other_spelling_and_nothing_else_does():
    assert enharmonic_instrumental_captions(instrumental_caption("A#")) == [instrumental_caption("Bb")]
    assert enharmonic_instrumental_captions(instrumental_caption("Bb")) == [instrumental_caption("A#")]
    assert enharmonic_instrumental_captions(instrumental_caption("Gbm7")) == [instrumental_caption("F#m7")]
    assert enharmonic_instrumental_captions(instrumental_caption("C")) == []            # a natural root: one name only
    assert enharmonic_instrumental_captions(instrumental_caption(None)) == []           # the generic "[Instrumental]"
    assert enharmonic_instrumental_captions(instrumental_caption("N")) == []
    assert enharmonic_instrumental_captions("amber lanterns drift") == []               # a lyric line


def test_each_capo_caption_names_the_songs_own_chord_it_was_shifted_from():
    original = _track(["Eb", "D", "Cm", "Eb"], "Eb major")
    capo = transpose_chord_track(original, 1, "D")                                       # Eb -> D, D -> C#, Cm -> Bm

    assert instrumental_caption_sources(original, capo) == {
        instrumental_caption("D"): [instrumental_caption("Eb")],
        instrumental_caption("C#"): [instrumental_caption("D")],
        instrumental_caption("Bm"): [instrumental_caption("Cm")],
    }
    assert instrumental_caption_sources(original, _track(["D"], "D major")) == {}       # not made event by event


# --- a respelled chord keeps its picture (F053) ------------------------------------------------------------------------

def test_a_chord_respelled_for_the_settled_key_gets_the_picture_it_already_has(tmp_path):
    lines = [_line(WORDS, 8.0)]
    images = tmp_path / "images"
    _picture(images / f"{line_hash(instrumental_caption('A#'))}.png", "the A# picture")
    respelled = _track(["F", "Bb"], "F major")                                           # was F, A# before the key check
    _picture(images / f"{line_hash(instrumental_caption('F'))}.png", "the F picture")
    _picture(images / f"{line_hash(lines[0].text)}.png", "the line's picture")

    filled = fill_missing_instrumental_images(images, lines, respelled, 16.0)

    assert filled == [instrumental_caption("Bb")]
    assert (images / f"{line_hash(instrumental_caption('Bb'))}.png").read_bytes() == b"the A# picture"
    assert fill_missing_instrumental_images(images, lines, respelled, 16.0) == []        # nothing left to fill
    for segment in build_image_timeline(lines, respelled, 16.0):
        assert (images / f"{segment.image_key}.png").exists()


def test_a_picture_kept_in_a_backup_folder_is_found_under_the_other_spelling_too(tmp_path):
    images, backup = tmp_path / "images", tmp_path / "images_backup_1"
    _picture(backup / f"{line_hash(instrumental_caption('G#m'))}.png", "backed-up picture")

    fill_missing_instrumental_images(images, [_line(WORDS, 8.0)], _track(["Abm", "Abm"], "Ab minor"), 16.0,
                                     source_dirs=[backup])

    assert (images / f"{line_hash(instrumental_caption('Abm'))}.png").read_bytes() == b"backed-up picture"


def test_a_capo_name_that_is_also_one_of_the_songs_own_chords_gets_the_right_picture(tmp_path):
    """An Eb-major song with its own D chord: at capo 1 the song's Eb is fretted as D and its D as C#. The EASY version's
    folder mirrors the song's pictures, so a "D" picture is already there -- the song's D, the wrong chord for the EASY
    version's D; and the song's own D picture must still become the EASY version's C#."""
    original = _track(["Eb", "D", "Eb", "D"], "Eb major")
    capo = transpose_chord_track(original, 1, "D")
    song_images, easy_images = tmp_path / "song" / "images", tmp_path / "easy" / "images"
    for label in ("Eb", "D"):
        _picture(song_images / f"{line_hash(instrumental_caption(label))}.png", f"picture of the song's {label}")
        _picture(easy_images / f"{line_hash(instrumental_caption(label))}.png", f"picture of the song's {label}")

    fill_missing_instrumental_images(
        easy_images, [], capo, 16.0, source_dirs=[song_images],
        source_captions=instrumental_caption_sources(original, capo), replace_from_sources=True,
    )

    assert (easy_images / f"{line_hash(instrumental_caption('D'))}.png").read_bytes() == b"picture of the song's Eb"
    assert (easy_images / f"{line_hash(instrumental_caption('C#'))}.png").read_bytes() == b"picture of the song's D"


# --- the EASY CHORD version's pictures (F056/F068/F071/F133) -----------------------------------------------------------

def test_every_picture_the_easy_versions_render_looks_up_is_its_songs_own_picture_of_that_moment(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")

    out = pipeline.build_capo_variant(song_dir)

    assert out == song_dir / "easychords" / "paper-lanterns-easychords.mp4"
    easy = renders[-1]
    assert easy["image_dir"] == song_dir / "easychords" / "images"
    song = load_song(song_dir / "lyrics_timed.json")
    song_timeline = build_image_timeline(song.lines, song.chord_track, 40.0)
    easy_timeline = build_image_timeline(easy["lines"], easy["chord_track"], 40.0)
    assert [(s.start, s.end) for s in easy_timeline] == [(s.start, s.end) for s in song_timeline]
    assert any(s.image_key != e.image_key for s, e in zip(song_timeline, easy_timeline))   # the capo renamed the chords
    for song_segment, easy_segment in zip(song_timeline, easy_timeline):
        easy_picture = easy["image_dir"] / f"{easy_segment.image_key}.png"
        assert easy_picture.exists(), easy_segment
        assert easy_picture.read_bytes() == (song_dir / "images" / f"{song_segment.image_key}.png").read_bytes()


def test_a_song_whose_pictures_are_filed_under_the_old_spelling_still_gives_its_easy_version_every_picture(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work", key="Bb major", labels=("A#", "D#", "F", "A#", "A#", "D#", "F", "A#"))
    song = load_song(song_dir / "lyrics_timed.json")
    song.chord_track = _track(["Bb", "Eb", "F", "Bb", "Bb", "Eb", "F", "Bb"], "Bb major")   # respelled after the key check
    save_song(song, song_dir / "lyrics_timed.json")

    pipeline.build_capo_variant(song_dir)

    easy = renders[-1]
    for segment in build_image_timeline(easy["lines"], easy["chord_track"], 40.0):
        assert (easy["image_dir"] / f"{segment.image_key}.png").exists(), segment


def test_rebuilding_the_easy_version_takes_the_songs_pictures_as_they_are_now(tmp_path, renders):
    song_dir = _make_song(tmp_path / "work")
    pipeline.build_capo_variant(song_dir)
    song = load_song(song_dir / "lyrics_timed.json")
    old_line_picture = song_dir / "images" / f"{line_hash(song.lines[0].text)}.png"
    old_line_picture.unlink()                                                  # a Redo changed the line...
    song.lines = [_line(["copper", "kettles", "hum", "softly", "tonight", "again"], 16.0)]
    save_song(song, song_dir / "lyrics_timed.json")
    _picture(song_dir / "images" / f"{line_hash(song.lines[0].text)}.png", "the new line's picture")
    first_chord = song_dir / "images" / f"{line_hash(instrumental_caption('Eb'))}.png"
    first_chord.write_bytes(b"a new picture bought for Eb")                     # ...and bought new pictures

    pipeline.build_capo_variant(song_dir)

    easy_images = song_dir / "easychords" / "images"
    assert not (easy_images / old_line_picture.name).exists()
    assert (easy_images / f"{line_hash(song.lines[0].text)}.png").read_bytes() == b"the new line's picture"
    assert (easy_images / f"{line_hash(instrumental_caption('D'))}.png").read_bytes() == b"a new picture bought for Eb"
    easy = renders[-1]
    for segment in build_image_timeline(easy["lines"], easy["chord_track"], 40.0):
        assert (easy_images / f"{segment.image_key}.png").exists(), segment
