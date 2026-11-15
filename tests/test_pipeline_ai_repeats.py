"""An AI song whose music model sang a section again gets another copy of that section in its lyrics (2026-11-14:
'Collect Call Home' showed no lyrics for 38 s of singing). Made-up words only."""

import json

from lyricvideo.pipeline import _build_repeat_adder

VERSE = ["the river runs beside the old stone mill", "and morning fog lies heavy on the hill",
         "a lantern swings above the wooden door", "i wait for you like i have waited before"]
BRIDGE = ["hold on hold on the storm will soon be past", "hold on hold on we finally found peace at last",
          "the winter came and covered every road", "we traded all our dreams for heavy loads"]
CHORUS = ["carry me home across the silver sea", "carry me home where i long to be",
          "the stars will guide us through the night", "carry me home to the morning light"]
TAGGED = "\n".join(["[Intro]", "", "[Verse]", *VERSE, "", "[Chorus]", *CHORUS, "", "[Bridge]", *BRIDGE, "", "[Solo]", "",
                    "[Chorus]", *CHORUS, "", "[Outro]"])
WRITTEN = VERSE + CHORUS + BRIDGE + CHORUS


def _song(tmp_path, heard_words):
    (tmp_path / "ai_song.json").write_text(json.dumps({"title": "T", "lyrics": TAGGED}), encoding="utf-8")
    (tmp_path / "transcript.json").write_text(json.dumps({"text": " ".join(w["word"] for w in heard_words),
                                                          "words": heard_words}), encoding="utf-8")
    return tmp_path


def _sung_at(lines, start, step=0.45):
    words = " ".join(lines).split()
    return [{"word": w, "start": start + i * step, "end": start + i * step + 0.3} for i, w in enumerate(words)]


def test_a_bridge_and_chorus_sung_again_are_added(tmp_path):
    heard = _sung_at(VERSE + CHORUS + BRIDGE + CHORUS + BRIDGE + CHORUS, 10.0)
    add = _build_repeat_adder(_song(tmp_path, heard))

    result = add(WRITTEN)

    assert result is not None
    assert result[0] == VERSE + CHORUS + BRIDGE + CHORUS + BRIDGE + CHORUS


def test_a_copy_the_recognizer_piled_onto_one_instant_is_not_added(tmp_path):
    """'Last Light on Maple Street': two verses written again at one instant, each word 0 s long."""
    heard = _sung_at(VERSE + CHORUS + BRIDGE, 10.0)
    heard += [{"word": w, "start": 80.0, "end": 80.0} for w in " ".join(BRIDGE + CHORUS).split()]
    heard += _sung_at(CHORUS, 90.0)
    add = _build_repeat_adder(_song(tmp_path, heard))

    assert add(WRITTEN) is None


def test_a_song_we_did_not_make_has_no_repeat_adder(tmp_path):
    (tmp_path / "transcript.json").write_text(json.dumps({"text": "", "words": []}), encoding="utf-8")

    assert _build_repeat_adder(tmp_path) is None


def test_lyrics_that_are_not_the_songs_own_written_lines_are_left_alone(tmp_path):
    heard = _sung_at(VERSE + CHORUS + BRIDGE + CHORUS + BRIDGE + CHORUS, 10.0)
    add = _build_repeat_adder(_song(tmp_path, heard))

    assert add(VERSE + BRIDGE + CHORUS) is None
