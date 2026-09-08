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
