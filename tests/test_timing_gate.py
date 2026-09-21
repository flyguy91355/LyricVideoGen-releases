"""timing_gate: is each lyric line highlighted within half a second of where it is sung? A song passes when at least 90%
of its judged lines are. Owner's standard, 2026-09-20, after 'Like a Prayer' (second half 20-85 s out of sync) was cleared
by a check that let a repeated chorus excuse a misplaced line. Song text is invented."""

from dataclasses import replace

from lyricvideo.anchors import HeardWord
from lyricvideo.models import LyricLine, Song, Word, load_song, save_song
import pytest

from lyricvideo.owner_verified import mark_verified, upload_label, verification
from lyricvideo.pipeline import list_flagged_songs, list_pending_uploads, list_uploadable_songs
from lyricvideo.timing_gate import (
    check_saved_song, check_sync, hidden_note, hold_if_timing_fails, pick_by_sync, scan_songs, settle_alignment,
    use_pass_share_from,
)


@pytest.fixture(autouse=True)
def _default_bar():
    use_pass_share_from(None)
    yield
    use_pass_share_from(None)

LINES = [
    "the river runs beside the old stone mill",
    "and morning fog lies heavy on the hill",
    "a lantern swings above the wooden door",
    "i wait for you like i have waited before",
    "your letters crossed the sea in autumn rain",
    "each candle burned until the break of dawn",
    "we carved our names into the cedar gate",
    "no train will carry us away too late",
    "the harvest moon hangs over silver fields",
    "and every bell rings out what the night reveals",
]
WORDS = [line.split() for line in LINES]
FLAT = [w for words in WORDS for w in words]
LINE_START = [10.0 + 8.0 * k for k in range(len(LINES))]        # eight seconds apart: no line hears its neighbour
TRUE = [LINE_START[k] + 0.4 * i for k, words in enumerate(WORDS) for i in range(len(words))]
HEARD = [HeardWord(w, t, t + 0.35) for w, t in zip(FLAT, TRUE)]


def placed(shift_by_line=None, shift_all=0.0):
    """Per-word (start, end) placements: where the video puts each word, given a per-line shift in seconds."""
    shift_by_line = shift_by_line or {}
    out = []
    for k, words in enumerate(WORDS):
        for i in range(len(words)):
            s = TRUE[len(out)] + shift_all + shift_by_line.get(k, 0.0)
            out.append((s, s + 0.3))
    return out


def test_a_song_placed_on_the_singing_passes():
    report = check_sync(WORDS, placed(), HEARD)

    assert report.passes and report.share == 1.0 and report.out_of_sync_lines == ()


def test_one_line_in_ten_a_second_off_is_exactly_ninety_percent_and_passes():
    report = check_sync(WORDS, placed({4: 1.0}), HEARD)

    assert report.share == 0.9 and report.passes and report.out_of_sync_lines == (5,)


def test_two_lines_in_ten_off_fails_and_names_them_and_the_percentage():
    report = check_sync(WORDS, placed({1: 1.0, 6: -1.0}), HEARD)

    assert not report.passes and report.share == 0.8 and report.out_of_sync_lines == (2, 7)
    assert "80%" in report.concern and "2, 7" in report.concern


def test_a_line_forty_milliseconds_inside_half_a_second_is_in_sync_and_sixty_over_is_not():
    assert check_sync(WORDS, placed(shift_all=0.46), HEARD).share == 1.0
    assert check_sync(WORDS, placed(shift_all=0.56), HEARD).share == 0.0


