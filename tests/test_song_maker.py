"""Create a Song (owner, 2026-10-31): voice + style + a sentence -> a band from the roster, Claude's lyrics, MiniMax's song."""

import json
from types import SimpleNamespace

import httpx
import pytest

from lyricvideo import song_maker
from lyricvideo.ai_generated import AI_FILE, AI_SONG_FILE, requested_key
from lyricvideo.song_maker import (
    NEW_BAND, band_choices, bands_for, create_song, music_prompt, musicbrainz_has_artist, name_new_band, sung_lines,
    write_song,
)

ROSTER = {"artists": [
    {"name": "Burning Ember", "style": "Hard rock", "voice": "female", "status": "active", "sound": "Loud guitars.",
     "vocal": "Soaring female vocal."},
    {"name": "Iron Canyon", "style": "Hard rock", "voice": "male", "status": "proposed", "sound": "Raspy.", "vocal": "Raspy male."},
    {"name": "Choir Kids", "style": "Hard rock", "voice": "mixed", "status": "active", "sound": "Big.", "vocal": "Group vocals."},
    {"name": "Late Ember", "style": "Hard rock", "voice": "female", "status": "proposed", "sound": "x", "vocal": "y"},
    {"name": "Copperleaf", "style": "Acoustic", "voice": "male", "status": "active", "sound": "Warm.", "vocal": "Soft male."},
]}

LYRICS = "[Intro]\n\n[Verse]\n" + "\n".join(f"line {i} of the verse" for i in range(6)) + "\n\n[Chorus]\nsing it loud\nsing it proud\n"
PLAN = {"title": "Wrong Turn", "key": "E minor", "bpm": 126, "chords": ["Em", "C", "G", "D"], "lyrics": LYRICS}


class FakeClaude:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.messages = self

    def create(self, **kwargs):
        assert kwargs["thinking"] == {"type": "disabled"}
        self.prompts.append(kwargs["messages"][0]["content"])
        reply = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(text=reply if isinstance(reply, str) else json.dumps(reply))])


def test_the_fitting_band_is_picked_its_own_voice_before_a_mixed_one_then_active_first():
    assert bands_for(ROSTER, "Female", "Hard rock") == ["Burning Ember", "Late Ember", "Choir Kids"]
    assert bands_for(ROSTER, "Male", "Hard rock") == ["Iron Canyon", "Choir Kids"]
    assert bands_for(ROSTER, "Female", "Country") == []


def test_the_band_menu_offers_the_fitting_bands_then_a_new_band_then_the_rest():
    assert band_choices(ROSTER, "Male", "Acoustic") == ["Copperleaf", NEW_BAND, "Burning Ember", "Choir Kids",
                                                        "Iron Canyon", "Late Ember"]
    assert band_choices(ROSTER, "Female", "Country")[0] == NEW_BAND


def test_a_new_band_never_takes_a_roster_name_or_a_real_acts_name():
    claude = FakeClaude({"names": ["Burning Ember", "Real Band", "Velvet Static"], "sound": "Big riffs.", "vocal": "Grit."})

    band = name_new_band(claude, "Female", "Hard rock", ROSTER, is_taken=lambda name: name == "Real Band")

    assert band["name"] == "Velvet Static"
    assert (band["style"], band["voice"], band["sound"], band["vocal"]) == ("Hard rock", "female", "Big riffs.", "Grit.")


def test_musicbrainz_is_asked_again_after_a_503_and_a_name_is_never_used_unchecked():
    calls = []

    def flaky(url, **kwargs):
        calls.append(kwargs["params"]["query"])
        status = 503 if len(calls) == 1 else 200
        return httpx.Response(status, json={"artists": [{"name": "velvet static"}]}, request=httpx.Request("GET", url))

    assert musicbrainz_has_artist("Velvet Static", http_get=flaky, sleep=lambda s: None) is True
    assert calls == ['artist:"Velvet Static"'] * 2

    def down(url, **kwargs):
        raise httpx.ConnectError("offline")

    with pytest.raises(RuntimeError, match="MusicBrainz"):
        musicbrainz_has_artist("Velvet Static", http_get=down, sleep=lambda s: None)


