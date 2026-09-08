# Update Available Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give LyricVideoGen version numbers and a self-update mechanism: the
Tkinter GUI checks once on launch whether a newer version has been released,
shows a banner if so, and can download/apply it and relaunch — mirroring
AITrading's Update Available feature, adapted for a local desktop program.

**Architecture:** A new `lyricvideo/update/` package (`version.py`,
`release_client.py`, `apply.py`) provides pure, independently-testable
version comparison, a GitHub Releases API client, and allow-listed
archive-extraction/copy logic — all ported from AITrading's `src/update/`
and adapted to this project's own paths and conventions (httpx instead of
requests, no severity tiering, no config file). `gui.py` wires these
together using its existing worker-thread + `queue.Queue` +
`root.after`-poll pattern (the same one `_run_worker`/`_poll_queue` already
use for running the video pipeline), so nothing here blocks the Tkinter
mainloop. A new `scripts/cut_release.sh` and a new public/unlisted GitHub
repo (`flyguy91355/LyricVideoGen-releases`) provide the release-distribution
side, matching AITrading's `cut_release.sh` shape.

**Tech Stack:** Python 3.12, Tkinter, httpx (already a dependency — do not
add `requests`), pytest, `gh` CLI (already authenticated as `flyguy91355`).

**Spec:** `docs/superpowers/specs/2026-09-08-update-available-design.md`

## Global Constraints

- Releases repo: `flyguy91355/LyricVideoGen-releases` (public, unlisted, no
  source code, packaged snapshots + release notes only).
- HTTP client: **httpx**, not `requests` — `requests` is not a project
  dependency and must not be added. Use module-level `httpx.get`/`httpx.post`
  with an injectable `http_get` parameter, matching the existing convention
  in `lyricvideo/imagery.py` / `tests/test_imagery.py`.
- **`httpx.get` does NOT follow redirects by default** (unlike `requests`).
  Any call that downloads the release archive itself
  (`https://github.com/.../archive/refs/tags/....tar.gz`, which 302s to
  `codeload.github.com`) MUST pass `follow_redirects=True` or the download
  silently returns an HTML redirect page instead of the tarball.
- No severity tiering (critical/routine) anywhere — one plain "update
  available" notice.
- Update check happens **once, on GUI launch, only** — no periodic
  background re-check, no manual "Check Now" button, no version-history
  panel.
- `VERSION` file at the repo root changes **only** when Apply Update
  actually runs (via `write_local_version`) — never hand-edited, never
  bumped by `cut_release.sh`, never bumped per-commit.
- Allow-list (`lyricvideo/update/apply.py` `ALLOWED_PATH_PREFIXES`):
  `lyricvideo/`, `tests/`, `docs/`, `requirements.txt`, `CLAUDE.md`, plus a
  bare top-level `*.py` or `*.sh` file (e.g. `run_lyricvideogen.sh`).
- Deny-list (`DENIED_PATH_PREFIXES`, always wins over the allow-list):
  `.env`, `songs/`, `work/`, `.venv/`; plus `DENIED_FILENAME_PREFIXES =
  (".env",)` so any `.env*` file is denied no matter where it sits in the
  tree.
- Every LyricVideoGen commit that stages a code file must also stage
  `CLAUDE.md` (enforced by `.githooks/pre-commit`, already installed). Keep
  each task's `CLAUDE.md` addition small (well under the 15-line threshold
  that would also require `docs/CLAUDE_HISTORY.md`) — the final task does
  the consolidated write-up.
- Tests live flat in `tests/`, one file per source module, matching this
  project's existing convention (`test_pdf_parse.py` for `pdf_parse.py`,
  etc.) — not a nested `tests/update/` directory.
- Run tests with `.venv/bin/python -m pytest tests/ -v` from
  `/home/doug/LyricVideoGen`.

---

### Task 1: `VERSION` file and `lyricvideo/update/version.py`

**Files:**
- Create: `/home/doug/LyricVideoGen/VERSION`
- Create: `lyricvideo/update/__init__.py`
- Create: `lyricvideo/update/version.py`
- Test: `tests/test_update_version.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: `parse_version(tag: str) -> tuple[int, ...]`,
  `is_newer(current: str, latest: str) -> bool`,
  `read_local_version(path: str) -> str | None`,
  `write_local_version(path: str, version: str) -> None` — all pure, no
  network, no Tkinter dependency. Task 2 imports `is_newer` from this
  module; Task 4/5 import `read_local_version`/`write_local_version`.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_update_version.py`:

```python
"""Tests for lyricvideo/update/version.py -- pure version comparison and
VERSION file I/O, no network dependency."""

import pytest

from lyricvideo.update.version import (
    parse_version,
    is_newer,
    read_local_version,
    write_local_version,
)


def test_parse_version_with_v_prefix():
    assert parse_version("v1.4.0") == (1, 4, 0)


def test_parse_version_without_v_prefix():
    assert parse_version("2.0.1") == (2, 0, 1)


def test_parse_version_two_part():
    assert parse_version("v1.4") == (1, 4)


def test_parse_version_rejects_non_numeric():
    with pytest.raises(ValueError):
        parse_version("v1.four.0")


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v1.5.0-rc1", (1, 5, 0)),
        ("v1.5.0-hotfix", (1, 5, 0)),
        ("1.5.0-rc1", (1, 5, 0)),
        ("v1.5.0+build7", (1, 5, 0)),
        ("1.2.3b1", (1, 2, 3)),
        ("  v1.5.0-rc1  ", (1, 5, 0)),
    ],
)
def test_parse_version_tolerates_prerelease_suffixes(tag, expected):
    assert parse_version(tag) == expected


@pytest.mark.parametrize("tag", ["v1.four.0", "vfour", "", "   ", "v", "1.2.", "-1.5"])
def test_parse_version_still_raises_on_a_genuinely_unparseable_tag(tag):
    with pytest.raises(ValueError):
        parse_version(tag)


def test_is_newer_true_when_latest_greater():
    assert is_newer("v1.0.0", "v1.1.0") is True


def test_is_newer_false_when_equal():
    assert is_newer("v1.4.0", "v1.4.0") is False


def test_is_newer_false_when_latest_older():
    assert is_newer("v2.0.0", "v1.9.9") is False


def test_is_newer_handles_different_part_counts():
    assert is_newer("v1.4", "v1.4.1") is True


def test_a_suffix_only_retag_is_not_advertised_as_an_update():
    assert is_newer("v1.5.0", "v1.5.0-rc1") is False


def test_read_local_version_returns_stripped_content(tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("v1.4.0\n", encoding="utf-8")
    assert read_local_version(str(version_file)) == "v1.4.0"


def test_read_local_version_returns_none_if_missing(tmp_path):
    version_file = tmp_path / "VERSION"
    assert read_local_version(str(version_file)) is None


def test_write_local_version_creates_file(tmp_path):
    version_file = tmp_path / "VERSION"
    write_local_version(str(version_file), "v1.5.0")
    assert version_file.read_text(encoding="utf-8").strip() == "v1.5.0"


def test_write_local_version_overwrites_existing(tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("v1.4.0", encoding="utf-8")
    write_local_version(str(version_file), "v1.5.0")
    assert version_file.read_text(encoding="utf-8").strip() == "v1.5.0"


def test_version_file_round_trips_non_ascii_content(tmp_path):
    version_file = tmp_path / "VERSION"
    write_local_version(str(version_file), "v1.5.0-rc1 - ünïcode")
    assert read_local_version(str(version_file)) == "v1.5.0-rc1 - ünïcode"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_update_version.py -v`
