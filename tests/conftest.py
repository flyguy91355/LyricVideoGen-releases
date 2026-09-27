import atexit
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

# --- the test run never reads or writes the owner's real home folder -----------------------------------------
# Many modules compute their state files from Path.home() AT IMPORT TIME, several as function DEFAULT ARGUMENTS
# (`def load_x(path=STATE_FILE)`), which a later monkeypatch of the module constant cannot reach. A test run once
# left cleared_songs.json, redone_songs.json and youtube_pending_comments.json with test songs in the real
# ~/.playalongvideoproduction/. So, BEFORE any project module is imported (this conftest loads before every test
# module), HOME/USERPROFILE point at a throwaway session home -- every import-time path lands there -- and the
# autouse fixture below then gives each test its own fresh home, redirecting every such constant and default.
# Caches that hold real model weights stay where they are (XDG_CACHE_HOME, the parent of torch's and Hugging
# Face's caches), and git keeps reading the owner's global config (safe.directory, identity).
_REAL_HOME = Path.home()
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="playalong-test-home-"))
os.environ.setdefault("XDG_CACHE_HOME", str(_REAL_HOME / ".cache"))
if "GIT_CONFIG_GLOBAL" not in os.environ and (_REAL_HOME / ".gitconfig").is_file():
    os.environ["GIT_CONFIG_GLOBAL"] = str(_REAL_HOME / ".gitconfig")
for _var in ("HOME", "USERPROFILE"):
    os.environ[_var] = str(_SESSION_HOME)

_PROJECT_MODULE_PREFIXES = ("lyricvideo", "deep_review", "scripts")
# The owner's real per-user folders, in case a module was somehow imported before the home moved.
_REAL_HOME_APP_DIRS = (".playalongvideoproduction", "PlayAlongVideoProductionImages")


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)


atexit.register(shutil.rmtree, _SESSION_HOME, True)       # again at exit, in case something still held it open


# Mirrors lyricvideo.pipeline.default_font()'s own candidate list: Linux DejaVu/
# Liberation first, then the bold fonts every Windows install ships with. Without
# the Windows entries, every render/assemble/chord-diagram test silently skipped
# on Windows (73 of them, found 2026-09-14) -- a whole layer of the suite that
# looked green because it never ran.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
]


@pytest.fixture
def test_font_path():
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    pytest.skip("no truetype font available in this environment")


def _home_relative(value) -> Path | None:
    """The part of `value` (a Path or str) below a home folder, when it is a per-user state path; else None."""
    if isinstance(value, str):
        if not value.startswith((str(_SESSION_HOME), str(_REAL_HOME))):
            return None
        value = Path(value)
    if not isinstance(value, Path):
        return None
    try:
        return value.relative_to(_SESSION_HOME)
    except ValueError:
        pass
    try:
        relative = value.relative_to(_REAL_HOME)
    except ValueError:
        return None
    return relative if relative.parts and relative.parts[0] in _REAL_HOME_APP_DIRS else None


def _relocated(value, home: Path):
    relative = _home_relative(value)
    if relative is None:
        return value
    moved = home / relative
    return str(moved) if isinstance(value, str) else moved


def _functions_of(module: types.ModuleType):
    for value in vars(module).values():
        if isinstance(value, types.FunctionType) and value.__module__ == module.__name__:
            yield value
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for member in vars(value).values():
                member = getattr(member, "__func__", member)          # staticmethod / classmethod
                if isinstance(member, types.FunctionType):
                    yield member


_scanned: dict[str, tuple[list[str], list[types.FunctionType]]] = {}


def _home_bound_names(module: types.ModuleType) -> tuple[list[str], list[types.FunctionType]]:
    """(module attributes, functions with a default argument) holding a per-user state path -- scanned once."""
    found = _scanned.get(module.__name__)
    if found is None:
        attributes = [name for name, value in vars(module).items() if _home_relative(value) is not None]
        functions = [
            func for func in _functions_of(module)
            if any(_home_relative(v) is not None for v in (func.__defaults__ or ()))
            or any(_home_relative(v) is not None for v in (func.__kwdefaults__ or {}).values())
        ]
        found = _scanned[module.__name__] = (attributes, functions)
    return found


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """Each test gets its own empty home: HOME/USERPROFILE, and every already-imported project module's per-user
    path constant AND function default argument that points into a home folder (settings.json, the cleared/redo
    logs, the YouTube token/comment/playlist/quota/upload-count state, the batch folder, dismissed songs, ...).
    A module first imported during the test keeps the throwaway session home -- still never the real one."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in [n for n in sys.modules if n.startswith(_PROJECT_MODULE_PREFIXES)]:
        module = sys.modules.get(name)
        if not isinstance(module, types.ModuleType):
            continue
        attributes, functions = _home_bound_names(module)
        for attribute in attributes:
            monkeypatch.setattr(module, attribute, _relocated(getattr(module, attribute), home))
        for func in functions:
            if func.__defaults__:
                monkeypatch.setattr(func, "__defaults__", tuple(_relocated(v, home) for v in func.__defaults__))
            if func.__kwdefaults__:
                monkeypatch.setattr(func, "__kwdefaults__", {k: _relocated(v, home) for k, v in func.__kwdefaults__.items()})


@pytest.fixture(autouse=True)
def _isolate_redo_log(tmp_path_factory, monkeypatch):
    """Tests that redo songs must never write into the owner's real ~/.playalongvideoproduction/redone_songs.json."""
    monkeypatch.setattr("lyricvideo.redo_log.LOG_FILE", tmp_path_factory.mktemp("redolog") / "redone_songs.json")
    monkeypatch.setattr("lyricvideo.cleared_log.LOG_FILE", tmp_path_factory.mktemp("clearedlog") / "cleared_songs.json")


@pytest.fixture(autouse=True)
def _isolate_image_library(tmp_path_factory, monkeypatch):
    """Tests must never read or write the owner's real ~/PlayAlongVideoProductionImages library."""
    monkeypatch.setenv("PLAYALONG_IMAGE_LIBRARY", str(tmp_path_factory.mktemp("imagelib")))
