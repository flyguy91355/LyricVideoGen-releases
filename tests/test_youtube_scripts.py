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


# --- update_support_description.py puts a key note right after the description, not before everything (owner,
# 2026-09-29, reversing the 2026-09-26 "note first" layout tested here before -- issue #7 review, F054's
# no-duplication guarantee still holds, just at the new position) ------------------------------------------------------

_TEMPLATE = "Tip line: https://ko-fi.com/x\n{description}\nThanks for playing along!"


def test_the_support_update_moves_a_key_note_after_the_description_and_is_stable():
    from lyricvideo.key_note import apply_key_note
    from lyricvideo.youtube_schedule import render_description

    script = _load_script("update_support_description")
    note = "📌 Song key: C minor (not D minor as shown in the video)"
    front = apply_key_note(render_description(_TEMPLATE, "Body."), "C minor", "D minor")   # apply_key_note just prepends

    once, regenerated = script.new_description(front, _TEMPLATE, [script.DEFAULT_OLD_TEXT])

    assert not regenerated
    assert once == f"Tip line: https://ko-fi.com/x\n\nBody.\n\n{note}\n\nThanks for playing along!"
    again, _regenerated = script.new_description(once, _TEMPLATE, [script.DEFAULT_OLD_TEXT])
    assert again == once


def test_the_support_update_converts_an_old_description_with_a_key_note():
    script = _load_script("update_support_description")
    note = "📌 Song key: C minor (not D minor as shown in the video)"
    old = f"{note}\n\nBody.\n\n{script.DEFAULT_OLD_TEXT}"

    updated, regenerated = script.new_description(old, _TEMPLATE, [script.DEFAULT_OLD_TEXT])

    assert not regenerated
    assert updated == f"Tip line: https://ko-fi.com/x\n\nBody.\n\n{note}\n\nThanks for playing along!"


def test_the_support_update_finds_nested_easy_chord_uploads(tmp_path):
    script = _load_script("update_support_description")
    main_dir = _uploaded_song(tmp_path / "some-song", "MAIN")
    easy_dir = _uploaded_song(tmp_path / "some-song" / "easychords", "EASY")

    assert script.find_uploaded_videos(tmp_path) == {
        "MAIN": ("some-song", main_dir), "EASY": ("some-song/easychords", easy_dir),
    }


# --- reorganize_playlists.py (owner, 2026-11-05: no 1-song artist playlists; genres from the fixed list) ---------------

def _playlist(pid, title, count=1):
    return {"id": pid, "title": title, "count": count}


def test_the_rework_sorts_the_channels_playlists_and_touches_only_its_own_kinds():
    script = _load_script("reorganize_playlists")
    playlists = [
        _playlist("P1", "Albert King - Play Along Videos", 2), _playlist("P2", "Eagles - Play Along Videos", 6),
        _playlist("P3", "Crosby - Play Along Videos", 2), _playlist("P4", "Country - Play Along Videos", 24),
        _playlist("P5", "Pop Rock / Soft Rock - Play Along Videos", 29), _playlist("P6", "#1 ALL Play Along Videos", 256),
        _playlist("P7", "Play Along Videos", 1), _playlist("P8", "EASY CHORD Play Along Songs", 166),
        _playlist("P9", "My Favourites", 3),
    ]

    sorted_ = script.sort_playlists(playlists, {"Albert King": 1, "Eagles": 6}, {"Country", "Pop Rock / Soft Rock"})

    assert [p["id"] for p in sorted_["delete_artist"]] == ["P1", "P3"]
    assert [p["id"] for p in sorted_["keep_artist"]] == ["P2"]
    assert [p["id"] for p in sorted_["fixed_genre"]] == ["P4"]
    assert [p["id"] for p in sorted_["old_genre"]] == ["P5"]
    assert [p["id"] for p in sorted_["other"]] == ["P6", "P7", "P8", "P9"]


class _Calls:
    def __init__(self, log, name):
        self._log, self._name = log, name

    def __getattr__(self, method):
        return lambda *a, **k: self._log.append((self._name, method)) or SimpleNamespace(execute=lambda: {})


class _CountingClient:
    def __init__(self):
        self.log = []

    def playlists(self):
        return _Calls(self.log, "playlists")

    def playlistItems(self):
        return _Calls(self.log, "playlistItems")


def test_the_quota_budget_stops_before_a_call_that_would_go_over():
    script = _load_script("reorganize_playlists")
    real = _CountingClient()
    client = script.QuotaBudget(real, max_units=101)

    client.playlistItems().list(part="id").execute()                 # 1
    client.playlistItems().insert(part="snippet", body={}).execute()  # 50
    client.playlists().delete(id="x").execute()                       # 50 -> 101
    with pytest.raises(script.BudgetSpent):
        client.playlists().list(part="id").execute()

    assert client.used == 101 and len(real.log) == 3


