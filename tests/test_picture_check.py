"""picture_check.py: every picture is looked at once (on the owner's Pro plan), good ones are remembered, bad ones go to
bad_images and out of the library (owner, 2026-11-11)."""
import json
from pathlib import Path

import pytest
from PIL import Image

from lyricvideo import picture_check as pc
from lyricvideo.image_library import ImageLibrary, content_id

_REAL_RUN_CLAUDE = pc._run_claude      # captured at import, before conftest's guard replaces it for each test


def _png(path: Path, color=(120, 80, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 18), color).save(path)
    return path


def _answer(verdict="good", problem=""):
    return lambda args, cwd: json.dumps({"structured_output": {"verdict": verdict, "problem": problem}})


def test_check_runs_claude_code_safely_on_the_pro_plan(tmp_path, monkeypatch):
    seen = {}

    def run(args, cwd):
        seen["args"], seen["cwd"] = args, cwd
        return json.dumps({"structured_output": {"verdict": "bad", "problem": "a hand with no arm"}})

    monkeypatch.setattr(pc, "_run_claude", run)
    verdict = pc.check_picture(_png(tmp_path / "pic.png"))
    assert verdict == pc.Verdict(good=False, problem="a hand with no arm")
    args = seen["args"]
    assert args[1:3] == ["-p", "--safe-mode"]
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--tools") + 1] == "Read"
    assert "pic.png" in args[-1] and seen["cwd"] == tmp_path


def test_the_api_key_never_reaches_claude_code(tmp_path, monkeypatch):
    """With ANTHROPIC_API_KEY set, Claude Code bills the API instead of the Pro plan."""
    monkeypatch.setattr(pc, "_run_claude", _REAL_RUN_CLAUDE)      # the real one, with subprocess.run faked
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    captured = {}

    class _Done:
        stdout = "{}"

    def fake_run(args, cwd, env, capture_output, text, timeout):
        captured["env"] = env
        return _Done()

    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    pc._run_claude(["claude", "-p", "x"], tmp_path)
    assert "ANTHROPIC_API_KEY" not in captured["env"] and "ANTHROPIC_AUTH_TOKEN" not in captured["env"]
    assert "PATH" in captured["env"]


@pytest.mark.parametrize("stdout", [
    json.dumps({"is_error": True, "result": "Claude usage limit reached"}),
    json.dumps({"result": "no structured output"}),
    "not json",
    "",
])
def test_no_verdict_means_unavailable(tmp_path, monkeypatch, stdout):
    monkeypatch.setattr(pc, "_run_claude", lambda args, cwd: stdout)
    with pytest.raises(pc.PictureCheckUnavailable):
        pc.check_picture(_png(tmp_path / "pic.png"))


def test_missing_claude_code_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_claude_executable", lambda: None)
    with pytest.raises(pc.PictureCheckUnavailable, match="not installed"):
        pc.check_picture(_png(tmp_path / "pic.png"))


def test_a_good_picture_is_checked_once_ever(tmp_path):
    lib = ImageLibrary(tmp_path / "lib")
    pic = _png(tmp_path / "song" / "images" / "a.png")
    calls = []

    def check(path):
        calls.append(path)
        return pc.Verdict(True)

    removed, summary = pc.check_song_pictures([(pic, "line one")], "Song", library=lib, check=check)
    assert removed == [] and summary.checked == 1 and len(calls) == 1
    # the same picture in another song, under another name: known good, not sent again
    other = tmp_path / "other" / "images" / "b.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(pic.read_bytes())
    removed, summary = pc.check_song_pictures([(other, "line two")], "Other", library=lib, check=check)
    assert removed == [] and summary.known_good == 1 and summary.checked == 0 and len(calls) == 1
    assert lib.check_verdict(content_id(pic)) == ("good", "")


def test_a_bad_picture_goes_to_bad_images_and_out_of_the_library(tmp_path):
    lib = ImageLibrary(tmp_path / "lib")
    work = tmp_path / "song"
    pic = _png(work / "images" / "a.png")
    twin = work / "images" / "c.png"            # the same picture under a second line
    twin.write_bytes(pic.read_bytes())
    backup = work / "images_backup_1" / "a.png"     # an old backup the images stage would copy straight back in
    backup.parent.mkdir()
    backup.write_bytes(pic.read_bytes())
    image_id = lib.add(pic, [1.0] + [0.0] * 511, prompt="a hand on a fender", source_text="line", song_title="Song")
    keep = _png(work / "images" / "k.png", color=(10, 200, 10))

    def check(path):
        return pc.Verdict(path.name != "a.png", "" if path.name != "a.png" else "a hand with no arm on the fender")

    removed, summary = pc.check_song_pictures([(pic, "line"), (keep, "fine line"), (twin, "line again")], "Song",
                                              work_dir=work, library=lib, check=check)
    assert set(removed) == {pic, twin} and summary.bad == 1 and summary.checked == 2
    assert not pic.exists() and not twin.exists() and not backup.exists() and keep.exists()
    bad = lib.root / "bad_images" / f"{image_id}.png"
    assert bad.exists()
    note = bad.with_suffix(".txt").read_text()
    assert "Song" in note and "a hand with no arm" in note
    assert not lib.has(image_id) and not lib.image_path(image_id).exists()
    assert lib.check_verdict(image_id) == ("bad", "a hand with no arm on the fender")