def test_a_line_placed_where_other_words_are_sung_is_out_of_sync_even_if_its_words_are_sung_elsewhere():
    # Line 1 and line 3 are the same chorus line, sung at 10 s and at 50 s. The video puts line 3 at 70 s, on top of line 4.
    # A check that accepts "these words are sung somewhere in the song" would excuse it; the gate must not.
    lines = [["hold", "me", "close", "tonight"], ["sing", "along", "with", "me", "now"], ["hold", "me", "close", "tonight"],
             ["rivers", "carry", "every", "sorrow", "away"]] + WORDS[:6]
    sung = {0: 10.0, 1: 30.0, 2: 50.0, 3: 70.0}
    starts = {0: 10.0, 1: 30.0, 2: 70.0, 3: 70.0}            # line 2 (a repeat of line 0) dropped onto line 3's slot
    for k in range(4, 10):
        sung[k] = starts[k] = 90.0 + 8.0 * (k - 4)
    heard, times = [], []
    for k, words in enumerate(lines):
        for i, w in enumerate(words):
            heard.append(HeardWord(w, sung[k] + 0.4 * i, sung[k] + 0.4 * i + 0.35))
            times.append((starts[k] + 0.4 * i, starts[k] + 0.4 * i + 0.3))
    report = check_sync(lines, times, heard)

    assert report.out_of_sync_lines == (3,)


def test_steady_drift_across_the_second_half_fails_like_the_real_song_that_slipped_through():
    drift = {k: 3.0 * (k - 4) for k in range(5, 10)}          # each late line 3, 6, 9... seconds early
    report = check_sync(WORDS, placed({k: -v for k, v in drift.items()}), HEARD)

    assert not report.passes and report.out_of_sync_lines == (6, 7, 8, 9, 10)


def test_a_line_the_recognizer_never_heard_is_not_counted_against_the_song():
    heard = [h for h, k in zip(HEARD, [k for k, words in enumerate(WORDS) for _ in words]) if k != 3]
    report = check_sync(WORDS, placed(), heard)

    assert report.passes and report.unjudged_lines == 1 and report.judged_lines == 9


def test_a_song_the_recognizer_heard_nothing_of_cannot_be_checked_and_is_set_aside():
    report = check_sync(WORDS, placed(), [])

    assert report.share is None and not report.passes and "could not be checked" in report.concern


def test_the_alignment_with_the_most_lines_in_sync_is_chosen_and_a_tie_keeps_the_preferred_one():
    drifted = placed({k: -3.0 * (k - 3) for k in range(4, 10)})
    exact = placed()

    name, times, report = pick_by_sync({"whole-song": drifted, "anchored": exact}, WORDS, HEARD, preferred="whole-song")
    assert name == "anchored" and times == exact and report.passes

    name, _, _ = pick_by_sync({"whole-song": exact, "anchored": list(exact)}, WORDS, HEARD, preferred="anchored")
    assert name == "anchored"


def _save(song_dir, times, concern="", heard=HEARD):
    song_dir.mkdir(parents=True)
    lines, n = [], 0
    for words in WORDS:
        lines.append(LyricLine(words=[Word(w, times[n + i][0], times[n + i][1]) for i, w in enumerate(words)]))
        n += len(words)
    save_song(Song(title="T", audio_path="a.mp3", lines=lines, lyrics_accuracy_concern=concern), song_dir / "lyrics_timed.json")
    import json
    (song_dir / "transcript.json").write_text(json.dumps({"words": [
        {"word": h.word, "start": h.start, "end": h.end} for h in heard]}), encoding="utf-8")


def test_a_saved_song_is_checked_from_its_own_files(tmp_path):
    _save(tmp_path / "good", placed())
    _save(tmp_path / "bad", placed({1: 1.0, 6: -1.0}))

    assert check_saved_song(tmp_path / "good").passes
    assert check_saved_song(tmp_path / "bad").out_of_sync_lines == (2, 7)


def test_a_failing_saved_song_gets_the_reason_written_in_and_a_passing_one_is_left_alone(tmp_path):
    _save(tmp_path / "good", placed())
    _save(tmp_path / "bad", placed({1: 1.0, 6: -1.0}))
    before = (tmp_path / "good" / "lyrics_timed.json").read_text(encoding="utf-8")

    assert hold_if_timing_fails(tmp_path / "good") == ""
    assert (tmp_path / "good" / "lyrics_timed.json").read_text(encoding="utf-8") == before
    reason = hold_if_timing_fails(tmp_path / "bad")
    assert "80%" in reason and load_song(tmp_path / "bad" / "lyrics_timed.json").lyrics_accuracy_concern == reason


