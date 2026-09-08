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
