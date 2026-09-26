"""The one-line "song key" texts for YouTube descriptions (owner, 2026-09-26): a short correction for a video whose
burned-in Key badge is wrong ("📌 Song key: C minor (not D minor as shown in the video)" -- no apology), and the plain
"🎸 Song key: D major" line for a normal description. Pure text helpers; nothing here talks to YouTube."""

from __future__ import annotations

KEY_NOTE_MARKER = "📌"  # a description's FIRST paragraph starting with this is a key note (the earlier long
                        # "📌 Correction: ..." notes count too, so the short one replaces them)


def build_key_note(true_key: str, shown_key: str) -> str:
    true_key, shown_key = (true_key or "").strip(), (shown_key or "").strip()
    if not true_key or not shown_key:
        raise ValueError("both the true key and the key shown in the video are required")
    if true_key == shown_key:
        raise ValueError(f"{true_key!r} is already what the video shows -- nothing to correct")
    return f"{KEY_NOTE_MARKER} Song key: {true_key} (not {shown_key} as shown in the video)"


def apply_key_note(description: str, true_key: str, shown_key: str) -> str:
    """The description with the key note as its first paragraph: an existing leading key note (short or the older
    long "Correction") is replaced, anything else stays exactly as it was. Safe to apply twice."""
    note = build_key_note(true_key, shown_key)
    text = (description or "").strip()
    first, sep, rest = text.partition("\n\n")
    if first.startswith(KEY_NOTE_MARKER):
        text = rest.strip() if sep else ""
    return f"{note}\n\n{text}" if text else note


def song_key_line(key: str | None) -> str:
    """The plain key line for a normal description ("" when the key is unknown -- never a guess)."""
    key = (key or "").strip()
    return f"🎸 Song key: {key}" if key else ""


def apply_key_fixes(description: str, true_key: str, shown_key: str, replacements: list[tuple[str, str]]) -> str:
    """apply_key_note plus exact text swaps for wording that itself states the wrong key (an EASY CHORD description's
    "(original key: Eb minor)" line). A swap whose old text is not there any more (already made) is skipped."""
    text = description or ""
    for old, new in replacements:
        if old in text:
            text = text.replace(old, new)
    return apply_key_note(text, true_key, shown_key)
