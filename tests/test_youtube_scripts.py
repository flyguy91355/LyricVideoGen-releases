"""The YouTube maintenance scripts under scripts/ (loaded straight from their files; nothing here talks to YouTube)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo.models import ChordTrack, LyricLine, Song, Word, save_song
from lyricvideo.youtube_state import YoutubeState, save_youtube_state

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _uploaded_song(folder: Path, video_id: str, key: str = "C major") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    save_song(Song(title="Some Song", audio_path="a.mp3", lines=[LyricLine(words=[Word(word="la")])],
                   chord_track=ChordTrack(key=key, events=[])), folder / "lyrics_timed.json")
    save_youtube_state(folder, YoutubeState(video_id=video_id, uploaded_at="2026-09-20T10:00:00", title="t"))
    return folder


# --- backfill_channel_organization.py visits EASY CHORD versions too (issue #7 review, F087) ---------------------------

def test_the_organization_backfill_visits_nested_easy_chord_uploads(tmp_path, monkeypatch):
    script = _load_script("backfill_channel_organization")
    _uploaded_song(tmp_path / "some-song", "MAIN", key="D minor")
    _uploaded_song(tmp_path / "some-song" / "easychords", "EASY", key="E minor")
    calls = []
    monkeypatch.setattr(script, "video_exists", lambda client, vid: True)
    monkeypatch.setattr(script, "organize_video",
                        lambda yt, ai, work_dir, key_override=None: calls.append((work_dir.name, key_override)) or [])

    counts = script.backfill("yt", "ai", tmp_path, key_overrides={"some-song": "C minor"})

    assert calls == [("some-song", "C minor"), ("easychords", None)]
    assert counts["organized"] == 2


def test_the_organization_backfill_stops_at_a_quota_error(tmp_path, monkeypatch):
    from googleapiclient.errors import HttpError

    script = _load_script("backfill_channel_organization")
    _uploaded_song(tmp_path / "a-song", "A")
    _uploaded_song(tmp_path / "b-song", "B")
    monkeypatch.setattr(script, "video_exists", lambda client, vid: True)
    content = (b'{"error": {"code": 403, "message": "x", "errors": [{"message": "x", "domain": "youtube.quota", '
               b'"reason": "quotaExceeded"}]}}')
    calls = []

    def organize(yt, ai, work_dir, key_override=None):
        calls.append(work_dir.name)
        raise HttpError(SimpleNamespace(status=403, reason="Forbidden"), content)

    monkeypatch.setattr(script, "organize_video", organize)

    counts = script.backfill("yt", "ai", tmp_path)

    assert calls == ["a-song"] and counts["stopped_for_quota"] is True


def test_the_organization_backfill_dry_run_makes_no_youtube_call(tmp_path, monkeypatch, capsys):
    script = _load_script("backfill_channel_organization")
    _uploaded_song(tmp_path / "some-song", "MAIN", key="G major")
    monkeypatch.setattr(script, "video_exists", lambda *a: pytest.fail("dry run called YouTube"))
    monkeypatch.setattr(script, "organize_video", lambda *a, **k: pytest.fail("dry run organized"))

    script.backfill(None, None, tmp_path, key_overrides={"some-song": "G minor"}, dry_run=True)

    assert "some-song (MAIN): would organize; easy-key playlists: none" in capsys.readouterr().out


def test_key_overrides_never_apply_to_an_easy_chord_version(tmp_path):
    script = _load_script("backfill_channel_organization")
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps({"notes": [
        {"label": "some-song", "true": "C minor", "shown": "D minor"},
        {"label": "some-song/easychords", "true": "C minor", "shown": "D major"},
    ]}), encoding="utf-8")

    assert script.load_key_overrides(notes) == {"some-song": "C minor"}
    assert script.load_key_overrides(tmp_path / "missing.json") == {}


def test_fixing_playlist_descriptions_backs_up_first_and_honors_dry_run(tmp_path, monkeypatch):
    script = _load_script("backfill_channel_organization")
    stale = [("easy_chord", "PLE", "EASY CHORD Play Along Songs", "old text", "new text")]
    monkeypatch.setattr(script, "stale_playlist_descriptions", lambda client: stale)
    updates = []
    monkeypatch.setattr(script, "update_playlist_description", lambda client, pid, title, desc: updates.append((pid, desc)))

    assert script.fix_playlist_descriptions("yt", dry_run=True, backup_dir=tmp_path / "reports") == 0
    assert updates == [] and not (tmp_path / "reports").exists()

    assert script.fix_playlist_descriptions("yt", dry_run=False, backup_dir=tmp_path / "reports") == 1
    assert updates == [("PLE", "new text")]
    backup = next((tmp_path / "reports").glob("playlist_description_backup_*.json"))
    assert json.loads(backup.read_text(encoding="utf-8"))["PLE"]["description"] == "old text"


# --- the retired support backfill can no longer write the raw template (issue #7 review, F088) --------------------------

def test_the_old_support_backfill_refuses_and_points_to_the_replacement(monkeypatch):
    script = _load_script("backfill_support_overlay_description")
    monkeypatch.setattr("lyricvideo.youtube_auth.load_credentials", lambda *a, **k: pytest.fail("touched YouTube"))

    with pytest.raises(SystemExit, match="update_support_description.py"):
        script.main()


def test_no_script_appends_the_support_template_raw():
    for path in SCRIPTS.glob("*.py"):
        assert 'f"{description}\\n\\n{support_text}"' not in path.read_text(encoding="utf-8"), path.name


# --- update_support_description.py keeps the key note first (issue #7 review, F054) ------------------------------------

_TEMPLATE = "Tip line: https://ko-fi.com/x\n{description}\nThanks for playing along!"


def test_the_support_update_keeps_a_key_note_first_and_is_stable():
    from lyricvideo.key_note import apply_key_note
    from lyricvideo.youtube_schedule import render_description

    script = _load_script("update_support_description")
    noted = apply_key_note(render_description(_TEMPLATE, "Body."), "C minor", "D minor")

    assert script.new_description(noted, _TEMPLATE, [script.DEFAULT_OLD_TEXT]) == noted


def test_the_support_update_converts_an_old_description_under_a_key_note():
    script = _load_script("update_support_description")
    note = "📌 Song key: C minor (not D minor as shown in the video)"
    old = f"{note}\n\nBody.\n\n{script.DEFAULT_OLD_TEXT}"

    assert script.new_description(old, _TEMPLATE, [script.DEFAULT_OLD_TEXT]) == (
        f"{note}\n\nTip line: https://ko-fi.com/x\n\nBody.\n\nThanks for playing along!"
    )


def test_the_support_update_finds_nested_easy_chord_uploads(tmp_path):
    script = _load_script("update_support_description")
    _uploaded_song(tmp_path / "some-song", "MAIN")
    _uploaded_song(tmp_path / "some-song" / "easychords", "EASY")

    assert script.find_uploaded_videos(tmp_path) == {"MAIN": "some-song", "EASY": "some-song/easychords"}
