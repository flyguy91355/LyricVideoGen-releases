import pytest

from lyricvideo.key_note import KEY_NOTE_MARKER, apply_key_note, build_key_note, song_key_line

OLD_LONG_NOTE = (
    "📌 Correction: the Key shown in the top-right corner of this video (A major) is the key of the "
    "chord shapes. The song's original key is Bb major, which is what you'll be playing in with your "
    "capo on fret 1. Sorry for the mix-up!"
)
BODY = (
    "☕ Tips are never expected, but always appreciated: https://ko-fi.com/playalongvideos\n"
    '▼ Click "more" for the song info ▼\n\n'
    "EASY CHORDS version -- Capo 1, play it in A shapes (original key: Bb major).\n\n"
    "A powerful anthem of hope."
)


def test_build_key_note_is_one_plain_line():
    assert build_key_note("Bb major", "A major") == "📌 Song key: Bb major (not A major as shown in the video)"


def test_build_key_note_rejects_a_key_that_needs_no_correction():
    with pytest.raises(ValueError):
        build_key_note("A major", "A major")


def test_build_key_note_rejects_blank_keys():
    with pytest.raises(ValueError):
        build_key_note("", "A major")
    with pytest.raises(ValueError):
        build_key_note("A major", "  ")


def test_apply_puts_the_note_first_when_the_description_has_none():
    out = apply_key_note("Some song text.", "C minor", "D minor")
    assert out == "📌 Song key: C minor (not D minor as shown in the video)\n\nSome song text."


def test_apply_replaces_the_old_long_correction_and_keeps_everything_else():
    out = apply_key_note(OLD_LONG_NOTE + "\n\n" + BODY, "Bb major", "A major")
    assert out == "📌 Song key: Bb major (not A major as shown in the video)\n\n" + BODY
    assert "Sorry" not in out


def test_apply_is_idempotent():
    once = apply_key_note("Some song text.", "C minor", "D minor")
    assert apply_key_note(once, "C minor", "D minor") == once


def test_apply_with_a_new_key_replaces_the_earlier_short_note():
    once = apply_key_note("Some song text.", "C minor", "D minor")
    twice = apply_key_note(once, "C major", "D minor")
    assert twice == "📌 Song key: C major (not D minor as shown in the video)\n\nSome song text."


def test_apply_only_touches_a_leading_note_paragraph():
    text = "Song text.\n\n" + KEY_NOTE_MARKER + " mentioned later in the body."
    out = apply_key_note(text, "C minor", "D minor")
    assert out.startswith("📌 Song key: C minor")
    assert out.endswith(KEY_NOTE_MARKER + " mentioned later in the body.")


def test_song_key_line_for_normal_descriptions():
    assert song_key_line("D major") == "🎸 Song key: D major"


def test_song_key_line_is_blank_without_a_key():
    assert song_key_line("") == ""
    assert song_key_line(None) == ""


def test_apply_key_fixes_rewrites_a_wrong_line_and_adds_the_note():
    from lyricvideo.key_note import apply_key_fixes
    desc = "EASY CHORDS version -- Capo 3, play it in Am shapes (original key: C minor).\n\nA great song."
    out = apply_key_fixes(
        desc, "C major", "C minor",
        [("play it in Am shapes (original key: C minor)", "play it in A shapes (original key: C major)")],
    )
    assert out == (
        "📌 Song key: C major (not C minor as shown in the video)\n\n"
        "EASY CHORDS version -- Capo 3, play it in A shapes (original key: C major).\n\nA great song."
    )


def test_apply_key_fixes_is_safe_to_run_twice_and_skips_a_replacement_already_made():
    from lyricvideo.key_note import apply_key_fixes
    desc = "EASY CHORDS version -- Capo 3, play it in Am shapes (original key: C minor).\n\nA great song."
    fixes = [("play it in Am shapes (original key: C minor)", "play it in A shapes (original key: C major)")]
    once = apply_key_fixes(desc, "C major", "C minor", fixes)
    assert apply_key_fixes(once, "C major", "C minor", fixes) == once


def test_apply_key_fixes_without_replacements_is_just_the_note():
    from lyricvideo.key_note import apply_key_fixes
    assert apply_key_fixes("Song text.", "C minor", "D minor", []) == apply_key_note("Song text.", "C minor", "D minor")


# --- a note pushed below the support template's tip line is still found (issue #7 review, F054) -------------------------

_TIP = '☕ Tips are welcome: https://ko-fi.com/x\n▼ Click "more" for the song info ▼'


def test_a_note_pushed_below_the_tip_is_replaced_not_duplicated():
    pushed = f"{_TIP}\n\n📌 Song key: C minor (not D minor as shown in the video)\n\nBody text."

    out = apply_key_note(pushed, "C major", "D minor")

    assert out.count("📌") == 1
    assert out == f"📌 Song key: C major (not D minor as shown in the video)\n\n{_TIP}\n\nBody text."


def test_a_stale_second_note_anywhere_is_removed():
    doubled = (
        "📌 Song key: C major (not D minor as shown in the video)\n\n"
        f"{_TIP}\n\n📌 Correction: an older, longer note.\n\nBody text."
    )

    out = apply_key_note(doubled, "C major", "D minor")

    assert out.count("📌") == 1 and out.endswith("Body text.") and _TIP in out


def test_split_key_note_separates_the_note_from_the_rest():
    from lyricvideo.key_note import split_key_note

    note, rest = split_key_note(f"{_TIP}\n\n📌 Song key: C minor (not D minor as shown in the video)\n\nBody.")

    assert note == "📌 Song key: C minor (not D minor as shown in the video)"
    assert rest == f"{_TIP}\n\nBody."
    assert split_key_note("Just a song.") == ("", "Just a song.")


# --- the OLD unconditional "🎸 Song key: X" line is stripped, not repositioned (owner, 2026-09-29) ---------------------

def test_strip_song_key_line_removes_a_leading_plain_key_line():
    from lyricvideo.key_note import strip_song_key_line

    assert strip_song_key_line("🎸 Song key: B minor\n\nA great song.") == "A great song."


def test_strip_song_key_line_leaves_a_description_with_none_unchanged():
    from lyricvideo.key_note import strip_song_key_line

    assert strip_song_key_line("A great song.") == "A great song."


def test_strip_song_key_line_only_checks_the_leading_paragraph():
    from lyricvideo.key_note import strip_song_key_line

    text = "A great song.\n\n🎸 Song key: mentioned later in the body."
    assert strip_song_key_line(text) == text


def test_strip_song_key_line_tolerates_a_blank_description():
    from lyricvideo.key_note import strip_song_key_line

    assert strip_song_key_line("") == ""
    assert strip_song_key_line(None) == ""