def test_a_known_bad_picture_is_moved_out_without_asking_again(tmp_path):
    lib = ImageLibrary(tmp_path / "lib")
    pic = _png(tmp_path / "song" / "images" / "a.png")
    lib.record_check(content_id(pic), "bad", "broken neck", "opus")

    def check(path):
        raise AssertionError("a known picture is never sent again")

    removed, summary = pc.check_song_pictures([(pic, "line")], "Song", library=lib, check=check)
    assert removed == [pic] and summary.bad == 1 and not pic.exists()


def test_unavailable_keeps_the_verdicts_that_did_come_back(tmp_path):
    lib = ImageLibrary(tmp_path / "lib")
    good = _png(tmp_path / "s" / "images" / "g.png")
    stuck = _png(tmp_path / "s" / "images" / "u.png", color=(1, 2, 3))

    def check(path):
        if path.name == "u.png":
            raise pc.PictureCheckUnavailable("Pro limit reached")
        return pc.Verdict(True)

    with pytest.raises(pc.PictureCheckUnavailable, match="Pro limit"):
        pc.check_song_pictures([(good, "a"), (stuck, "b")], "Song", library=lib, check=check)
    assert lib.check_verdict(content_id(good)) == ("good", "")
    assert lib.check_verdict(content_id(stuck)) is None and stuck.exists()


def test_bad_pictures_in_a_made_video_are_found_and_recorded_but_never_moved(tmp_path):
    """Before an upload (owner, 2026-11-13): a video made before the check is judged; the render that replaces a bad
    picture moves it out, not the check."""
    lib = ImageLibrary(tmp_path / "lib")
    hand = _png(tmp_path / "s" / "images" / "h.png", color=(200, 0, 0))
    fine = _png(tmp_path / "s" / "images" / "f.png")
    twin = _png(tmp_path / "s" / "images" / "t.png", color=(200, 0, 0))
    asked = []

    def check(path):
        asked.append(path.name)
        return pc.Verdict(False, "a hand with no arm") if path.name in ("h.png", "t.png") else pc.Verdict(True)

    bad = pc.bad_pictures_in([(hand, "Quarters and dimes brought me here"), (fine, "fine"), (twin, "again")],
                             library=lib, check=check)

    assert bad == [("Quarters and dimes brought me here", "a hand with no arm")]
    assert sorted(asked) == ["f.png", "h.png"]                  # the same picture under two lines is judged once
    assert hand.exists() and twin.exists() and fine.exists()
    assert lib.check_verdict(content_id(hand)) == ("bad", "a hand with no arm")
    assert pc.bad_pictures_in([(hand, "x"), (fine, "y")], library=lib, check=check) == [("x", "a hand with no arm")]
    assert sorted(asked) == ["f.png", "h.png"]                  # known verdicts are never sent again


def test_bad_pictures_in_waits_when_a_picture_cannot_be_checked_unless_one_is_already_bad(tmp_path):
    lib = ImageLibrary(tmp_path / "lib")
    good = _png(tmp_path / "s" / "images" / "g.png")
    stuck = _png(tmp_path / "s" / "images" / "u.png", color=(1, 2, 3))
    hand = _png(tmp_path / "s" / "images" / "h.png", color=(200, 0, 0))

    def check(path):
        if path.name == "u.png":
            raise pc.PictureCheckUnavailable("Pro limit reached")
        return pc.Verdict(path.name != "h.png", "a hand with no arm" if path.name == "h.png" else "")

    with pytest.raises(pc.PictureCheckUnavailable, match="Pro limit"):
        pc.bad_pictures_in([(good, "a"), (stuck, "b")], library=lib, check=check)
    assert lib.check_verdict(content_id(good)) == ("good", "")         # what came back is kept
    assert pc.bad_pictures_in([(stuck, "b"), (hand, "c")], library=lib, check=check) == [("c", "a hand with no arm")]