Expected: FAIL/ERROR — `lyricvideo.update` module doesn't exist yet.

- [ ] **Step 3: Create the package and implementation**

Create `lyricvideo/update/__init__.py` (empty file).

Create `lyricvideo/update/version.py`:

```python
"""Pure version comparison and local VERSION-file I/O for the Update
Available feature. No network, no GUI dependency -- fully unit-testable.
See docs/superpowers/specs/2026-09-08-update-available-design.md."""

from pathlib import Path

_PRERELEASE_SEPARATORS = ("-", "+")
_DIGITS = "0123456789"


def _parse_part(part: str) -> int:
    """One dot-separated segment to an int, tolerating a trailing
    non-numeric pre-release marker ('3b1' -> 3). Raises ValueError if the
    segment has no leading digits at all."""
    digits = ""
    for char in part.strip():
        if char not in _DIGITS:
            break
        digits += char
    if not digits:
        raise ValueError(f"Unparseable version segment: {part!r}")
    return int(digits)


def parse_version(tag: str) -> tuple[int, ...]:
    """Parses a tag like 'v1.4.0' or '1.4.0' into (1, 4, 0). Tolerant of a
    pre-release/build suffix: 'v1.5.0-rc1', 'v1.5.0+build7', and '1.2.3b1'
    all parse to the same tuple as their plain numeric version -- the
    suffix is ignored entirely for comparison, so 'v1.5.0-rc1' compares
    EQUAL to 'v1.5.0'. Still raises ValueError on a segment with no
    leading digits at all (e.g. 'v1.four.0')."""
    cleaned = tag.strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    for separator in _PRERELEASE_SEPARATORS:
        cleaned = cleaned.split(separator, 1)[0]
    parts = cleaned.split(".")
    return tuple(_parse_part(part) for part in parts)


def is_newer(current: str, latest: str) -> bool:
    """True iff latest's parsed version is strictly greater than current's.
    Shorter tuples are padded with zeros so 'v1.4' vs 'v1.4.1' compares as
    (1, 4, 0) < (1, 4, 1) rather than raising."""
    current_parts = parse_version(current)
    latest_parts = parse_version(latest)
    length = max(len(current_parts), len(latest_parts))
    current_padded = current_parts + (0,) * (length - len(current_parts))
    latest_padded = latest_parts + (0,) * (length - len(latest_parts))
    return latest_padded > current_padded


def read_local_version(path: str) -> str | None:
    """Returns the stripped contents of the local VERSION file, or None if
    it doesn't exist yet."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    return file_path.read_text(encoding="utf-8").strip()


def write_local_version(path: str, version: str) -> None:
    """Writes version (stripped) to the local VERSION file, creating it if
    needed."""
    Path(path).write_text(version.strip() + "\n", encoding="utf-8")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_update_version.py -v`
Expected: all PASS.

- [ ] **Step 5: Create the VERSION file**

Create `/home/doug/LyricVideoGen/VERSION` with exactly:

```
v1.0.0
```

- [ ] **Step 6: Add an in-progress note to CLAUDE.md**

Open `CLAUDE.md` and add a new section right after the "## Tests" section:

```markdown
## Update Available Feature (in progress)

See `docs/superpowers/specs/2026-09-08-update-available-design.md`. `VERSION`
at the repo root tracks the last version actually applied to this checkout
(never hand-edited). `lyricvideo/update/version.py` provides version
parsing/comparison and VERSION-file I/O.
```

- [ ] **Step 7: Commit**

```bash
cd /home/doug/LyricVideoGen
git add VERSION lyricvideo/update/__init__.py lyricvideo/update/version.py tests/test_update_version.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Add VERSION file and lyricvideo/update/version.py

First piece of the Update Available feature (see
docs/superpowers/specs/2026-09-08-update-available-design.md): pure
version parsing/comparison and VERSION-file I/O, ported from
AITrading's src/update/version.py with no behavior changes.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `lyricvideo/update/release_client.py`

**Files:**
- Create: `lyricvideo/update/release_client.py`
- Test: `tests/test_update_release_client.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `is_newer` from `lyricvideo/update/version.py` (Task 1).
- Produces: `RELEASES_REPO: str` (module constant),
  `fetch_latest_release(repo: str = RELEASES_REPO, http_get=None) -> dict`
  returning `{"tag_name": str, "notes": str, "download_url": str}`,
  `check_for_update(current_version: str, repo: str = RELEASES_REPO,
  http_get=None) -> dict | None`. Task 4/5 import both `check_for_update`
  and `RELEASES_REPO`. Both functions raise on any HTTP error or an invalid
  tag — they do not swallow exceptions themselves.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_update_release_client.py`:

```python
"""Tests for lyricvideo/update/release_client.py -- the GitHub Releases API
client, with the HTTP call injected so no real network access is needed."""

import pytest

from lyricvideo.update.release_client import (
    RELEASES_REPO,
    fetch_latest_release,
    check_for_update,
)


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_releases_repo_constant():
    assert RELEASES_REPO == "flyguy91355/LyricVideoGen-releases"


def test_fetch_latest_release_parses_response():
    def fake_get(url, timeout=None):
        assert url == "https://api.github.com/repos/flyguy91355/LyricVideoGen-releases/releases/latest"
        return _FakeResponse({
            "tag_name": "v1.1.0",
            "body": "Adds a Relaunch Now button.",
        })

    result = fetch_latest_release(http_get=fake_get)
    assert result == {
        "tag_name": "v1.1.0",
        "notes": "Adds a Relaunch Now button.",
        "download_url": (
            "https://github.com/flyguy91355/LyricVideoGen-releases"
            "/archive/refs/tags/v1.1.0.tar.gz"
        ),
    }


def test_fetch_latest_release_strips_missing_body_to_empty_notes():
    def fake_get(url, timeout=None):
        return _FakeResponse({"tag_name": "v1.1.0"})

    result = fetch_latest_release(http_get=fake_get)
    assert result["notes"] == ""


def test_fetch_latest_release_raises_on_http_error():
    def fake_get(url, timeout=None):
        return _FakeResponse({}, status_code=404)

    with pytest.raises(RuntimeError):
        fetch_latest_release(http_get=fake_get)


@pytest.mark.parametrize(
    "hostile_tag",
    [
        "../../../../etc/passwd",
        "v1.0.0/../evil",
        "v1.0.0..",
        "v1.0.0/extra",
        "v1.0.0\\evil",
        "v1.0.0 evil",
        "",
        "latest",
    ],
)
def test_hostile_or_unexpected_tags_are_refused(hostile_tag):
    def fake_get(url, timeout=None):
        return _FakeResponse({"tag_name": hostile_tag, "body": ""})

    with pytest.raises(ValueError):
        fetch_latest_release(http_get=fake_get)


def test_a_non_string_tag_is_refused():
    def fake_get(url, timeout=None):
        return _FakeResponse({"tag_name": None, "body": ""})

    with pytest.raises(ValueError):
        fetch_latest_release(http_get=fake_get)


def test_check_for_update_returns_release_when_newer():
    def fake_get(url, timeout=None):
        return _FakeResponse({"tag_name": "v1.1.0", "body": "Notes."})

    result = check_for_update("v1.0.0", http_get=fake_get)
    assert result["tag_name"] == "v1.1.0"


