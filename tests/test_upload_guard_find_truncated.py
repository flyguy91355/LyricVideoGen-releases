"""scripts/find_truncated_videos.py on a synthetic work root (loaded from its file; nothing is sent anywhere)."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

from lyricvideo.youtube_state import YoutubeState, save_youtube_state

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "find_truncated_videos.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_script_find_truncated_videos", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    return path


@pytest.fixture
def work_root(tmp_path):
    root = tmp_path / "work"
    _touch(root / "blorp" / "blorp.mp4")                                    # whole
    _touch(root / "blorp" / "easychords" / "blorp-easychords.mp4")          # cut short, already on YouTube
    save_youtube_state(root / "blorp" / "easychords", YoutubeState(video_id="EASY1", uploaded_at="t", title="x"))
    _touch(root / "zingo" / "zingo.mp4")                                    # cut short, local only
    _touch(root / "zingo" / "zingo.previous.mp4")                           # set aside earlier: skipped
    _touch(root / "zingo" / "zingo.mp4.rendering")                          # unfinished render: skipped
    _touch(root / "zingo" / "redo_backup_20260901_000000" / "zingo.mp4")    # a backup copy: skipped
    _touch(root / "quib" / "quib.mp4")                                      # unreadable
    _touch(root / "hush" / "hush.mp4")                                      # no audio track
    return root


_LENGTHS = {
    "blorp.mp4": (200.0, 200.3),
    "blorp-easychords.mp4": (80.0, 330.0),
    "zingo.mp4": (79.5, 301.0),
    "hush.mp4": (120.0, None),
}


def _fake_probe(path):
    name = Path(path).name
    if name not in _LENGTHS:
        raise RuntimeError(f"Could not read back the rendered video {name}")
    return _LENGTHS[name]


def test_it_lists_cut_short_videos_marks_uploaded_ones_and_changes_nothing_by_default(work_root, monkeypatch, capsys):
    script = _load_script()
    probed = []
    monkeypatch.setattr(script, "rendered_stream_seconds", lambda p: probed.append(Path(p).name) or _fake_probe(p))

    assert script.main(["--work-root", str(work_root)]) == 0

    out = capsys.readouterr().out
    assert sorted(probed) == ["blorp-easychords.mp4", "blorp.mp4", "hush.mp4", "quib.mp4", "zingo.mp4"]
    assert "CUT SHORT   blorp/easychords/blorp-easychords.mp4  picture 1:20 of audio 5:30  ON YOUTUBE (EASY1)" in out
    assert "CUT SHORT   zingo/zingo.mp4  picture 1:20 of audio 5:01\n" in out
    assert "UNREADABLE  quib/quib.mp4" in out
    assert "NO AUDIO    hush/hush.mp4" in out
    assert "blorp/blorp.mp4" not in out
    assert "5 videos checked: 2 cut short (1 already on YouTube), 2 without readable audio or unreadable." in out
    assert "DRY RUN" in out
    assert (work_root / "zingo" / "zingo.mp4").exists()
    assert (work_root / "blorp" / "easychords" / "blorp-easychords.mp4").exists()
    assert not list(work_root.rglob("*.truncated*"))


def test_set_aside_renames_only_the_cut_short_files_and_a_second_run_finds_nothing_left(work_root, monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr(script, "rendered_stream_seconds", _fake_probe)

    assert script.main(["--work-root", str(work_root), "--set-aside"]) == 0

    assert not (work_root / "zingo" / "zingo.mp4").exists()
    assert (work_root / "zingo" / "zingo.truncated.mp4").exists()
    assert (work_root / "blorp" / "easychords" / "blorp-easychords.truncated.mp4").exists()
    assert (work_root / "blorp" / "blorp.mp4").exists()                 # whole: untouched
    assert (work_root / "quib" / "quib.mp4").exists()                   # unreadable: never set aside
    assert (work_root / "hush" / "hush.mp4").exists()                   # no audio: never set aside
    assert (work_root / "zingo" / "zingo.previous.mp4").exists()
    assert "SET ASIDE: 2 of 2 renamed" in capsys.readouterr().out

    assert script.main(["--work-root", str(work_root), "--set-aside"]) == 0
    assert "0 cut short" in capsys.readouterr().out


def test_set_aside_never_overwrites_an_earlier_set_aside_copy(tmp_path):
    script = _load_script()
    video = _touch(tmp_path / "zingo.mp4")
    earlier = _touch(tmp_path / "zingo.truncated.mp4")
    earlier.write_bytes(b"earlier")

    target = script.set_aside(video)

    assert target.name == "zingo.truncated-2.mp4"
    assert earlier.read_bytes() == b"earlier"


def test_only_checks_just_that_folder(work_root, monkeypatch, capsys):
    script = _load_script()
    probed = []
    monkeypatch.setattr(script, "rendered_stream_seconds", lambda p: probed.append(Path(p).name) or _fake_probe(p))

    script.main(["--work-root", str(work_root), "--only", "blorp/easychords"])

    assert probed == ["blorp-easychords.mp4"]


def test_a_real_cut_short_mp4_is_found(tmp_path, capsys):
    from lyricvideo.assemble import _ffmpeg_binary

    root = tmp_path / "work"
    (root / "glim").mkdir(parents=True)
    try:
        for name, picture, audio in (("glim.mp4", 1, 5), ("whole.mp4", 2, 2)):
            subprocess.run(
                [_ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", f"color=c=blue:s=64x48:r=24:d={picture}",
                 "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio}",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "glim" / name)],
                check=True, capture_output=True, timeout=60,
            )
    except Exception as e:
        pytest.skip(f"could not make a synthetic mp4 ({type(e).__name__})")

    assert _load_script().main(["--work-root", str(root)]) == 0

    out = capsys.readouterr().out
    assert "CUT SHORT   glim/glim.mp4  picture 0:01 of audio 0:05" in out
    assert "whole.mp4" not in out


def test_only_accepts_a_windows_style_path_and_says_so_when_nothing_matches(work_root, monkeypatch, capsys):
    script = _load_script()
    probed = []
    monkeypatch.setattr(script, "rendered_stream_seconds", lambda p: probed.append(Path(p).name) or _fake_probe(p))

    script.main(["--work-root", str(work_root), "--only", r"blorp\easychords"])
    assert probed == ["blorp-easychords.mp4"]

    assert script.main(["--work-root", str(work_root), "--only", "no-such-song"]) == 1
    assert "No finished video found" in capsys.readouterr().out


def test_a_missing_work_root_is_reported_not_silently_empty(tmp_path, capsys):
    assert _load_script().main(["--work-root", str(tmp_path / "nope")]) == 1
    assert "No work folder" in capsys.readouterr().out
