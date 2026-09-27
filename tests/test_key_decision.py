import json
from types import SimpleNamespace

import pytest

from lyricvideo.key_decision import (
    KEY_DECISION_FILE, KEY_HOLD_PREFIX, KEY_OWNER_FILE, apply_saved_owner_key, decide_key, load_decision,
    load_owner_key, save_owner_key, settle_song_key,
)
from lyricvideo.key_estimate import KeyEstimate
from lyricvideo.models import ChordEvent, ChordTrack, Song


def track(*labels_and_seconds, key="C major"):
    events, t = [], 0.0
    for label, secs in labels_and_seconds:
        events.append(ChordEvent(start=t, end=t + secs, label=label))
        t += secs
    return ChordTrack(events=events, key=key, bpm=100.0)


D_MAJOR = KeyEstimate(tonic=2, mode="major", margin=0.3)
D_SONG = track(("D", 8), ("G", 4), ("A", 4), ("D", 8))


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls, self.messages = reply, [], self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def test_agreement_between_the_chords_and_the_second_opinion_confirms_the_key():
    d = decide_key(D_MAJOR, "D major", None, ["D major", "B minor"])
    assert d.confirmed and d.key == "D major" and d.source == "agreed"


def test_disagreement_needs_the_owner():
    d = decide_key(D_MAJOR, "G major", None, ["D major", "G major"])
    assert not d.confirmed and d.status == "review" and d.key == ""
    assert d.chord_key == "D major" and d.published_key == "G major"
    assert d.concern().startswith(KEY_HOLD_PREFIX) and "D major" in d.concern() and "G major" in d.concern()


def test_no_second_opinion_needs_the_owner():
    d = decide_key(D_MAJOR, None, None, ["D major"])
    assert d.status == "review" and "no second opinion" in d.concern().lower()


def test_no_chords_needs_the_owner():
    d = decide_key(None, None, None, [])
    assert d.status == "review" and "no chords" in d.concern().lower()


def test_the_owners_key_always_wins_even_against_both_opinions():
    d = decide_key(D_MAJOR, "G major", "Bb minor", ["D major"])
    assert d.confirmed and d.source == "owner" and d.key == "Bb minor"


def test_an_unreadable_owner_key_is_ignored():
    assert decide_key(D_MAJOR, "D major", "lydian", []).source == "agreed"


def test_owner_key_round_trip_and_canonical_spelling(tmp_path):
    assert load_owner_key(tmp_path) is None
    assert save_owner_key(tmp_path, "a# major") == "Bb major"
    assert load_owner_key(tmp_path) == "Bb major"
    assert json.loads((tmp_path / KEY_OWNER_FILE).read_text())["key"] == "Bb major"


def test_saving_a_nonsense_owner_key_is_refused(tmp_path):
    with pytest.raises(ValueError):
        save_owner_key(tmp_path, "lydian")
    assert load_owner_key(tmp_path) is None


def test_settle_confirms_respells_and_saves_the_decision(tmp_path):
    client = FakeClient("KEY: D major")
    decision, out = settle_song_key(tmp_path, D_SONG, "All for Love", "Bryan Adams", client)
    assert decision.confirmed and out.key == "D major"
    saved = load_decision(tmp_path)
    assert saved.confirmed and saved.key == "D major" and saved.source == "agreed"
    assert (tmp_path / KEY_DECISION_FILE).exists()
    prompt = client.calls[0]["messages"][0]["content"]
    assert "All for Love" in prompt and "D major" in prompt


def test_settle_respells_the_chords_to_the_confirmed_keys_convention(tmp_path):
    sharp_track = track(("A#", 8), ("D#", 4), ("F", 4), ("A#", 8), key="A# major")   # spelled with sharps by mistake
    decision, out = settle_song_key(tmp_path, sharp_track, "x", "y", FakeClient("KEY: Bb major"))
    assert decision.confirmed and out.key == "Bb major"
    assert [e.label for e in out.events][:2] == ["Bb", "Eb"]


def test_settle_holds_when_the_second_opinion_disagrees_but_keeps_a_provisional_key(tmp_path):
    decision, out = settle_song_key(tmp_path, D_SONG, "x", "y", FakeClient("KEY: G major"))
    assert decision.status == "review" and out.key == "D major"
    assert not load_decision(tmp_path).confirmed


