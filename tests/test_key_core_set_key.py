"""Set Key in Flagged for Lyrics Review (issue #7 review, F013/F117): agreeing with the key the chords already carry
confirms the song at once, a bad key gets a clear message, and a click during a running job says why nothing happens."""

from types import SimpleNamespace

import pytest

from lyricvideo.gui import LyricVideoGUI
from lyricvideo.key_decision import (
    KeyDecision, confirmed_key_for_upload, key_needs_attention, key_state, load_decision, load_owner_key, save_decision,
)
from lyricvideo.models import ChordEvent, ChordTrack, Song, save_song


def _song(tmp_path, key="D major", video=False, decision=None):
    song_dir = tmp_path / "work" / "some-song"
    song_dir.mkdir(parents=True)
    events = [ChordEvent(float(i * 4), float(i * 4 + 4), label) for i, label in enumerate(["D", "G", "A", "D"])]
    save_song(Song(title="Some Song", audio_path="a.mp3", chord_track=ChordTrack(events=events, key=key, bpm=100.0)),
              song_dir / "lyrics_timed.json")
    if video:
        (song_dir / "some-song.mp4").write_bytes(b"video")
    if decision is not None:
        save_decision(song_dir, decision)
    return song_dir


def _stub(tmp_path, monkeypatch, typed, make_video_now=True, running=False):
    monkeypatch.setattr("lyricvideo.gui.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("lyricvideo.gui.simpledialog.askstring", lambda title, prompt, **kw: typed)
    seen = {"errors": [], "infos": [], "asked": [], "made": [], "invalidated": []}
    monkeypatch.setattr("lyricvideo.gui.messagebox.showerror", lambda title, message, **kw: seen["errors"].append(message))
    monkeypatch.setattr("lyricvideo.gui.messagebox.showinfo", lambda title, message, **kw: seen["infos"].append(message))

    def askyesno(title, message, **kw):
        seen["asked"].append(message)
        return make_video_now

    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", askyesno)
    stub = SimpleNamespace(
        root=None, _running=running,
        _on_render_anyway_flagged=lambda slug, confirm=True: seen["made"].append((slug, confirm)),
        **{name: (lambda name=name: seen["invalidated"].append(name)) for name in (
            "_invalidate_flagged_list", "_invalidate_pending_list", "_invalidate_upload_list",
            "_invalidate_easy_chord_backfill_list")},
    )
    return stub, seen


HELD = KeyDecision(status="review", chord_key="D major", published_key="B minor", candidates=["D major", "B minor"])


def test_agreeing_with_the_chords_confirms_a_held_song_at_once(tmp_path, monkeypatch):
    song_dir = _song(tmp_path, decision=HELD)
    stub, seen = _stub(tmp_path, monkeypatch, "D major", make_video_now=False)

    LyricVideoGUI._on_set_key_flagged(stub, "some-song")

    saved = load_decision(song_dir)
    assert saved.confirmed and saved.key == "D major" and saved.source == "owner"
    assert key_state(song_dir) == "confirmed" and not key_needs_attention(song_dir)
    assert confirmed_key_for_upload(song_dir) == "D major"
    assert seen["asked"] and seen["made"] == [] and seen["errors"] == []      # no video yet: still offered, declined
    assert "_invalidate_flagged_list" in seen["invalidated"] and "_invalidate_pending_list" in seen["invalidated"]


def test_agreeing_with_the_key_an_existing_video_shows_needs_no_new_video(tmp_path, monkeypatch):
    song_dir = _song(tmp_path, video=True)                                   # an unchecked song made before the key check
    stub, seen = _stub(tmp_path, monkeypatch, "d major")

    LyricVideoGUI._on_set_key_flagged(stub, "some-song")

    assert key_state(song_dir) == "confirmed"
    assert seen["asked"] == [] and seen["made"] == [] and "already shows" in seen["infos"][0]


def test_a_different_key_waits_for_the_new_video_before_it_is_confirmed(tmp_path, monkeypatch):
    song_dir = _song(tmp_path, video=True, decision=HELD)
    stub, seen = _stub(tmp_path, monkeypatch, "B minor")

    LyricVideoGUI._on_set_key_flagged(stub, "some-song")

    assert load_owner_key(song_dir) == "B minor"
    assert key_state(song_dir) == "review"                                   # the old video still shows D major
    assert "made again" in seen["asked"][0] and seen["made"] == [("some-song", False)]


@pytest.mark.parametrize("typed,saved", [("Cb major", "B major"), ("E# minor", "F minor"), ("F♯ Minor", "F# minor")])
def test_real_enharmonic_spellings_are_saved(tmp_path, monkeypatch, typed, saved):
    song_dir = _song(tmp_path, decision=HELD)
    stub, seen = _stub(tmp_path, monkeypatch, typed, make_video_now=False)

    LyricVideoGUI._on_set_key_flagged(stub, "some-song")

    assert load_owner_key(song_dir) == saved and seen["errors"] == []


@pytest.mark.parametrize("typed", ["E##", "H minor", "lydian", "CM"])
def test_a_bad_key_shows_a_clear_error_and_saves_nothing(tmp_path, monkeypatch, typed):
    song_dir = _song(tmp_path, decision=HELD)
    stub, seen = _stub(tmp_path, monkeypatch, typed)

    LyricVideoGUI._on_set_key_flagged(stub, "some-song")

    assert load_owner_key(song_dir) is None and not load_decision(song_dir).confirmed
    assert len(seen["errors"]) == 1 and "Nothing was saved" in seen["errors"][0] and seen["made"] == []


def test_set_key_while_a_job_runs_says_so_instead_of_doing_nothing(tmp_path, monkeypatch):
    song_dir = _song(tmp_path, decision=HELD)
    asked = []
    stub, seen = _stub(tmp_path, monkeypatch, "D major", running=True)
    monkeypatch.setattr("lyricvideo.gui.simpledialog.askstring", lambda *a, **k: asked.append(1) or "D major")

    LyricVideoGUI._on_set_key_flagged(stub, "some-song")

    assert asked == [] and load_owner_key(song_dir) is None
    assert seen["infos"] and "running" in seen["infos"][0]
