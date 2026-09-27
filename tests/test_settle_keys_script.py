"""scripts/settle_keys.py (issue #7 review, F055/F105): a re-run costs no Claude call for a song whose key is already
confirmed, and the chords are spelled per Settings' "Use flats in flat keys" box."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from lyricvideo.key_decision import KeyDecision, load_decision, save_decision
from lyricvideo.models import ChordEvent, ChordTrack, Song, load_song, save_song
from lyricvideo.settings import Settings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "settle_keys.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_script_settle_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls, self.messages = reply, 0, self

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def _song(root, name, labels, key):
    work_dir = root / "work" / name
    work_dir.mkdir(parents=True)
    events = [ChordEvent(float(i * 4), float(i * 4 + 4), label) for i, label in enumerate(labels)]
    save_song(Song(title=name, audio_path="a.mp3", chord_track=ChordTrack(events=events, key=key, bpm=100.0)),
              work_dir / "lyrics_timed.json")
    (work_dir / "song_info.json").write_text(json.dumps({"title": name, "artist": "Nobody"}), encoding="utf-8")
    return work_dir


def _run(monkeypatch, root, client, prefer_flats=True, argv=("--apply",)):
    import anthropic
    import dotenv
    script = _load_script()
    monkeypatch.setattr(script, "PROJECT_ROOT", root)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: client)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(Settings, "load", staticmethod(lambda *a, **k: Settings(prefer_flats=prefer_flats)))
    return script.main(list(argv))


def test_a_rerun_asks_nothing_about_songs_already_settled(tmp_path, monkeypatch, capsys):
    settled = _song(tmp_path, "settled-song", ["D", "G", "A", "D"], "D major")
    save_decision(settled, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major", at="then"))
    fresh = _song(tmp_path, "fresh-song", ["D", "G", "A", "D"], "D major")
    client = FakeClient("KEY: D major")

    assert _run(monkeypatch, tmp_path, client) == 0

    assert client.calls == 1                                         # only the song with no decision was asked about
    assert load_decision(settled).at == "then" and load_decision(fresh).confirmed
    out = capsys.readouterr().out
    assert "1 already-settled" in out and "1 already-right" in out
    [report] = list((tmp_path / "reports").glob("key_rollout_*.json"))
    assert {r["action"] for r in json.loads(report.read_text(encoding="utf-8"))} == {"already-settled", "already-right"}


def test_a_correction_follows_the_flats_setting(tmp_path, monkeypatch):
    song = _song(tmp_path, "flat-song", ["A#", "D#", "F", "A#"], "A major")      # stored with a wrong key

    _run(monkeypatch, tmp_path, FakeClient("KEY: Bb major"), prefer_flats=False)

    track = load_song(song / "lyrics_timed.json").chord_track
    assert track.key == "A# major" and [e.label for e in track.events] == ["A#", "D#", "F", "A#"]
    assert load_decision(song).key == "Bb major"
