"""The test run never touches the owner's real home folder (tests/conftest.py): every test gets its own empty home,
and every per-user state path -- module constants AND function default arguments bound at import time -- points
into it. A run once left test songs in the real ~/.playalongvideoproduction/ (cleared/redo logs, pending
comments)."""

import importlib
import inspect
from pathlib import Path

import pytest

STATE_MODULES = [
    "lyricvideo.settings", "lyricvideo.batch", "lyricvideo.cleared_log", "lyricvideo.redo_log",
    "lyricvideo.dismissed_songs", "lyricvideo.youtube_auth", "lyricvideo.youtube_comment_state",
    "lyricvideo.youtube_playlist_state", "lyricvideo.youtube_quota_state", "lyricvideo.youtube_upload_count_state",
]
APP_DIR = ".playalongvideoproduction"
# Imported at collection, as every real test module does, so the conftest fixture sees them before each test runs.
_MODULES = {name: importlib.import_module(name) for name in STATE_MODULES}


def _state_paths(module):
    """Every Path in the module (constants, and defaults of its functions/methods) that names the app's folder."""
    found = []
    for name, value in vars(module).items():
        if isinstance(value, Path) and APP_DIR in value.parts:
            found.append((name, value))
    for name, func in inspect.getmembers(module, inspect.isfunction):
        if func.__module__ == module.__name__:
            found += [(f"{name}()", v) for v in (func.__defaults__ or ()) if isinstance(v, Path) and APP_DIR in v.parts]
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        for name, member in vars(cls).items():
            func = getattr(member, "__func__", member)
            if inspect.isfunction(func):
                found += [(f"{cls.__name__}.{name}()", v) for v in (func.__defaults__ or ())
                          if isinstance(v, Path) and APP_DIR in v.parts]
    return found


def test_each_test_runs_in_its_own_throwaway_home(tmp_path_factory):
    assert Path.home().is_relative_to(tmp_path_factory.getbasetemp())


@pytest.mark.parametrize("module_name", STATE_MODULES)
def test_every_per_user_state_path_points_into_this_tests_home(module_name):
    module = _MODULES[module_name]
    home = Path.home()
    for label, path in _state_paths(module):
        assert path.is_relative_to(home), f"{module_name}.{label} still points at {path}"


def test_a_settings_save_with_the_default_path_lands_in_the_test_home():
    from lyricvideo.settings import Settings

    Settings(fps=30).save()

    assert (Path.home() / APP_DIR / "settings.json").exists()
    assert Settings.load().fps == 30