def test_check_for_update_returns_none_when_not_newer():
    def fake_get(url, timeout=None):
        return _FakeResponse({"tag_name": "v1.0.0", "body": "Notes."})

    assert check_for_update("v1.0.0", http_get=fake_get) is None


def test_check_for_update_returns_none_when_current_is_ahead():
    def fake_get(url, timeout=None):
        return _FakeResponse({"tag_name": "v0.9.0", "body": "Notes."})

    assert check_for_update("v1.0.0", http_get=fake_get) is None


def test_check_for_update_raises_on_fetch_failure():
    def fake_get(url, timeout=None):
        return _FakeResponse({}, status_code=500)

    with pytest.raises(RuntimeError):
        check_for_update("v1.0.0", http_get=fake_get)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_update_release_client.py -v`
Expected: FAIL/ERROR — `release_client` module doesn't exist yet.

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/update/release_client.py`:

```python
"""GitHub Releases API client for the LyricVideoGen-releases distribution
repo. The HTTP call is injectable (http_get param) so tests never hit the
real network -- mirrors this project's existing injected-http-client
convention (see lyricvideo/imagery.py and tests/test_imagery.py's
_FakeHttpClient). Uses httpx, not requests -- requests is not a project
dependency. See
docs/superpowers/specs/2026-09-08-update-available-design.md."""

import re

import httpx

from .version import is_newer

RELEASES_REPO = "flyguy91355/LyricVideoGen-releases"

_DEFAULT_TIMEOUT_SECS = 10

# An optional "v", a dotted numeric version, and an optional pre-release/
# build suffix built only from characters that can't change a URL's shape.
_VALID_TAG_PATTERN = re.compile(r"v?\d+(?:\.\d+)*[0-9A-Za-z._+-]*")


def _validated_tag(tag) -> str:
    """Returns the stripped tag if it looks like a real release tag, else
    raises ValueError. tag_name arrives from the GitHub API -- a trust
    boundary, even for a self-controlled releases repo -- and is
    interpolated straight into a URL that gets downloaded and unpacked
    over this program's own files. A tag containing "/" or ".." could
    point that URL at an entirely different path."""
    if not isinstance(tag, str):
        raise ValueError(f"Release tag_name is not a string: {tag!r}")
    stripped = tag.strip()
    if ".." in stripped or not _VALID_TAG_PATTERN.fullmatch(stripped):
        raise ValueError(f"Refusing to build a download URL for release tag: {tag!r}")
    return stripped


def fetch_latest_release(repo: str = RELEASES_REPO, http_get=None) -> dict:
    """Fetches the latest release from the given public repo via GitHub's
    public Releases API -- no credential needed since the distribution repo
    is public. Raises on any HTTP error, and on an unvalidated tag (see
    _validated_tag)."""
    getter = http_get or httpx.get
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    response = getter(url, timeout=_DEFAULT_TIMEOUT_SECS)
    response.raise_for_status()
    data = response.json()
    tag = _validated_tag(data["tag_name"])
    download_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz"
    return {
        "tag_name": tag,
        "notes": (data.get("body") or "").strip(),
        "download_url": download_url,
    }


def check_for_update(current_version: str, repo: str = RELEASES_REPO, http_get=None) -> dict | None:
    """Returns the latest release dict if it's newer than current_version,
    else None. Raises on any fetch failure (network, HTTP error,
    unparseable tag) -- the caller (the GUI's launch-time background
    check) is responsible for treating a failure as 'no update info
    available' rather than crashing; this function stays honest about
    failures instead of silently swallowing them."""
    release = fetch_latest_release(repo, http_get=http_get)
    if is_newer(current_version, release["tag_name"]):
        return release
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_update_release_client.py -v`
Expected: all PASS.

- [ ] **Step 5: Extend the CLAUDE.md note**

In `CLAUDE.md`, extend the "## Update Available Feature (in progress)"
section added in Task 1 by adding one sentence at the end:

```markdown
`lyricvideo/update/release_client.py` fetches the latest release from
`flyguy91355/LyricVideoGen-releases` via GitHub's public Releases API
(httpx, injectable `http_get`); `check_for_update()` combines it with
`is_newer()`.
```

- [ ] **Step 6: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/update/release_client.py tests/test_update_release_client.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Add lyricvideo/update/release_client.py