def test_claudes_song_is_asked_again_until_it_is_usable():
    claude = FakeClaude("no json here", {**PLAN, "key": "H minor"}, PLAN)

    plan = write_song(claude, "Female", "Hard rock", "a love gone wrong", ROSTER["artists"][0])

    assert (plan.title, plan.key, plan.bpm, plan.chords) == ("Wrong Turn", "E minor", 126, ["Em", "C", "G", "D"])
    assert len(claude.prompts) == 3 and "a love gone wrong" in claude.prompts[0]
    assert sung_lines(plan.lyrics)[0] == "line 0 of the verse" and "[Chorus]" not in sung_lines(plan.lyrics)


def test_the_song_prompt_never_asks_for_chords_a_guitarist_can_play_along_with():
    # 2026-11-05: those words made Claude write the chords as lyric lines under [Intro]/[Solo] (18 of 36 test replies;
    # 0 of 54 without them), and MiniMax sang them (Hands Like My Father's, Thunder Keeps Me Company, Cracked Bowl Kitchen).
    claude = FakeClaude(PLAN)

    write_song(claude, "Female", "Hard rock", "x", ROSTER["artists"][0])

    assert "guitarist" not in claude.prompts[0].lower()


def test_the_music_prompt_uses_the_bands_own_vocal_only_when_it_sings_with_that_voice():
    plan = write_song(FakeClaude(PLAN), "Female", "Hard rock", "x", ROSTER["artists"][0])

    own = music_prompt("Female", "Hard rock", ROSTER["artists"][0], plan)
    other = music_prompt("Male", "Hard rock", ROSTER["artists"][0], plan)

    assert "Soaring female vocal." in own and "126 BPM, key of E minor" in own and "(Em, C, G, D)" in own
    assert "Soaring female vocal." not in other and "Male lead vocal." in other


class FakeReplicate:
    def __init__(self, fail=False):
        self.fail = fail
        self.posted = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posted.append(json)
        return httpx.Response(201, json={"status": "starting", "urls": {"get": "https://r/get", "cancel": "https://r/cancel"}},
                              request=httpx.Request("POST", url))

    def get(self, url, headers=None, timeout=None):
        if url == "https://r/get":
            body = {"status": "failed" if self.fail else "succeeded", "output": "https://r/song.mp3"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
        return httpx.Response(200, content=b"mp3 bytes", request=httpx.Request("GET", url))


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr("lyricvideo.imagery.time.sleep", lambda s: None)
    monkeypatch.setattr(song_maker, "_tag_audio", lambda path, title, band: None)


def test_a_created_song_gets_its_own_folder_lyrics_ai_tag_and_record_and_a_new_band_joins_the_roster(tmp_path, quick):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(ROSTER), encoding="utf-8")
    (tmp_path / "work" / "wrong-turn").mkdir(parents=True)          # taken: the song gets wrong-turn-2
    claude = FakeClaude({"names": ["Velvet Static"], "sound": "Big riffs.", "vocal": "Grit."}, PLAN)
    replicate = FakeReplicate()

    made = create_song(tmp_path / "work", "Female", "Country", "a love gone wrong", NEW_BAND, claude, "token",
                       roster_path=roster_path, is_taken=lambda n: False, http_client=replicate)

    folder = tmp_path / "work" / "wrong-turn-2"
    assert (made.title, made.band, made.work_dir, made.audio_path) == ("Wrong Turn", "Velvet Static", folder,
                                                                        folder / "Wrong Turn.mp3")
    assert made.audio_path.read_bytes() == b"mp3 bytes"
    assert (folder / "Wrong Turn.txt").read_text(encoding="utf-8").splitlines() == sung_lines(LYRICS)
    assert (folder / AI_FILE).exists() and requested_key(folder) == "E minor"
    record = json.loads((folder / AI_SONG_FILE).read_text(encoding="utf-8"))
    assert record["band"] == "Velvet Static" and record["about"] == "a love gone wrong" and record["lyrics"] == LYRICS.strip()
    assert replicate.posted[0]["input"]["lyrics"] == LYRICS.strip() and "Grit." in replicate.posted[0]["input"]["prompt"]
    band = json.loads(roster_path.read_text(encoding="utf-8"))["artists"][-1]
    assert band["name"] == "Velvet Static" and band["songs"][0]["work_dir"] == "work/wrong-turn-2"


def test_a_song_minimax_could_not_make_leaves_no_folder_and_no_roster_change(tmp_path, quick):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(ROSTER), encoding="utf-8")

    with pytest.raises(RuntimeError, match="MiniMax could not make the song"):
        create_song(tmp_path / "work", "Female", "Hard rock", "x", "Burning Ember", FakeClaude(PLAN), "token",
                    roster_path=roster_path, http_client=FakeReplicate(fail=True))

    assert list((tmp_path / "work").iterdir()) == []
    assert json.loads(roster_path.read_text(encoding="utf-8")) == ROSTER


