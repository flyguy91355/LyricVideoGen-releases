"""The one-line "song key" texts for YouTube descriptions (owner, 2026-09-26): a short correction for a video whose
burned-in Key badge is wrong ("📌 Song key: C minor (not D minor as shown in the video)" -- no apology), and the plain
"🎸 Song key: D major" line for a normal description. Pure text helpers; nothing here talks to YouTube."""

from __future__ import annotations

KEY_NOTE_MARKER = "📌"  # every key note starts with this (the short "Song key" one, and the earlier long
                        # "📌 Correction: ..." notes, which the short one replaces)
# A paragraph starting with one of these is a key note WHEREVER it sits -- e.g. pushed below the support template's tip
# line by an update_support_description run (issue #7 review, F054). These are the only two formats ever written; any
# OTHER 📌 paragraph (say a support template whose top line starts with 📌) is ordinary text and is never lifted out
# or replaced -- treating every leading 📌 paragraph as a note would delete that line, or duplicate it on a re-render.
KEY_NOTE_PREFIXES = (f"{KEY_NOTE_MARKER} Song key:", f"{KEY_NOTE_MARKER} Correction")


def _is_key_note(paragraph: str) -> bool:
    return paragraph.strip().startswith(KEY_NOTE_PREFIXES)


def split_key_note(description: str) -> tuple[str, str]:
    """(the description's key note, everything else). The note is the top-most key-note paragraph (a "📌 Song key:" /
    "📌 Correction" paragraph, wherever it sits); EVERY key-note paragraph is taken out of the rest, so a stale or
    duplicated note can never linger in the body. ("", description) when there is none."""
    paragraphs = (description or "").strip().split("\n\n")
    note, rest = "", []
    for paragraph in paragraphs:
        if _is_key_note(paragraph):
            note = note or paragraph.strip()
            continue
        rest.append(paragraph)
    return note, "\n\n".join(rest).strip()


def build_key_note(true_key: str, shown_key: str) -> str:
    true_key, shown_key = (true_key or "").strip(), (shown_key or "").strip()
    if not true_key or not shown_key:
        raise ValueError("both the true key and the key shown in the video are required")
    if true_key == shown_key:
        raise ValueError(f"{true_key!r} is already what the video shows -- nothing to correct")
    return f"{KEY_NOTE_MARKER} Song key: {true_key} (not {shown_key} as shown in the video)"


def apply_key_note(description: str, true_key: str, shown_key: str) -> str:
    """The description with the key note as its first paragraph: every existing key note (short or the older long
    "Correction"; leading, or pushed further down by a support-template re-render) is removed first, anything else
    stays exactly as it was. Safe to apply twice, and never leaves two notes (issue #7 review, F054)."""
    note = build_key_note(true_key, shown_key)
    _old_note, text = split_key_note(description)
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
