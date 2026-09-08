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