def test_a_song_needs_a_sentence_about_it(tmp_path):
    with pytest.raises(ValueError, match="what the song is about"):
        create_song(tmp_path, "Female", "Hard rock", "  ", "Burning Ember", None, "token", roster_path=tmp_path / "r.json")


def test_a_band_name_the_owner_typed_joins_the_roster_without_asking_claude_for_a_name(tmp_path, quick):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(ROSTER), encoding="utf-8")
    claude = FakeClaude(PLAN)                                       # only the song is asked for

    made = create_song(tmp_path / "work", "Male", "Country", "a dusty road", "  Gravel Saints ", claude, "token",
                       roster_path=roster_path, is_taken=lambda n: pytest.fail("the GUI already asked"),
                       http_client=FakeReplicate())

    band = json.loads(roster_path.read_text(encoding="utf-8"))["artists"][-1]
    assert made.band == "Gravel Saints" and len(claude.prompts) == 1
    assert (band["name"], band["style"], band["voice"], band["vocal"]) == ("Gravel Saints", "Country", "male", "Male lead vocal.")
    assert band["songs"][0]["title"] == "Wrong Turn"


def test_a_typed_name_of_a_roster_band_in_other_letters_is_that_band(tmp_path, quick):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(ROSTER), encoding="utf-8")

    made = create_song(tmp_path / "work", "Female", "Hard rock", "x", "burning ember", FakeClaude(PLAN), "token",
                       roster_path=roster_path, http_client=FakeReplicate())

    artists = json.loads(roster_path.read_text(encoding="utf-8"))["artists"]
    assert made.band == "Burning Ember" and len(artists) == len(ROSTER["artists"])


class _Picks:
    """A stand-in random: the voice it is told, the first style, and the first band option or the last (a new band)."""
    def __init__(self, voice, new_band):
        self.voice, self.new_band = voice, new_band

    def choice(self, options):
        if tuple(options) == song_maker.VOICES:
            return self.voice
        if NEW_BAND in options:
            return options[-1] if self.new_band else options[0]
        return options[0]


def test_a_scheduled_song_picks_a_style_and_voice_then_a_roster_band_or_a_new_one_and_its_own_subject():
    roster = {"artists": [dict(ROSTER["artists"][4], songs=[{"about": "a porch at dusk"}])]}   # Copperleaf: male Acoustic
    claude = FakeClaude({"about": "a long drive home"})

    plan = song_maker.plan_scheduled_song(claude, roster, rng=_Picks("Male", new_band=False))

    assert plan == ("Male", "Acoustic", "Copperleaf", "a long drive home")
    assert "a porch at dusk" in claude.prompts[0]                    # told what earlier songs were about
    assert song_maker.plan_scheduled_song(FakeClaude({"about": "x"}), roster, rng=_Picks("Male", new_band=True))[2] == NEW_BAND


def test_scheduled_songs_stop_for_the_day_once_made_or_after_three_failures(tmp_path):
    from lyricvideo.song_maker import record_scheduled_song, scheduled_song_due
    state = tmp_path / "scheduled.json"

    assert not scheduled_song_due(0, "2026-10-31", state)
    assert scheduled_song_due(2, "2026-10-31", state)
    record_scheduled_song(True, "2026-10-31", state)
    assert scheduled_song_due(2, "2026-10-31", state)
    record_scheduled_song(True, "2026-10-31", state)
    assert not scheduled_song_due(2, "2026-10-31", state)
    assert scheduled_song_due(2, "2026-11-01", state)                 # a new day

    for _ in range(3):
        record_scheduled_song(False, "2026-11-01", state)
    assert not scheduled_song_due(2, "2026-11-01", state)


def test_a_scheduled_song_is_marked_scheduled(tmp_path, quick):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(ROSTER), encoding="utf-8")

    made = create_song(tmp_path / "work", "Female", "Hard rock", "x", "Burning Ember", FakeClaude(PLAN), "token",
                       roster_path=roster_path, http_client=FakeReplicate(), scheduled=True)

    assert json.loads((made.work_dir / AI_SONG_FILE).read_text(encoding="utf-8"))["scheduled"] is True
    assert json.loads(roster_path.read_text(encoding="utf-8"))["artists"][0]["songs"][-1]["scheduled"] is True