GitHub Releases API client for flyguy91355/LyricVideoGen-releases,
using httpx (this project's existing HTTP convention) instead of
AITrading's requests-based version. No severity tiering -- the whole
release body is the notes, verbatim.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `lyricvideo/update/apply.py`

**Files:**
- Create: `lyricvideo/update/apply.py`
- Test: `tests/test_update_apply.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: `ALLOWED_PATH_PREFIXES`, `DENIED_PATH_PREFIXES`,
  `DENIED_FILENAME_PREFIXES` (tuples), `is_path_updatable(relative_path:
  str) -> bool`, `requirements_changed(old_content: str, new_content: str)
  -> bool`, `extract_release_archive(archive_path: str, dest_dir: str) ->
  str`, `copy_updatable_files(source_dir: str, target_dir: str) ->
  list[str]`. Task 5 imports `extract_release_archive`,
  `copy_updatable_files`, and `requirements_changed`.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_update_apply.py`:

```python
"""Tests for lyricvideo/update/apply.py -- path allow/deny logic for what
an applied update is permitted to touch, and requirements.txt change
detection."""

import tarfile
from pathlib import Path

from lyricvideo.update.apply import (
    is_path_updatable,
    requirements_changed,
    extract_release_archive,
    copy_updatable_files,
)


def test_lyricvideo_package_files_are_updatable():
    assert is_path_updatable("lyricvideo/gui.py") is True


def test_tests_files_are_updatable():
    assert is_path_updatable("tests/test_gui.py") is True


def test_docs_files_are_updatable():
    assert is_path_updatable("docs/CLAUDE_HISTORY.md") is True


def test_requirements_txt_is_updatable():
    assert is_path_updatable("requirements.txt") is True


def test_claude_md_is_updatable():
    assert is_path_updatable("CLAUDE.md") is True


def test_top_level_py_file_is_updatable():
    assert is_path_updatable("start.py") is True


def test_top_level_sh_file_is_updatable():
    assert is_path_updatable("run_lyricvideogen.sh") is True


def test_env_file_is_never_updatable():
    assert is_path_updatable(".env") is False


def test_songs_directory_is_never_updatable():
    assert is_path_updatable("songs/wish-you-were-here.pdf") is False


def test_work_directory_is_never_updatable():
    assert is_path_updatable("work/some-song/final.mp4") is False


def test_venv_directory_is_never_updatable():
    assert is_path_updatable(".venv/bin/python") is False


def test_random_top_level_file_is_not_updatable():
    assert is_path_updatable("README.md") is False


def test_env_variant_is_denied_anywhere_in_the_tree():
    assert is_path_updatable("lyricvideo/.env.local") is False


def test_traversal_path_is_never_updatable():
    assert is_path_updatable("../outside.py") is False
    assert is_path_updatable("lyricvideo/../../outside.py") is False


def test_absolute_path_is_never_updatable():
    assert is_path_updatable("/etc/passwd") is False


def test_requirements_changed_true_when_different():
    assert requirements_changed("httpx>=0.27\n", "httpx>=0.28\n") is True


def test_requirements_changed_false_when_identical():
    content = "httpx>=0.27\nanthropic>=0.34\n"
    assert requirements_changed(content, content) is False


def test_requirements_changed_ignores_trailing_whitespace_differences():
    assert requirements_changed("httpx>=0.27\n", "httpx>=0.27") is False


def _make_fake_release_tarball(tmp_path):
    source_root = tmp_path / "flyguy91355-LyricVideoGen-abc1234"
    (source_root / "lyricvideo").mkdir(parents=True)
    (source_root / "songs").mkdir()
    (source_root / "lyricvideo" / "gui.py").write_text("# new gui.py\n", encoding="utf-8")
    (source_root / "requirements.txt").write_text("httpx>=0.28\n", encoding="utf-8")
    (source_root / "songs" / "sneaky.pdf").write_text("should never be copied", encoding="utf-8")
    (source_root / "README.md").write_text("not updatable\n", encoding="utf-8")

    archive_path = tmp_path / "release.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_root, arcname=source_root.name)
    return archive_path


def test_extract_release_archive_returns_top_level_dir(tmp_path):
    archive_path = _make_fake_release_tarball(tmp_path)
    dest_dir = tmp_path / "extracted"
    dest_dir.mkdir()

    extracted_root = extract_release_archive(str(archive_path), str(dest_dir))

    assert (Path(extracted_root) / "lyricvideo" / "gui.py").exists()


def test_copy_updatable_files_only_copies_allowed_paths(tmp_path):
    archive_path = _make_fake_release_tarball(tmp_path)
    dest_dir = tmp_path / "extracted"
    dest_dir.mkdir()
    extracted_root = extract_release_archive(str(archive_path), str(dest_dir))

    target_dir = tmp_path / "live_install"
    target_dir.mkdir()

    copied = copy_updatable_files(extracted_root, str(target_dir))

    assert sorted(copied) == ["lyricvideo/gui.py", "requirements.txt"]
    assert (target_dir / "lyricvideo" / "gui.py").read_text(encoding="utf-8") == "# new gui.py\n"
    assert not (target_dir / "songs").exists()
    assert not (target_dir / "README.md").exists()


def test_copy_updatable_files_never_overwrites_denied_paths_even_if_present(tmp_path):
    source_root = tmp_path / "release_source"
    (source_root / "songs").mkdir(parents=True)
    (source_root / "songs" / "sneaky.pdf").write_text("should never be copied", encoding="utf-8")
    (source_root / "lyricvideo").mkdir()
    (source_root / "lyricvideo" / "gui.py").write_text("# new gui.py\n", encoding="utf-8")

    target_dir = tmp_path / "live_install"
    (target_dir / "songs").mkdir(parents=True)
    (target_dir / "songs" / "real_song.pdf").write_text(
        "owner's real song -- must survive", encoding="utf-8"
    )

    copy_updatable_files(str(source_root), str(target_dir))

    assert (target_dir / "songs" / "real_song.pdf").read_text(encoding="utf-8") == (
        "owner's real song -- must survive"
    )
    assert not (target_dir / "songs" / "sneaky.pdf").exists()


def test_symlinked_destination_is_never_overwritten(tmp_path):
    target_dir = tmp_path / "live_install"
    (target_dir / "lyricvideo").mkdir(parents=True)
    real_env = target_dir / ".env"
    real_env.write_text("SECRET=1", encoding="utf-8")
    symlink_path = target_dir / "lyricvideo" / "gui.py"
    symlink_path.symlink_to(real_env)

    source_root = tmp_path / "release_source"
    (source_root / "lyricvideo").mkdir(parents=True)
    (source_root / "lyricvideo" / "gui.py").write_text(
        "# malicious overwrite attempt\n", encoding="utf-8"
    )

    copy_updatable_files(str(source_root), str(target_dir))

    assert real_env.read_text(encoding="utf-8") == "SECRET=1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_update_apply.py -v`
Expected: FAIL/ERROR — `apply` module doesn't exist yet.

- [ ] **Step 3: Write the implementation**

Create `lyricvideo/update/apply.py`:

```python
"""Applying a downloaded release to the program's own files: what's safe to
touch, and how the archive gets extracted and copied. Deny-list always
wins over allow-list -- this program's input songs, generated work files,
managed virtualenv, and credentials must never be touched by an applied
update. Ported from AITrading's src/update/apply.py; only the path lists
below are LyricVideoGen-specific, the traversal/symlink-safety logic is
unchanged. See
docs/superpowers/specs/2026-09-08-update-available-design.md."""

import logging
import ntpath
import shutil
import tarfile
from pathlib import Path

logger = logging.getLogger(__name__)

DENIED_PATH_PREFIXES = (
    ".env",
    "songs/",
    "work/",
    ".venv/",
)

# Deliberately looser than the boundary rule above: ".env.local" etc. are
# real credential-adjacent files, so any file whose basename starts with
# ".env" is denied outright no matter where it sits.
DENIED_FILENAME_PREFIXES = (".env",)

ALLOWED_PATH_PREFIXES = (
    "lyricvideo/",
    "tests/",
    "docs/",
    "requirements.txt",
    "CLAUDE.md",
)


def _matches_path_entry(normalized: str, entry: str) -> bool:
    """Directory-boundary-aware match: a trailing-slash entry ("songs/")
    matches anything inside that directory; a bare entry ("CLAUDE.md")
    matches that exact path, or something genuinely nested under it --
    never a longer sibling name such as "CLAUDE.md.bak"."""
    if entry.endswith("/"):
        return normalized.startswith(entry)
    return normalized == entry or normalized.startswith(entry + "/")


def _is_traversal_unsafe(normalized: str) -> bool:
    """True for any path that isn't a plain relative path inside the
    install root: empty, absolute (POSIX, Windows drive, or UNC), or
    containing a ".." component."""
    if not normalized:
        return True
    if normalized.startswith("/"):
        return True
    if ntpath.splitdrive(normalized)[0]:
        return True
    return any(part == ".." for part in normalized.split("/"))


def is_path_updatable(relative_path: str) -> bool:
    """True iff an update is allowed to overwrite this path (relative to
    the repo root). Checked in order: traversal/absolute-path rejection,
    then the deny-list (always wins), then the explicit allow-list, then a
    fallback rule for a bare top-level *.py or *.sh file (e.g.
    run_lyricvideogen.sh)."""
    normalized = relative_path.replace("\\", "/")

    if _is_traversal_unsafe(normalized):
        return False

    basename = normalized.rsplit("/", 1)[-1]
    for denied_name in DENIED_FILENAME_PREFIXES:
        if basename.startswith(denied_name):
            return False

    for denied in DENIED_PATH_PREFIXES:
        if _matches_path_entry(normalized, denied):
            return False

    for allowed in ALLOWED_PATH_PREFIXES:
        if _matches_path_entry(normalized, allowed):
            return True

    if "/" not in normalized and (normalized.endswith(".py") or normalized.endswith(".sh")):
        return True

    return False


def requirements_changed(old_content: str, new_content: str) -> bool:
    """True iff the two requirements.txt contents differ, ignoring
    leading/trailing whitespace (a trailing-newline-only diff shouldn't
    trigger a real pip install)."""
    return old_content.strip() != new_content.strip()


def extract_release_archive(archive_path: str, dest_dir: str) -> str:
    """Extracts a .tar.gz release archive into dest_dir and returns the
    path to its single top-level directory (GitHub's auto-generated
    release tarballs always have exactly one)."""
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(dest_dir, filter="data")
    dest_path = Path(dest_dir)
    top_level_dirs = [entry for entry in dest_path.iterdir() if entry.is_dir()]
    if len(top_level_dirs) != 1:
        raise ValueError(
            f"Expected exactly one top-level directory in the release archive, "
            f"found {len(top_level_dirs)}"
        )
    return str(top_level_dirs[0])


def _safe_destination(target_root: Path, relative: str) -> Path | None:
    """Where `relative` may actually be written under target_root, or None
    if writing there wouldn't land where the allow-list thinks it does.
    shutil.copy2 FOLLOWS a symlink at the destination and writes straight
    through it, so a symlink planted at an allow-listed path pointing at
    .env (or songs/, or work/) would overwrite that denied location while
    every string-level check still reported "allowed"."""
    destination = target_root / relative
    if destination.is_symlink():
        return None
    try:
        resolved_root = target_root.resolve()
        real_relative = destination.resolve().relative_to(resolved_root).as_posix()
    except (OSError, ValueError):
        return None
    if not is_path_updatable(real_relative):
        return None
    return destination


def copy_updatable_files(source_dir: str, target_dir: str) -> list[str]:
    """Walks source_dir, copies every file whose path (relative to
    source_dir) passes is_path_updatable() into the same relative path
    under target_dir, creating parent directories as needed. Returns the
    sorted list of relative paths actually copied. Never touches anything
    outside that allow-list, even if the source tree contains a denied
    path -- the deny check in is_path_updatable() is authoritative
    regardless of what's on disk in target_dir already."""
    source_path = Path(source_dir)
    target_path = Path(target_dir)
    copied: list[str] = []

    for file_path in source_path.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(source_path).as_posix()
        if not is_path_updatable(relative):
            continue
        destination = _safe_destination(target_path, relative)
        if destination is None:
            logger.warning(
                "Update: refusing to write %s -- the destination is a symlink or "
                "resolves outside the allow-listed install path",
                relative,
            )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, destination)
        copied.append(relative)

    return sorted(copied)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_update_apply.py -v`
Expected: all PASS.

- [ ] **Step 5: Extend the CLAUDE.md note**

Extend the "## Update Available Feature (in progress)" section in
`CLAUDE.md` with:

```markdown
`lyricvideo/update/apply.py` extracts a downloaded release archive and
copies only allow-listed paths (`lyricvideo/`, `tests/`, `docs/`,
`requirements.txt`, `CLAUDE.md`, a bare top-level `*.py`/`*.sh`) onto the
repo root, refusing `.env`, `songs/`, `work/`, `.venv/`, and any
path-traversal or symlink-destination escape.
```

- [ ] **Step 6: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/update/apply.py tests/test_update_apply.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Add lyricvideo/update/apply.py

Allow/deny-listed archive extraction and file copying for the Update
Available feature, ported from AITrading's src/update/apply.py with
LyricVideoGen's own allow-list (lyricvideo/, tests/, docs/,
requirements.txt, CLAUDE.md) and deny-list (.env, songs/, work/,
.venv/) -- the traversal/symlink-safety logic itself is unchanged.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: GUI — version in title, launch-time check, banner