def test_settle_without_a_client_needs_the_owner(tmp_path):
    decision, _out = settle_song_key(tmp_path, D_SONG, "x", "y", None)
    assert decision.status == "review"


def test_settle_uses_the_owner_key_and_does_not_ask_claude(tmp_path):
    save_owner_key(tmp_path, "G major")
    client = FakeClient("KEY: D major")
    decision, out = settle_song_key(tmp_path, D_SONG, "x", "y", client)
    assert decision.confirmed and decision.source == "owner" and out.key == "G major"
    assert client.calls == []


def test_apply_saved_owner_key_fixes_a_saved_song_and_reports_it(tmp_path):
    song = Song(title="T", audio_path="a.mp3", chord_track=track(("D", 8), ("G", 4), key="Bb major"))
    assert apply_saved_owner_key(tmp_path, song) is False        # nothing set yet: untouched
    assert song.chord_track.key == "Bb major"
    save_owner_key(tmp_path, "D major")
    assert apply_saved_owner_key(tmp_path, song) is True
    assert song.chord_track.key == "D major"
    assert load_decision(tmp_path).source == "owner"
    assert apply_saved_owner_key(tmp_path, song) is False        # already applied: no change to report


def test_key_state_and_the_key_an_upload_uses(tmp_path):
    from lyricvideo.key_decision import KeyNotConfirmed, confirmed_key_for_upload, key_state, save_decision, KeyDecision
    song = tmp_path / "some-song"
    song.mkdir()
    assert key_state(song) == "unchecked"
    with pytest.raises(KeyNotConfirmed):
        confirmed_key_for_upload(song)
    save_decision(song, KeyDecision(status="review", chord_key="D major", published_key="G major"))
    assert key_state(song) == "review"
    with pytest.raises(KeyNotConfirmed):
        confirmed_key_for_upload(song)
    save_decision(song, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major"))
    assert key_state(song) == "confirmed" and confirmed_key_for_upload(song) == "D major"


def test_an_easy_chord_folder_uses_its_songs_key_decision(tmp_path):
    from lyricvideo.key_decision import confirmed_key_for_upload, key_state, save_decision, KeyDecision
    song = tmp_path / "some-song"
    (song / "easychords").mkdir(parents=True)
    save_decision(song, KeyDecision(status="confirmed", key="Eb major", source="agreed", chord_key="Eb major"))
    assert key_state(song / "easychords") == "confirmed"
    assert confirmed_key_for_upload(song / "easychords") == "Eb major"


# --- issue #7 review, F013/F019: Set Key that agrees with the chords must CONFIRM the song --------------------------------

def test_set_key_agreeing_with_the_chords_confirms_a_song_held_for_its_key(tmp_path):
    from lyricvideo.key_decision import confirmed_key_for_upload, key_needs_attention, key_state
    decision, held_track = settle_song_key(tmp_path, D_SONG, "x", "y", FakeClient("KEY: B minor"))
    assert decision.status == "review" and held_track.key == "D major"          # the chords' own answer, provisional
    song = Song(title="T", audio_path="a.mp3", chord_track=held_track)
    save_owner_key(tmp_path, "D major")                                          # the owner agrees with the chords

    changed = apply_saved_owner_key(tmp_path, song)

    assert changed is False                                                      # nothing to respell
    saved = load_decision(tmp_path)
    assert saved.confirmed and saved.key == "D major" and saved.source == "owner"
    assert saved.published_key == "B minor"                                      # what the second opinion said is kept
    assert key_state(tmp_path) == "confirmed" and confirmed_key_for_upload(tmp_path) == "D major"
    assert key_needs_attention(tmp_path) is False


def test_set_key_confirms_an_unchecked_song_whose_saved_key_already_matches(tmp_path):
    from lyricvideo.key_decision import key_state
    song = Song(title="T", audio_path="a.mp3", chord_track=track(("D", 8), ("G", 4), ("A", 4), key="D major"))
    assert key_state(tmp_path) == "unchecked"
    save_owner_key(tmp_path, "d major")

    assert apply_saved_owner_key(tmp_path, song) is False
    assert key_state(tmp_path) == "confirmed" and load_decision(tmp_path).key == "D major"


def test_confirm_owner_key_needs_an_owner_key_and_keeps_a_matching_confirmed_decision(tmp_path):
    from lyricvideo.key_decision import KeyDecision, confirm_owner_key, save_decision
    assert confirm_owner_key(tmp_path, D_SONG) is None and load_decision(tmp_path) is None
    save_decision(tmp_path, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major", at="then"))
    save_owner_key(tmp_path, "D major")
    assert confirm_owner_key(tmp_path, D_SONG).at == "then"                     # the same key: left as it is
    save_owner_key(tmp_path, "B minor")
    changed = confirm_owner_key(tmp_path, D_SONG)
    assert changed.confirmed and changed.key == "B minor" and changed.source == "owner"   # the owner always wins
    assert load_decision(tmp_path).key == "B minor"


def test_apply_saved_owner_key_compares_keys_not_spellings(tmp_path):
    sharp = track(("A#", 8), ("D#", 4), ("F", 4), key="A# major")               # spelled with the flats box off
    song = Song(title="T", audio_path="a.mp3", chord_track=sharp)
    save_owner_key(tmp_path, "Bb major")

    assert apply_saved_owner_key(tmp_path, song, prefer_flats=False) is False
    assert song.chord_track.key == "A# major" and load_decision(tmp_path).key == "Bb major"
    assert apply_saved_owner_key(tmp_path, song) is True                         # flats wanted: respelled
    assert [e.label for e in song.chord_track.events] == ["Bb", "Eb", "F"] and song.chord_track.key == "Bb major"


# --- issue #7 review, F105: Settings' "Use flats in flat keys" box is honored where the spelling is decided ---------------

def test_settle_with_the_flats_box_off_keeps_sharps_but_records_the_canonical_key(tmp_path):
    sharp = track(("A#", 8), ("D#", 4), ("F", 4), ("Gm", 4), ("A#", 8), key="")
    decision, out = settle_song_key(tmp_path, sharp, "x", "y", FakeClient("KEY: Bb major"), prefer_flats=False)
    assert decision.confirmed and decision.key == "Bb major"
    assert out.key == "A# major" and [e.label for e in out.events] == ["A#", "D#", "F", "Gm", "A#"]
    decision, out = settle_song_key(tmp_path, sharp, "x", "y", FakeClient("KEY: Bb major"))
    assert out.key == "Bb major" and [e.label for e in out.events] == ["Bb", "Eb", "F", "Gm", "Bb"]


# --- issue #7 review, F117/F123: every real key spelling, and never a KeyError ----------------------------------------

@pytest.mark.parametrize("typed,saved", [
    ("Cb major", "B major"), ("Fb", "E major"), ("E# minor", "F minor"), ("B#", "C major"),
    ("F♯ Minor", "F# minor"), ("B♭ MAJOR", "Bb major"), ("g sharp minor", "G# minor"), ("E-flat major", "Eb major"),
])
def test_set_key_accepts_every_real_key_spelling(tmp_path, typed, saved):
    assert save_owner_key(tmp_path, typed) == saved and load_owner_key(tmp_path) == saved


@pytest.mark.parametrize("typed", ["E##", "Dbb", "H minor", "CM", "D dorian", "", "   "])
def test_set_key_refuses_a_non_key_with_value_error_only(tmp_path, typed):
    with pytest.raises(ValueError):
        save_owner_key(tmp_path, typed)
    assert load_owner_key(tmp_path) is None


def test_a_cb_major_second_opinion_is_read_not_a_crash(tmp_path):
    b_song = track(("B", 8), ("E", 4), ("F#", 4), ("B", 8))
    decision, out = settle_song_key(tmp_path, b_song, "x", "y", FakeClient("KEY: Cb major"))
    assert decision.confirmed and decision.key == "B major" and out.key == "B major"


def test_owner_and_decision_files_are_written_whole(tmp_path):
    from lyricvideo.key_decision import KeyDecision, save_decision
    save_owner_key(tmp_path, "D major")
    save_decision(tmp_path, KeyDecision(status="confirmed", key="D major", source="owner"))
    assert sorted(p.name for p in tmp_path.iterdir()) == [KEY_DECISION_FILE, KEY_OWNER_FILE]   # no temp files left
