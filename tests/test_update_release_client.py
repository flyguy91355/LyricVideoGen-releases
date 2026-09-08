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