**Files:**
- Modify: `lyricvideo/gui.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `read_local_version` (Task 1), `check_for_update`,
  `RELEASES_REPO` (Task 2).
- Produces: `LyricVideoGUI._available_update: dict | None` attribute,
  `LyricVideoGUI._update_queue: queue.Queue` attribute — Task 5 pushes
  `("apply_status"|"apply_done"|"apply_error", payload)` tuples onto this
  same queue and reads `self._available_update` when the banner is
  clicked.

This task has no new automated tests of its own — it's Tkinter wiring, and
this project's own convention (see `tests/test_gui.py`, which only tests
`gui.py`'s pure helper functions) is to leave that kind of wiring untested
directly. Verify it by hand at the end of this task.

- [ ] **Step 1: Add the new imports**

In `lyricvideo/gui.py`, the current imports (lines 1-15) are:

```python
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from dotenv import load_dotenv

from .pipeline import run_pipeline, slugify as _slugify
```

Replace that whole block with:

```python
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import httpx
from dotenv import load_dotenv

from .pipeline import run_pipeline, slugify as _slugify
from .update.apply import copy_updatable_files, extract_release_archive, requirements_changed
from .update.release_client import RELEASES_REPO, check_for_update
from .update.version import read_local_version, write_local_version
```

- [ ] **Step 2: Add the VERSION file path constant**

Find this line (currently around line 44):

```python
PROJECT_ROOT = Path(__file__).resolve().parent.parent
```

Add directly below it:

```python
_VERSION_FILE_PATH = PROJECT_ROOT / "VERSION"
```

- [ ] **Step 3: Update `__init__` — window title, new attributes, start the check**

Find the current `__init__` body:

```python
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("LyricVideoGen")
        root.geometry("760x600")

        self._queue: "queue.Queue" = queue.Queue()
        self._running = False
        self._log_pending = ""
        self._log_has_uncommitted_line = False
```

Replace with:

```python
    def __init__(self, root: tk.Tk):
        self.root = root
        current_version = read_local_version(str(_VERSION_FILE_PATH)) or "v0.0.0"
        root.title(f"LyricVideoGen {current_version}")
        root.geometry("760x600")

        self._queue: "queue.Queue" = queue.Queue()
        self._update_queue: "queue.Queue" = queue.Queue()
        self._current_version = current_version
        self._available_update: dict | None = None
        self._running = False
        self._log_pending = ""
        self._log_has_uncommitted_line = False
```

Then find the end of `__init__`:

```python
        self._build_widgets()
        self._check_api_keys()
```

Replace with:

```python
        self._build_widgets()
        self._check_api_keys()
        self._start_update_check()
```

- [ ] **Step 4: Save the top frame reference for the banner to pack above**

In `_build_widgets`, find:

```python
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", **pad)
        frame.columnconfigure(1, weight=1)
```

Replace with:

```python
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        self.update_banner_var = tk.StringVar()
        self.update_banner = ttk.Label(
            self.root,
            textvariable=self.update_banner_var,
            foreground="#0060c0",
            cursor="hand2",
            anchor="w",
        )
        self.update_banner.bind("<Button-1>", self._on_update_banner_clicked)

        frame = ttk.Frame(self.root)
        frame.pack(fill="x", **pad)
        frame.columnconfigure(1, weight=1)
        self.top_frame = frame
```

(`self.update_banner` is created but deliberately not packed here — it's
packed only once an update is actually available, in Step 6 below.)

- [ ] **Step 5: Add the launch-time check and its worker**

Add these two new methods to `LyricVideoGUI`, right after `_check_api_keys`:

```python
    def _start_update_check(self) -> None:
        thread = threading.Thread(target=self._update_check_worker, daemon=True)
        thread.start()
        self.root.after(200, self._poll_update_queue)

    def _update_check_worker(self) -> None:
        try:
            release = check_for_update(self._current_version, RELEASES_REPO)
        except Exception:
            # Offline, GitHub hiccup, or no releases cut yet -- the launch-
            # time check must never surface an error or crash the GUI.
            return
        if release is not None:
            self._update_queue.put(("available", release))