def test_the_plans_client_refuses_any_youtube_change():
    script = _load_script("reorganize_playlists")
    client = script.QuotaBudget(_CountingClient(), max_units=500, read_only=True)

    client.playlists().list(part="id").execute()
    with pytest.raises(RuntimeError):
        client.playlists().delete(id="x")


def _rework_fixture(tmp_path, monkeypatch, script, playlists):
    for slug, vid, artist in (("song-a", "va", "Albert King"), ("song-b", "vb", "Eagles"), ("song-c", "vc", "Eagles")):
        folder = _uploaded_song(tmp_path / slug, vid)
        (folder / "song_info.json").write_text(json.dumps({"title": slug, "artist": artist, "genres": ["Blues"]}),
                                               encoding="utf-8")
    cache = {"artist:Albert King": "P1", "genre:Pop Rock / Soft Rock": "P5", "all": "PA"}
    log = []
    monkeypatch.setattr(script, "list_own_playlists", lambda yt: list(playlists))
    monkeypatch.setattr(script, "load_playlist_ids", lambda: dict(cache))
    monkeypatch.setattr(script, "remove_playlist_ids", lambda keys: [cache.pop(k, None) for k in keys])
    monkeypatch.setattr(script, "load_genres", lambda: ["Pop Rock / Soft Rock"])
    monkeypatch.setattr(script, "video_exists", lambda yt, vid: True)
    monkeypatch.setattr(script, "delete_playlist", lambda yt, pid: log.append(("delete", pid)) or True)
    monkeypatch.setattr(script, "load_key_overrides", lambda path: {})
    return cache, log


def test_the_rework_deletes_one_song_artist_playlists_sorts_every_video_then_drops_old_genres(tmp_path, monkeypatch):
    script = _load_script("reorganize_playlists")
    playlists = [_playlist("P1", "Albert King - Play Along Videos"), _playlist("P5", "Pop Rock / Soft Rock - Play Along Videos")]
    cache, log = _rework_fixture(tmp_path, monkeypatch, script, playlists)

    def organize(yt, ai, work_dir, **kwargs):
        assert kwargs["include_artist_catalog"] is False
        log.append(("organize", work_dir.name))
        return []

    monkeypatch.setattr(script, "organize_video", organize)

    result = script.run("yt", "ai", tmp_path, apply=True, progress_path=tmp_path / "done.json")

    assert log == [("delete", "P1"), ("organize", "song-a"), ("organize", "song-b"), ("organize", "song-c"),
                   ("delete", "P5")]
    assert "artist:Albert King" not in cache and "genre:Pop Rock / Soft Rock" not in cache and cache["all"] == "PA"
    assert result["finished"] is True


def test_the_rework_stops_at_the_budget_keeps_old_genres_and_a_rerun_carries_on(tmp_path, monkeypatch):
    script = _load_script("reorganize_playlists")
    playlists = [_playlist("P5", "Pop Rock / Soft Rock - Play Along Videos")]
    _cache, log = _rework_fixture(tmp_path, monkeypatch, script, playlists)
    budget = {"left": 2}

    def organize(yt, ai, work_dir, **kwargs):
        if budget["left"] == 0:
            raise script.BudgetSpent("budget")
        budget["left"] -= 1
        log.append(("organize", work_dir.name))
        return []

    monkeypatch.setattr(script, "organize_video", organize)
    progress = tmp_path / "done.json"

    first = script.run("yt", "ai", tmp_path, apply=True, progress_path=progress)
    assert first["finished"] is False and ("delete", "P5") not in log

    budget["left"] = 5
    second = script.run("yt", "ai", tmp_path, apply=True, progress_path=progress)

    assert [entry for entry in log if entry[0] == "organize"] == [
        ("organize", "song-a"), ("organize", "song-b"), ("organize", "song-c")]
    assert second["finished"] is True and log[-1] == ("delete", "P5")


def test_the_rework_plan_changes_nothing(tmp_path, monkeypatch):
    script = _load_script("reorganize_playlists")
    playlists = [_playlist("P1", "Albert King - Play Along Videos")]
    _cache, log = _rework_fixture(tmp_path, monkeypatch, script, playlists)
    monkeypatch.setattr(script, "organize_video", lambda *a, **k: log.append("organize"))

    result = script.run("yt", "ai", tmp_path, apply=False, progress_path=tmp_path / "done.json")

    assert log == [] and not (tmp_path / "done.json").exists()
    assert [p["id"] for p in result["sorted"]["delete_artist"]] == ["P1"]