def test_an_existing_concern_is_never_overwritten_and_a_song_with_no_transcript_is_not_held_here(tmp_path):
    _save(tmp_path / "flagged", placed({1: 1.0, 6: -1.0}), concern="wrong lyrics")
    _save(tmp_path / "untested", placed({1: 1.0, 6: -1.0}), heard=[])

    assert hold_if_timing_fails(tmp_path / "flagged") == ""
    assert load_song(tmp_path / "flagged" / "lyrics_timed.json").lyrics_accuracy_concern == "wrong lyrics"
    assert hold_if_timing_fails(tmp_path / "untested") == ""      # cannot judge: only a fresh render holds on that


# --- wiring: what the app does with the verdict ------------------------------------------------------------------

def _rendered(work_root, name, times):
    _save(work_root / name, times)
    (work_root / name / "t.mp4").write_bytes(b"video")           # the app looks for <slugified title>.mp4


def test_a_rendered_song_that_fails_the_check_is_not_offered_for_upload(tmp_path):
    _rendered(tmp_path, "good", placed())
    _rendered(tmp_path, "bad", placed({1: 1.0, 6: -1.0}))

    assert list_pending_uploads(tmp_path) == ["good"]


def test_a_rendered_song_that_fails_the_check_appears_for_review_with_the_reason(tmp_path):
    _rendered(tmp_path, "good", placed())
    _rendered(tmp_path, "bad", placed({1: 1.0, 6: -1.0}))

    assert list_flagged_songs(tmp_path) == ["bad"]
    assert "80%" in load_song(tmp_path / "bad" / "lyrics_timed.json").lyrics_accuracy_concern


def test_a_fresh_render_takes_the_alignment_that_is_in_sync_and_reports_no_concern():
    drifted = placed({k: -3.0 * (k - 3) for k in range(4, 10)})

    settled = settle_alignment({"whole-song": drifted, "anchored": placed()}, WORDS, HEARD, preferred="whole-song")

    assert settled.method == "anchored" and settled.times == placed() and settled.concern == ""


def test_a_fresh_render_that_no_alignment_gets_in_sync_is_set_aside_with_the_reason():
    drifted = placed({k: -3.0 * (k - 3) for k in range(4, 10)})

    settled = settle_alignment({"whole-song": drifted}, WORDS, HEARD, preferred="whole-song")

    assert settled.concern.startswith("SET ASIDE FOR REVIEW") and "not precise enough" in settled.concern


def test_an_earlier_concern_survives_when_the_sync_check_passes_but_is_replaced_when_it_fails():
    earlier = "SET ASIDE FOR REVIEW -- a line sits where nobody sings."

    assert settle_alignment({"whole-song": placed()}, WORDS, HEARD, "whole-song", earlier).concern == earlier
    failing = settle_alignment({"whole-song": placed({1: 1.0, 6: -1.0})}, WORDS, HEARD, "whole-song", earlier).concern
    assert "80%" in failing and earlier not in failing


def test_a_fresh_render_the_recognizer_heard_nothing_of_is_set_aside_as_unchecked():
    settled = settle_alignment({"whole-song": placed()}, WORDS, [], preferred="whole-song")

    assert "could not be checked" in settled.concern


# --- the render itself: pipeline._align_lyrics (only the slow speech models are faked) ------------------------------

def _twenty_lines():
    """Twenty four-word lines, eight seconds apart, each word made of letters no other line shares."""
    lines = [[f"qu{chr(97 + k)}{chr(97 + i)}x" for i in range(4)] for k in range(20)]
    truth = [(10.0 + 8.0 * k + 0.4 * i, 10.0 + 8.0 * k + 0.4 * i + 0.3) for k in range(20) for i in range(4)]
    return lines, truth