```

- [ ] **Step 6: Add the update-queue poll loop and banner click handler**

Add this method right after `_update_check_worker`:

```python
    def _poll_update_queue(self) -> None:
        try:
            while True:
                kind, payload = self._update_queue.get_nowait()
                if kind == "available":
                    self._available_update = payload
                    self.update_banner_var.set(
                        f"Update available: {payload['tag_name']} — click for details"
                    )
                    self.update_banner.pack(
                        fill="x", padx=8, pady=(4, 0), before=self.top_frame
                    )
                elif kind == "apply_status":
                    if hasattr(self, "_update_status_var"):
                        self._update_status_var.set(payload)
                elif kind == "apply_done":
                    self._on_apply_update_done(payload)
                elif kind == "apply_error":
                    self._on_apply_update_error(payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_update_queue)

    def _on_update_banner_clicked(self, _event=None) -> None:
        if self._available_update is not None:
            self._open_update_dialog(self._available_update)
```

- [ ] **Step 7: `_open_update_dialog`, `_on_apply_update_done`, and
  `_on_apply_update_error` are added in Task 5** — for this task, add a
  temporary placeholder so the file stays valid:

Add this method right after `_on_update_banner_clicked`:

```python
    def _open_update_dialog(self, release: dict) -> None:
        raise NotImplementedError("wired in Task 5")

    def _on_apply_update_done(self, tag_name: str) -> None:
        raise NotImplementedError("wired in Task 5")

    def _on_apply_update_error(self, message: str) -> None:
        raise NotImplementedError("wired in Task 5")
```

(These three are placeholders only — Task 5 replaces every one of them
with real implementations before the feature is usable end-to-end. They
exist here so `_on_update_banner_clicked` and `_poll_update_queue` have
something to call and the module imports cleanly.)

- [ ] **Step 8: Run the existing test suite to confirm nothing broke**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_gui.py -v`
Expected: all PASS (these tests only exercise `_slugify`/`_split_log_text`,
unaffected by this task).

- [ ] **Step 9: Manually verify the launch-time check and banner**

Since `flyguy91355/LyricVideoGen-releases` doesn't exist yet (created in
Task 6), `check_for_update` will raise inside `_update_check_worker`,
which swallows it — so at this point in the plan, launching the GUI should
show **no** banner and no error. Confirm that:

```bash
cd /home/doug/LyricVideoGen && ./run_lyricvideogen.sh
```

The window title should read `LyricVideoGen v1.0.0`, the GUI should open
normally with no banner, and generating a video should still work exactly
as before. Close the window when confirmed.

- [ ] **Step 10: Extend the CLAUDE.md note**

Extend the "## Update Available Feature (in progress)" section with:

```markdown
`gui.py` shows the current version in its window title and checks for a
newer release once on launch (background thread), showing a clickable
banner if one exists.
```

- [ ] **Step 11: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/gui.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Wire launch-time update check and banner into the GUI

Window title now shows the current version; a background thread
checks flyguy91355/LyricVideoGen-releases once on launch and shows a
clickable banner if a newer release exists. The banner's dialog is
wired in the next commit -- clicking it currently raises
NotImplementedError, matching the plan's task boundary.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: GUI — Apply Update dialog, apply worker, Relaunch Now

**Files:**
- Modify: `lyricvideo/gui.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `extract_release_archive`, `copy_updatable_files`,
  `requirements_changed` (Task 3); `write_local_version` (Task 1);
  `self._update_queue`, `self._available_update` (Task 4).
- Produces: a fully working Apply Update flow. No new interfaces consumed
  by later tasks.

No new automated tests — same rationale as Task 4 (Tkinter wiring only;
`apply.py`'s functions this calls are already unit-tested in Task 3).
Verified by hand at the end of this task and again in Task 7's end-to-end
check.

- [ ] **Step 1: Replace the three placeholder methods from Task 4**

In `lyricvideo/gui.py`, find the placeholders added in Task 4:

```python
    def _open_update_dialog(self, release: dict) -> None:
        raise NotImplementedError("wired in Task 5")

    def _on_apply_update_done(self, tag_name: str) -> None:
        raise NotImplementedError("wired in Task 5")

    def _on_apply_update_error(self, message: str) -> None:
        raise NotImplementedError("wired in Task 5")
