"""L01 (issue #7 review, wave 2): unsung credit lines are dropped BEFORE the lyrics are judged against the audio, so
correct lyrics are not held for their credits and the concern's line numbers are the saved lines' own. Made-up words
only."""

import json
from pathlib import Path

from lyricvideo.lyric_audio_match import describe_mismatch, score_lyrics_against_transcript
from lyricvideo.pipeline import _concern_after_trim, run_pipeline
from tests.test_pipeline import _patch_common

SUNG = ["zorblat quintor vesh plimbo", "drastik worfel glimmet trosk"]
HEARD_TEXT = "zorblat quintor vesh plimbo drastik worfel glimmet trosk"
CREDITS = ["Guitar : Fennimore Quadwright", "Piano : Ostrava Pellingham", "Strings : Brinkle Hollowmere",
           "Drums : Tavish Oddlebury"]
TIMES = [10.0, 16.0, 205.0, 206.0, 207.0, 208.0]           # the credits are stamped long after the singing ends (~22 s)


def _audio_evidence(monkeypatch):
    words = HEARD_TEXT.split()
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: HEARD_TEXT)
    monkeypatch.setattr(
        "lyricvideo.pipeline.load_transcript_words",
        lambda work_dir: [{"word": w, "start": 10.0 + 1.4 * i, "end": 10.9 + 1.4 * i} for i, w in enumerate(words)],
    )
    monkeypatch.setattr("lyricvideo.pipeline.vocal_loudness", lambda path, hop=0.5: [1.0] * 44 + [0.0] * 400)


def test_credit_lines_dropped_after_the_fetch_leave_a_song_that_passes_with_no_concern(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    _audio_evidence(monkeypatch)

    def fetch(audio_path, title, artist, duration, alt_titles, client, audio_check=None, reconcile=None, arbiter=None,
              times_out=None):
        lines = SUNG + CREDITS
        times_out["line_times"] = list(TIMES)
        return lines, "sidecar", describe_mismatch(audio_check(lines))            # the credits fail the whole candidate

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fetch)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir, end_stage="fetch_lyrics")

    saved = json.loads((work_dir / "lyric_lines.json").read_text(encoding="utf-8"))
    assert saved["lines"] == SUNG and saved["line_times"] == TIMES[:2]
    assert saved["concern"] == ""


def test_a_fetch_that_takes_the_trim_gets_it_and_it_drops_only_the_credits(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    _audio_evidence(monkeypatch)
    dropped_inside = []

    def fetch(audio_path, title, artist, duration, alt_titles, client, audio_check=None, reconcile=None, arbiter=None,
              times_out=None, trim=None):
        lines, times, dropped = trim(SUNG + CREDITS, list(TIMES))                # trimmed BEFORE it is scored
        dropped_inside.append(dropped)
        times_out["line_times"] = times
        match = audio_check(lines)
        return lines, "sidecar", "" if match.coverage >= 0.7 else describe_mismatch(match)

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fetch)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir, end_stage="fetch_lyrics")

    assert dropped_inside == [CREDITS]
    saved = json.loads((work_dir / "lyric_lines.json").read_text(encoding="utf-8"))
    assert saved["lines"] == SUNG and saved["concern"] == ""


def test_a_trimmed_song_that_still_fails_names_its_own_line_numbers_and_keeps_the_rest_of_the_concern():
    heard = "zorblat quintor vesh plimbo"
    check = lambda lines: score_lyrics_against_transcript(lines, heard)   # noqa: E731
    kept = ["zorblat quintor vesh plimbo", "grelk moopish vantaro", "splendle quorbin hask", "tivvel morsk pandry",
            "yorbit kessle frame"]
    before = CREDITS[:1] + kept + CREDITS[1:]
    concern = describe_mismatch(check(before)) + " The AI judge found a wrong stretch."

    after = _concern_after_trim(check, before, kept, concern)

    assert after.startswith(describe_mismatch(check(kept)))
    assert "lines 2-5" in after and after.endswith("The AI judge found a wrong stretch.")