def _render_with(monkeypatch, tmp_path, whole, anchored, needed=None):
    import json
    from lyricvideo import pipeline
    lines, truth = _twenty_lines()
    (tmp_path / "transcript.json").write_text(json.dumps({"words": [
        {"word": w, "start": s, "end": s + 0.35} for w, (s, _) in zip([w for line in lines for w in line], truth)]}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "vocal_loudness", lambda path: [])
    monkeypatch.setattr(pipeline, "prepare_alignment", lambda path: object())
    monkeypatch.setattr(pipeline, "align_words", lambda *a, **k: whole)
    monkeypatch.setattr(pipeline, "align_words_anchored", lambda *a, **k: (anchored, None))
    parsed = [LyricLine(words=[Word(w) for w in line]) for line in lines]
    return pipeline._align_lyrics("vocals.wav", tmp_path, parsed, [w for line in lines for w in line], 400.0, needed=needed)


def test_a_render_with_three_lines_of_twenty_a_second_late_is_set_aside_even_though_the_old_word_check_accepts_it(monkeypatch, tmp_path):
    _, truth = _twenty_lines()
    late = [(s + 1.2, e + 1.2) if (k // 4) in (3, 9, 15) else (s, e) for k, (s, e) in enumerate(truth)]

    times, concern = _render_with(monkeypatch, tmp_path, whole=late, anchored=late)

    assert "85%" in concern and "not precise enough" in concern
    assert times == late                                            # still rendered, just not trusted


def test_a_render_takes_whichever_alignment_is_in_sync_and_is_not_set_aside(monkeypatch, tmp_path):
    _, truth = _twenty_lines()
    drifted = [(s + 3.0 * max(0, k // 4 - 9), e + 3.0 * max(0, k // 4 - 9)) for k, (s, e) in enumerate(truth)]

    times, concern = _render_with(monkeypatch, tmp_path, whole=drifted, anchored=truth)

    assert concern == "" and times == truth


def test_a_song_held_by_the_check_is_recorded_as_removed_from_the_cleared_list(tmp_path, monkeypatch):
    from lyricvideo import cleared_log
    monkeypatch.setattr(cleared_log, "LOG_FILE", tmp_path / "cleared.json")
    _save(tmp_path / "bad", placed({1: 1.0, 6: -1.0}))
    cleared_log.record_cleared("bad", "passed the older checks")

    hold_if_timing_fails(tmp_path / "bad")

    assert cleared_log.cleared_songs() == []
    assert "80%" in cleared_log.history()[-1]["note"] and cleared_log.history()[-1]["status"] == "removed"


def test_a_scan_reports_every_song_and_holds_only_the_ones_not_yet_on_youtube_when_asked(tmp_path):
    _rendered(tmp_path, "good", placed())
    _rendered(tmp_path, "bad-pending", placed({1: 1.0, 6: -1.0}))
    _rendered(tmp_path, "bad-live", placed({1: 1.0, 6: -1.0}))
    (tmp_path / "bad-live" / "youtube_state.json").write_text("{}", encoding="utf-8")

    rows = {r["slug"]: r for r in scan_songs(tmp_path)}
    assert {s: (r["passes"], r["on_youtube"]) for s, r in rows.items()} == {
        "good": (True, False), "bad-pending": (False, False), "bad-live": (False, True)}
    assert load_song(tmp_path / "bad-pending" / "lyrics_timed.json").lyrics_accuracy_concern == ""      # report only

    scan_songs(tmp_path, hold=True)
    assert "80%" in load_song(tmp_path / "bad-pending" / "lyrics_timed.json").lyrics_accuracy_concern
    assert load_song(tmp_path / "bad-live" / "lyrics_timed.json").lyrics_accuracy_concern == ""         # a live video is your call


# --- the pass mark is the owner's setting (Settings.timing_pass_percent), not a constant -----------------------------

def test_a_song_at_ninety_percent_fails_when_the_bar_is_ninety_five_and_the_reason_says_so():
    report = check_sync(WORDS, placed({4: 1.0}), HEARD, needed=0.95)

    assert report.share == 0.9 and not report.passes and "95% are needed" in report.concern


def test_lowering_the_bar_lets_an_eighty_percent_song_pass_exactly_at_the_bar_and_not_above_it():
    lines_off = placed({1: 1.0, 6: -1.0})

    assert check_sync(WORDS, lines_off, HEARD, needed=0.80).passes
    assert not check_sync(WORDS, lines_off, HEARD, needed=0.85).passes


def test_the_default_bar_follows_whatever_the_app_registered_as_its_source():
    use_pass_share_from(lambda: 0.95)

    assert not check_sync(WORDS, placed({4: 1.0}), HEARD).passes            # 90% now falls short with no explicit bar
    use_pass_share_from(lambda: 0.85)
    assert check_sync(WORDS, placed({4: 1.0}), HEARD).passes


def test_a_render_uses_the_bar_it_is_given(monkeypatch, tmp_path):
    _, truth = _twenty_lines()
    late = [(s + 1.2, e + 1.2) if (k // 4) in (3, 9, 15) else (s, e) for k, (s, e) in enumerate(truth)]      # 85% in sync

    _, concern = _render_with(monkeypatch, tmp_path, whole=late, anchored=late, needed=0.85)

    assert concern == ""


def test_lowering_the_bar_releases_a_song_the_check_held_and_the_cleared_list_takes_it_back(tmp_path):
    from lyricvideo import cleared_log
    _save(tmp_path / "s", placed({1: 1.0, 6: -1.0}))

    assert hold_if_timing_fails(tmp_path / "s", needed=0.90) != ""
    assert hold_if_timing_fails(tmp_path / "s", needed=0.80) == ""
    assert load_song(tmp_path / "s" / "lyrics_timed.json").lyrics_accuracy_concern == ""
    assert [e["slug"] for e in cleared_log.cleared_songs()] == ["s"]


def test_raising_the_bar_holds_a_song_that_used_to_pass_and_names_the_new_bar(tmp_path):
    _save(tmp_path / "s", placed({4: 1.0}))

    assert hold_if_timing_fails(tmp_path / "s", needed=0.90) == ""
    reason = hold_if_timing_fails(tmp_path / "s", needed=0.95)
    assert "95% are needed" in reason and load_song(tmp_path / "s" / "lyrics_timed.json").lyrics_accuracy_concern == reason


def test_a_concern_from_another_check_is_never_released_or_rewritten_by_the_timing_check(tmp_path):
    mixed = ("Only 60% of these lyrics match what is sung. SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: "
             "only 80% of the lines start within half a second of where they are sung (90% are needed); lines 2, 7 are off.")
    _save(tmp_path / "text-only", placed(), concern="wrong lyrics")
    _save(tmp_path / "text-and-timing", placed(), concern=mixed)

    assert hold_if_timing_fails(tmp_path / "text-only") == "" and hold_if_timing_fails(tmp_path / "text-and-timing") == ""
    assert load_song(tmp_path / "text-only" / "lyrics_timed.json").lyrics_accuracy_concern == "wrong lyrics"
    assert load_song(tmp_path / "text-and-timing" / "lyrics_timed.json").lyrics_accuracy_concern == mixed


def test_the_pending_list_follows_the_bar_as_it_moves(tmp_path):
    _rendered(tmp_path, "s", placed({1: 1.0, 6: -1.0}))                       # 80% in sync

    use_pass_share_from(lambda: 0.90)
    assert list_pending_uploads(tmp_path) == []
    use_pass_share_from(lambda: 0.80)
    assert list_pending_uploads(tmp_path) == ["s"]


# --- the Upload to YouTube list offers only songs that positively pass ------------------------------------------------

def test_the_upload_list_offers_only_songs_that_positively_pass_the_bar(tmp_path):
    _rendered(tmp_path, "good", placed())
    _rendered(tmp_path, "bad", placed({1: 1.0, 6: -1.0}))
    _rendered(tmp_path, "unchecked", placed())
    (tmp_path / "unchecked" / "transcript.json").write_text('{"words": []}', encoding="utf-8")      # nothing to check it against
    _rendered(tmp_path, "lyrics-flagged", placed())
    save_song(replace(load_song(tmp_path / "lyrics-flagged" / "lyrics_timed.json"), lyrics_accuracy_concern="wrong lyrics"),
              tmp_path / "lyrics-flagged" / "lyrics_timed.json")

    assert list_uploadable_songs(tmp_path) == ["good"]


def test_the_upload_list_still_offers_a_passing_song_that_is_already_on_youtube_and_never_writes_to_a_failing_one(tmp_path):
    _rendered(tmp_path, "live-good", placed())
    _rendered(tmp_path, "live-bad", placed({1: 1.0, 6: -1.0}))
    for name in ("live-good", "live-bad"):
        (tmp_path / name / "youtube_state.json").write_text("{}", encoding="utf-8")
    before = (tmp_path / "live-bad" / "lyrics_timed.json").read_text(encoding="utf-8")

    assert list_uploadable_songs(tmp_path) == ["live-good"]
    assert (tmp_path / "live-bad" / "lyrics_timed.json").read_text(encoding="utf-8") == before      # a live video is only reported


def test_the_upload_list_follows_the_bar(tmp_path):
    _rendered(tmp_path, "ninety", placed({4: 1.0}))

    use_pass_share_from(lambda: 0.90)
    assert list_uploadable_songs(tmp_path) == ["ninety"]
    use_pass_share_from(lambda: 0.95)
    assert list_uploadable_songs(tmp_path) == []


def test_the_note_under_the_upload_list_says_how_many_videos_are_hidden_and_below_what():
    assert hidden_note(6, 90) == "6 videos hidden: below 90%"
    assert hidden_note(1, 95) == "1 video hidden: below 95%"
    assert hidden_note(0, 90) == ""


# --- the owner's own verdict (2026-09-20: "if i decide its a good video its a good video") -------------------------------

def test_a_song_the_owner_verified_remembers_the_automatic_score_and_an_unverified_one_reports_nothing(tmp_path):
    _save(tmp_path / "a", placed({1: 1.0, 6: -1.0}))
    _save(tmp_path / "b", placed())

    mark_verified(tmp_path / "a", automatic_share=0.83)

    assert verification(tmp_path / "a")["automatic_share"] == 0.83
    assert verification(tmp_path / "b") is None


def test_a_redo_that_changes_the_timing_voids_the_verification(tmp_path):
    _save(tmp_path / "a", placed({1: 1.0, 6: -1.0}))
    mark_verified(tmp_path / "a", automatic_share=0.8)

    _rewrite = tmp_path / "a" / "lyrics_timed.json"
    song = load_song(_rewrite)
    song.lines[0].words[0].start_time += 0.7                       # what a redo does: a new timing file
    save_song(song, _rewrite)

    assert verification(tmp_path / "a") is None


def test_a_verified_song_that_fails_the_bar_is_offered_for_upload_and_is_not_held_or_flagged(tmp_path):
    _rendered(tmp_path, "checked", placed({1: 1.0, 6: -1.0}))                  # 80% in sync: fails the 90% bar
    before = (tmp_path / "checked" / "lyrics_timed.json").read_text(encoding="utf-8")
    mark_verified(tmp_path / "checked", automatic_share=0.8)

    assert list_uploadable_songs(tmp_path) == ["checked"]
    assert list_pending_uploads(tmp_path) == ["checked"]
    assert list_flagged_songs(tmp_path) == []
    assert (tmp_path / "checked" / "lyrics_timed.json").read_text(encoding="utf-8") == before      # nothing written into it


def test_the_owners_verdict_also_overrides_a_lyric_text_concern(tmp_path):
    _rendered(tmp_path, "wording", placed())
    path = tmp_path / "wording" / "lyrics_timed.json"
    save_song(replace(load_song(path), lyrics_accuracy_concern="Only 60% of these lyrics match what is sung."), path)
    assert list_uploadable_songs(tmp_path) == []

    mark_verified(tmp_path / "wording", automatic_share=1.0)

    assert list_uploadable_songs(tmp_path) == ["wording"] and list_flagged_songs(tmp_path) == []


def test_a_verified_song_goes_back_to_needing_review_once_it_is_redone(tmp_path):
    _rendered(tmp_path, "redone", placed({1: 1.0, 6: -1.0}))
    mark_verified(tmp_path / "redone", automatic_share=0.8)
    path = tmp_path / "redone" / "lyrics_timed.json"
    song = load_song(path)
    song.lines[2].words[0].start_time += 1.5                        # the redo's new timing (still failing)
    save_song(song, path)

    assert list_uploadable_songs(tmp_path) == [] and list_flagged_songs(tmp_path) == ["redone"]


def test_marking_a_song_verified_puts_it_on_the_cleared_list_with_the_reason(tmp_path):
    from lyricvideo import cleared_log
    _save(tmp_path / "a", placed({1: 1.0, 6: -1.0}))

    mark_verified(tmp_path / "a", automatic_share=0.8)

    assert [e["slug"] for e in cleared_log.cleared_songs()] == ["a"]
    assert "verified" in cleared_log.history()[-1]["note"] and "80%" in cleared_log.history()[-1]["note"]


def test_the_upload_list_row_says_a_song_was_verified_by_the_owner_and_what_the_automatic_score_was(tmp_path):
    _save(tmp_path / "plain", placed())
    _save(tmp_path / "checked", placed({1: 1.0, 6: -1.0}))
    mark_verified(tmp_path / "checked", automatic_share=0.83)

    assert upload_label(tmp_path / "plain") == "plain"
    assert upload_label(tmp_path / "checked") == "checked  ✔ verified by you (83% automatic)"


def _gui_with_a_failing_song(tmp_path, monkeypatch, answer):
    from types import SimpleNamespace
    from lyricvideo.settings import Settings
    monkeypatch.setattr("lyricvideo.gui.PROJECT_ROOT", tmp_path)
    (tmp_path / "work").mkdir()
    _rendered(tmp_path / "work", "hard-song", placed({1: 1.0, 6: -1.0}))                   # 80% in sync
    asked, refreshed = [], []
    monkeypatch.setattr("lyricvideo.gui.messagebox.askyesno", lambda title, message, **kw: asked.append(message) or answer)
    stub = SimpleNamespace(
        settings=Settings(),
        _invalidate_upload_list=lambda: refreshed.append("upload"), _invalidate_pending_list=lambda: refreshed.append("pending"),
        _invalidate_flagged_list=lambda: refreshed.append("flagged"),
    )
    return stub, asked, refreshed


def test_marking_verified_asks_first_shows_the_automatic_score_then_records_the_owners_verdict_and_refreshes_the_lists(tmp_path, monkeypatch):
    from lyricvideo.gui import LyricVideoGUI
    stub, asked, refreshed = _gui_with_a_failing_song(tmp_path, monkeypatch, answer=True)

    LyricVideoGUI._on_mark_verified(stub, "hard-song")

    assert len(asked) == 1 and "80%" in asked[0] and "90%" in asked[0]
    assert verification(tmp_path / "work" / "hard-song")["automatic_share"] == 0.8
    assert sorted(refreshed) == ["flagged", "pending", "upload"]


def test_saying_no_to_the_verify_prompt_records_nothing(tmp_path, monkeypatch):
    from lyricvideo.gui import LyricVideoGUI
    stub, asked, refreshed = _gui_with_a_failing_song(tmp_path, monkeypatch, answer=False)

    LyricVideoGUI._on_mark_verified(stub, "hard-song")

    assert verification(tmp_path / "work" / "hard-song") is None and refreshed == []


def test_only_the_upload_list_rows_carry_the_verified_label(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lyricvideo.gui import LyricVideoGUI
    monkeypatch.setattr("lyricvideo.gui.PROJECT_ROOT", tmp_path)
    (tmp_path / "work").mkdir()
    _rendered(tmp_path / "work", "hard-song", placed({1: 1.0, 6: -1.0}))
    mark_verified(tmp_path / "work" / "hard-song", automatic_share=0.8)
    stub = SimpleNamespace()

    assert LyricVideoGUI._song_label(stub, "upload", "hard-song") == "hard-song  ✔ verified by you (80% automatic)"
    assert LyricVideoGUI._song_label(stub, "redo", "hard-song") == "hard-song"


def test_the_hold_command_leaves_a_song_the_owner_verified_alone(tmp_path):
    _rendered(tmp_path, "checked", placed({1: 1.0, 6: -1.0}))                  # fails the bar
    mark_verified(tmp_path / "checked", automatic_share=0.8)
    before = (tmp_path / "checked" / "lyrics_timed.json").read_text(encoding="utf-8")

    scan_songs(tmp_path, hold=True)

    assert (tmp_path / "checked" / "lyrics_timed.json").read_text(encoding="utf-8") == before
    assert verification(tmp_path / "checked") is not None                       # and the verdict is still valid