```

Replace all three with:

```python
    def _open_update_dialog(self, release: dict) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Update available: {release['tag_name']}")
        dialog.geometry("480x360")
        self._update_dialog_window = dialog

        notes_widget = scrolledtext.ScrolledText(dialog, wrap="word", height=14)
        notes_widget.insert("1.0", release.get("notes", "") or "(no release notes)")
        notes_widget.configure(state="disabled")
        notes_widget.pack(fill="both", expand=True, padx=8, pady=8)

        self._update_status_var = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=self._update_status_var, foreground="#666").pack(
            anchor="w", padx=8
        )

        self._update_button_frame = ttk.Frame(dialog)
        self._update_button_frame.pack(fill="x", padx=8, pady=8)

        self._update_apply_button = ttk.Button(
            self._update_button_frame,
            text="Apply Update",
            command=lambda: self._on_apply_update_clicked(release),
        )
        self._update_apply_button.pack(side="left")
        ttk.Button(self._update_button_frame, text="Close", command=dialog.destroy).pack(
            side="right"
        )

        def _on_close() -> None:
            self._update_dialog_window = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", _on_close)

    def _on_apply_update_clicked(self, release: dict) -> None:
        if not messagebox.askyesno(
            "Apply update",
            f"Download and apply {release['tag_name']} now?\n\n"
            "This reinstalls dependencies if they changed and overwrites the "
            "program's own files. Your songs, work files, and .env are never "
            "touched.",
        ):
            return
        self._update_apply_button.state(["disabled"])
        self._update_status_var.set("Downloading...")
        thread = threading.Thread(
            target=self._apply_update_worker, args=(release,), daemon=True
        )
        thread.start()

    def _apply_update_worker(self, release: dict) -> None:
        try:
            self._update_queue.put(("apply_status", "Downloading..."))
            # follow_redirects=True is required: unlike requests, httpx does
            # NOT follow redirects by default, and this URL 302s to
            # codeload.github.com.
            response = httpx.get(release["download_url"], timeout=60, follow_redirects=True)
            response.raise_for_status()

            with tempfile.TemporaryDirectory() as tmp_dir:
                archive_path = Path(tmp_dir) / "release.tar.gz"
                archive_path.write_bytes(response.content)

                self._update_queue.put(("apply_status", "Extracting..."))
                extract_dir = Path(tmp_dir) / "extracted"
                extract_dir.mkdir()
                extracted_root = extract_release_archive(str(archive_path), str(extract_dir))

                old_requirements_path = PROJECT_ROOT / "requirements.txt"
                old_requirements = (
                    old_requirements_path.read_text(encoding="utf-8")
                    if old_requirements_path.exists()
                    else ""
                )
                new_requirements_path = Path(extracted_root) / "requirements.txt"
                new_requirements = (
                    new_requirements_path.read_text(encoding="utf-8")
                    if new_requirements_path.exists()
                    else old_requirements
                )

                if requirements_changed(old_requirements, new_requirements):
                    self._update_queue.put(("apply_status", "Installing dependencies..."))
                    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
                    pip_result = subprocess.run(
                        [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
                        cwd=extracted_root,
                        capture_output=True,
                        text=True,
                    )
                    if pip_result.returncode != 0:
                        self._update_queue.put((
                            "apply_error",
                            f"pip install failed — program left unchanged:\n{pip_result.stderr}",
                        ))
                        return

                self._update_queue.put(("apply_status", "Copying files..."))
                copy_updatable_files(extracted_root, str(PROJECT_ROOT))
                write_local_version(str(_VERSION_FILE_PATH), release["tag_name"])

            self._update_queue.put(("apply_done", release["tag_name"]))
        except Exception as e:
            self._update_queue.put(("apply_error", f"{type(e).__name__}: {e}"))

    def _on_apply_update_done(self, tag_name: str) -> None:
        self._update_status_var.set(f"Updated to {tag_name}. Relaunch to use it.")
        self._update_apply_button.pack_forget()
        ttk.Button(
            self._update_button_frame, text="Relaunch Now", command=self._on_relaunch_clicked
        ).pack(side="left")

    def _on_apply_update_error(self, message: str) -> None:
        self._update_status_var.set(f"Update failed: {message}")
        self._update_apply_button.state(["!disabled"])

    def _on_relaunch_clicked(self) -> None:
        venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
        subprocess.Popen([str(venv_python), "-m", "lyricvideo.gui"], cwd=str(PROJECT_ROOT))
        self.root.destroy()
```

- [ ] **Step 2: Run the existing test suite to confirm nothing broke**

Run: `cd /home/doug/LyricVideoGen && .venv/bin/python -m pytest tests/test_gui.py -v`
Expected: all PASS.

- [ ] **Step 3: Manual smoke test with a fake local release**

Since `flyguy91355/LyricVideoGen-releases` doesn't exist until Task 6,
verify the dialog and apply flow now using a hand-crafted local HTTP
override — run this from a Python shell to confirm the pieces fit together
before wiring up the real network in Task 7:

```bash
cd /home/doug/LyricVideoGen && .venv/bin/python -c "
from lyricvideo.update.apply import extract_release_archive, copy_updatable_files, requirements_changed
from lyricvideo.update.version import write_local_version, read_local_version
import tarfile, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'flyguy91355-LyricVideoGen-releases-abc123'
    (src / 'lyricvideo').mkdir(parents=True)
    (src / 'lyricvideo' / 'gui.py').write_text('# test\n')
    (src / 'requirements.txt').write_text((Path('requirements.txt')).read_text())
    archive = Path(tmp) / 'r.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(src, arcname=src.name)
    extracted = Path(tmp) / 'extracted'
    extracted.mkdir()
    root = extract_release_archive(str(archive), str(extracted))
    print('extracted ok:', root)
    target = Path(tmp) / 'target'
    target.mkdir()
    print('copied:', copy_updatable_files(root, str(target)))
    write_local_version(str(target / 'VERSION'), 'v9.9.9')
    print('version now:', read_local_version(str(target / 'VERSION')))
"
```

Expected output: `extracted ok: ...`, `copied: ['lyricvideo/gui.py',
'requirements.txt']`, `version now: v9.9.9` — confirming the same
extract/copy/write-version sequence the GUI worker calls behaves
correctly outside of Tkinter.

- [ ] **Step 4: Extend and finalize the CLAUDE.md note**

Replace the whole "## Update Available Feature (in progress)" section in
`CLAUDE.md` (built incrementally across Tasks 1-4) with this consolidated
version — drop the "(in progress)" marker:

```markdown
## Update Available Feature

See `docs/superpowers/specs/2026-09-08-update-available-design.md`.
`VERSION` at the repo root tracks the last version actually applied to
this checkout (never hand-edited, never bumped per-commit).
`lyricvideo/update/` provides version parsing/comparison
(`version.py`), a GitHub Releases API client against the public,
unlisted `flyguy91355/LyricVideoGen-releases` repo (`release_client.py`,
httpx-based), and allow-listed archive extraction/copy
(`apply.py` — allows `lyricvideo/`, `tests/`, `docs/`, `requirements.txt`,
`CLAUDE.md`, a bare top-level `*.py`/`*.sh`; denies `.env`, `songs/`,
`work/`, `.venv/`). `gui.py` checks once on launch (background thread) and
shows a clickable banner if a newer release exists; clicking it opens a
dialog with the release notes and an Apply Update button (confirms first,
then downloads/reinstalls-dependencies-if-changed/copies/writes the new
VERSION) followed by a Relaunch Now button. No severity tiering, no
periodic re-check, no manual "Check Now" button — see the spec for why.
Cut a release with `scripts/cut_release.sh <version-tag> <notes-file>`.
```

- [ ] **Step 5: Commit**

```bash
cd /home/doug/LyricVideoGen
git add lyricvideo/gui.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Wire the Apply Update dialog, apply worker, and Relaunch Now

Completes the GUI side of the Update Available feature: clicking the
banner opens a dialog with the release notes; Apply Update confirms,
then downloads/extracts/reinstalls-dependencies-if-changed/copies
files/writes VERSION in a background thread; a Relaunch Now button
spawns a fresh process and closes this one on success.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Create the releases repo and `scripts/cut_release.sh`

**Files:**
- Create: `scripts/cut_release.sh`
- Modify: `CLAUDE.md`

**Interfaces:** none — this task is release tooling, not code the program
imports.

- [ ] **Step 1: Create the releases repo, if it doesn't already exist**

```bash
gh repo view flyguy91355/LyricVideoGen-releases >/dev/null 2>&1 || \
gh repo create flyguy91355/LyricVideoGen-releases --public \
  --description "Release distribution for LyricVideoGen -- no source code, only packaged release archives + notes"
```

Confirm it exists:

```bash
gh repo view flyguy91355/LyricVideoGen-releases --json name,visibility
```

Expected: `{"name":"LyricVideoGen-releases","visibility":"PUBLIC"}`.

- [ ] **Step 2: Write `scripts/cut_release.sh`**

```bash
mkdir -p /home/doug/LyricVideoGen/scripts
```

Create `scripts/cut_release.sh`:

```bash
#!/bin/bash
# Cuts a new release on the LyricVideoGen-releases distribution repo.
# Usage: scripts/cut_release.sh <version-tag> <notes-file>
# Example: scripts/cut_release.sh v1.1.0 /tmp/release-notes.txt
#
# Adapted from AITrading's scripts/cut_release.sh: same "always push a
# fresh snapshot before tagging" fix (a stale releases repo must never be
# tagged as if it were current), no severity argument (LyricVideoGen has
# no critical/routine distinction -- see
# docs/superpowers/specs/2026-09-08-update-available-design.md), and no
# config-file lookup for the repo name (hardcoded here and in
# lyricvideo/update/release_client.py's RELEASES_REPO constant).

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <version-tag> <notes-file>"
    exit 1
fi

VERSION_TAG="$1"
NOTES_FILE="$2"
RELEASES_REPO="flyguy91355/LyricVideoGen-releases"

if [ ! -f "$NOTES_FILE" ]; then
    echo "Notes file not found: $NOTES_FILE"
    exit 1
fi

echo "Syncing a fresh code snapshot to $RELEASES_REPO before tagging..."
CLONE_DIR=$(mktemp -d)
trap 'rm -rf "$CLONE_DIR"' EXIT

git clone --quiet "https://github.com/${RELEASES_REPO}.git" "$CLONE_DIR"
find "$CLONE_DIR" -mindepth 1 -maxdepth 1 -not -name '.git' -exec rm -rf {} +

FILELIST=$(mktemp)
git ls-files -- lyricvideo/ tests/ docs/ requirements.txt CLAUDE.md run_lyricvideogen.sh > "$FILELIST"
while IFS= read -r f; do
    mkdir -p "$CLONE_DIR/$(dirname "$f")"
    cp "$f" "$CLONE_DIR/$f"
done < "$FILELIST"
rm -f "$FILELIST"

pushd "$CLONE_DIR" > /dev/null
git add -A
if git diff --cached --quiet; then
    echo "No code changes since the last release sync -- releases repo already current."
else
    git -c user.email="flyguy91355@gmail.com" -c user.name="flyguy91355" \
        commit --quiet -m "Sync $(date -u +%Y-%m-%d) for $VERSION_TAG"
    git push --quiet origin main
    echo "Pushed fresh code snapshot."
fi
popd > /dev/null

gh release create "$VERSION_TAG" \
    --repo "$RELEASES_REPO" \
    --title "$VERSION_TAG" \
    --notes-file "$NOTES_FILE"

echo "Released $VERSION_TAG to $RELEASES_REPO"
```

Make it executable:

```bash
chmod +x /home/doug/LyricVideoGen/scripts/cut_release.sh
```

- [ ] **Step 3: Extend the CLAUDE.md note**

The consolidated section written in Task 5 already mentions
`cut_release.sh`; no further edit is needed here beyond staging the file.

- [ ] **Step 4: Commit**

```bash
cd /home/doug/LyricVideoGen
git add scripts/cut_release.sh CLAUDE.md
git commit -m "$(cat <<'EOF'
Add scripts/cut_release.sh

Cuts a release on flyguy91355/LyricVideoGen-releases (created this
session, public/unlisted): syncs a fresh snapshot of the allow-listed
paths, then tags a GitHub release with the given notes file as the
body. Adapted from AITrading's cut_release.sh minus severity tiering
and the config-file repo lookup.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Cut the first real release and verify end-to-end

**Files:** none created or modified by this task's own hands — this is a
verification pass.

- [ ] **Step 1: Write release notes for the first release**

```bash
cat > /tmp/lyricvideogen-v1.0.1-notes.txt <<'EOF'
First release cut through the new Update Available feature. If you're
seeing this in the app, the update check, download, and apply pipeline
all worked end to end.
EOF
```

- [ ] **Step 2: Cut the release**

```bash
cd /home/doug/LyricVideoGen && bash scripts/cut_release.sh v1.0.1 /tmp/lyricvideogen-v1.0.1-notes.txt
```

Expected: the script prints `Pushed fresh code snapshot.` (or the
"already current" line if nothing changed since Task 6) followed by
`Released v1.0.1 to flyguy91355/LyricVideoGen-releases`.

- [ ] **Step 3: Confirm `check_for_update` sees it over the real network**

```bash
cd /home/doug/LyricVideoGen && .venv/bin/python -c "
from lyricvideo.update.release_client import check_for_update
result = check_for_update('v1.0.0')
print(result)
"
```

Expected: a dict with `'tag_name': 'v1.0.1'` and the notes text from Step
1 — a real integration check against the actual GitHub API, not a unit
test with an injected fake.

- [ ] **Step 4: Ask the owner to verify the GUI visually**

This step needs a human at the keyboard — a Tkinter window can't be
screenshotted or driven headlessly the way this project verifies its web
UI. Ask the owner to:

1. Close LyricVideoGen if it's currently open.
2. Relaunch it via the `~/Desktop/lyricvideogen.desktop` icon (or
   `./run_lyricvideogen.sh`).
3. Confirm the window title reads `LyricVideoGen v1.0.0` and, within a
   few seconds, a blue "Update available: v1.0.1 — click for details"
   banner appears above the form.
4. Click the banner, confirm the dialog shows the v1.0.1 release notes
   text from Step 1.
5. Click Apply Update, confirm the download, then click "Relaunch Now"
   when it appears.
6. Confirm the relaunched window's title now reads `LyricVideoGen v1.0.1`
   and the banner is gone.

Do not mark this task complete until the owner confirms all six points.

- [ ] **Step 5: Confirm the VERSION file was actually updated**

```bash
cat /home/doug/LyricVideoGen/VERSION
```

Expected: `v1.0.1`.

---

### Task 8: Finalize documentation

**Files:**
- Modify: `CLAUDE.md` (no content change expected — see Step 1)
- Modify: `docs/CLAUDE_HISTORY.md`

- [ ] **Step 1: Confirm CLAUDE.md is already accurate**

The "## Update Available Feature" section was already finalized in Task
5, Step 4, and nothing about the feature's shape changed in Tasks 6-7. Read
`CLAUDE.md` and confirm that section still accurately describes the
shipped feature (in particular that it mentions `scripts/cut_release.sh`).
No edit should be needed; if reality drifted from that description during
implementation, fix it now.

- [ ] **Step 2: Add the dated CLAUDE_HISTORY.md entry**

Append to `docs/CLAUDE_HISTORY.md` (after the existing 2026-09-08 entry):

```markdown
## 2026-09-08 — Update Available feature shipped

Modeled on AITrading's Update Available feature (owner: "make the program
see that theres an update and version update like aitrader"), adapted for
a local desktop program rather than an always-on server. Full design in
`docs/superpowers/specs/2026-09-08-update-available-design.md`.

Shipped: a `VERSION` file (bumped only by a successful Apply Update, never
per-commit); a new public, unlisted `flyguy91355/LyricVideoGen-releases`
repo; `lyricvideo/update/` (`version.py`, `release_client.py` — httpx-based
since `requests` isn't a project dependency, `apply.py` — allow-list
`lyricvideo/`/`tests/`/`docs/`/`requirements.txt`/`CLAUDE.md`/top-level
`*.py`/`*.sh`, deny-list `.env`/`songs/`/`work/`/`.venv/`); a launch-time-
only update check in `gui.py` with a clickable banner, an Apply Update
dialog (download/reinstall-dependencies-if-changed/copy/write-VERSION),
and a Relaunch Now button that spawns a fresh process; and
`scripts/cut_release.sh`.

Deliberately dropped from AITrading's version, per owner decision during
brainstorming: severity (critical/routine) tiering, periodic background
re-checking, a manual "Check Now" button, and a version-history panel —
none of it earns its cost for a program the owner is the only person ever
applying an update to.

One real gotcha hit during implementation: `httpx.get()` does not follow
redirects by default (unlike `requests`), and the release archive download
URL 302s to `codeload.github.com` — the apply worker passes
`follow_redirects=True` explicitly, or the "download" silently succeeds
with an HTML redirect page instead of the tarball.

Verified end-to-end: cut v1.0.1 as the first real release, confirmed
`check_for_update()` sees it over the real GitHub API, and the owner
confirmed the banner/dialog/apply/relaunch flow live in the running GUI.
```

- [ ] **Step 3: Commit**

```bash
cd /home/doug/LyricVideoGen
git add CLAUDE.md docs/CLAUDE_HISTORY.md
git commit -m "$(cat <<'EOF'
Document the shipped Update Available feature in CLAUDE_HISTORY

Dated write-up of the feature built across the preceding commits,
including the httpx redirect gotcha and what was deliberately left
out relative to AITrading's version.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
