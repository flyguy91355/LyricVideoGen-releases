"""Issue #7 review, wave 2 (lyrics): regression tests for L02-L11 and the identify/text_clean follow-ups.
Every lyric line here is made-up placeholder text."""

import json
from types import SimpleNamespace

import pytest

from lyricvideo.anchors import HeardWord
from lyricvideo.fetch_lyrics import (
    _drop_header_lines,
    _hit_to_lines_and_times,
    artist_matches,
    fetch_lyric_lines_verified,
)
from lyricvideo.lyric_arbiter import arbitrate, describe_arbitration, parse_arbitration
from lyricvideo.lyric_audio_match import (
    MIN_COVERAGE,
    _tokens,
    audio_match_passes,
    coverage_excusing,
    drop_unsung_leading_lines,
    drop_unsung_trailing_lines,
    score_lyrics_against_transcript,
)
from lyricvideo.models import LyricLine, Song, Word, load_song, save_song

# --- shared placeholder song ------------------------------------------------------------------------------------

LINES = [
    "alpha bravo charlie delta",
    "echo foxtrot golf hotel",
    "india juliett kilo lima",
    "mike november oscar papa",
    "quebec romeo sierra tango",
    "uniform victor whiskey xray",
]
REAL_STARTS = [100.0, 115.0, 130.0, 145.0, 160.0, 172.0]      # this recording: singing ends about 176 s


def _heard_at_real_times():
    heard = []
    for line, start in zip(LINES, REAL_STARTS):
        for k, word in enumerate(line.split()):
            heard.append(HeardWord(word, start + k * 0.9, start + k * 0.9 + 0.8))
    return heard


LOUD_100_TO_176 = [0.0] * 200 + [1.0] * 152 + [0.0] * 40         # 0.5 s hops


# --- L02: a heard line is never dropped as a "credit" -----------------------------------------------------------

def test_heard_last_lines_are_kept_when_the_source_was_timed_to_a_longer_edition():
    stamps = [t + 20.0 for t in REAL_STARTS]                        # the source's edition has a 20 s longer intro

    lines, times, dropped = drop_unsung_trailing_lines(LINES, stamps, _heard_at_real_times(), LOUD_100_TO_176, hop=0.5)

    assert dropped == [] and lines == LINES and times == stamps


def test_unheard_credits_after_late_stamped_real_lines_are_still_dropped():
    credits = ["Strings : Zorblat Ensemble", "Mastering : Quennick Vale"]
    stamps = [t + 20.0 for t in REAL_STARTS] + [215.0, 216.0]

    lines, _, dropped = drop_unsung_trailing_lines(
        LINES + credits, stamps, _heard_at_real_times(), LOUD_100_TO_176, hop=0.5,
    )

    assert lines == LINES and dropped == credits


def test_heard_first_lines_are_kept_when_the_source_was_timed_to_a_shorter_intro():
    heard = [HeardWord(w, 20.0 + i * 0.5, 20.4 + i * 0.5) for i, w in enumerate("alpha bravo charlie delta".split())]
    heard += [HeardWord(w, 30.0 + i * 0.5, 30.4 + i * 0.5) for i, w in enumerate("echo foxtrot golf hotel".split())]
    loud = [0.0] * 40 + [1.0] * 100                                 # silent until 20 s

    lines, _, dropped = drop_unsung_leading_lines(LINES[:3], [1.0, 2.0, 40.0], heard, loud, hop=0.5)

    assert dropped == [] and lines == LINES[:3]


def test_unheard_leading_credits_before_early_stamped_real_lines_are_still_dropped():
    heard = [HeardWord(w, 20.0 + i * 0.5, 20.4 + i * 0.5) for i, w in enumerate("alpha bravo charlie delta".split())]
    loud = [0.0] * 40 + [1.0] * 100

    lines, _, dropped = drop_unsung_leading_lines(
        ["Composer : Brindle Oakum"] + LINES[:2], [0.2, 1.0, 12.0], heard, loud, hop=0.5,
    )

    assert dropped == ["Composer : Brindle Oakum"] and lines == LINES[:2]


# --- L03: typographic apostrophes and accents -------------------------------------------------------------------

CONTRACTIONS = [
    "we won't carry the quimble home",
    "i'm not sure you're the zandor",
    "they don't fold the brisket lanterns",
    "she can't hear the dorvish bells",
]
RIGHT_QUOTE, LEFT_QUOTE, MODIFIER_APOSTROPHE = chr(0x2019), chr(0x2018), chr(0x02BC)


def test_tokens_fold_curly_apostrophes_and_accents():
    assert _tokens(f"Don{RIGHT_QUOTE}t") == ["don't"]
    assert _tokens(f"don{LEFT_QUOTE}t I{MODIFIER_APOSTROPHE}m") == ["don't", "i'm"]
    assert _tokens("Zéphyra blörk ÑANDU") == ["zephyra", "blork", "nandu"]
    assert _tokens("straße") == ["strasse"]


def test_lyrics_with_curly_apostrophes_match_a_transcript_with_straight_ones():
    from lyricvideo.fetch_lyrics import _clean_lyric_lines

    heard = " ".join(CONTRACTIONS)
    curly = _clean_lyric_lines([line.replace("'", RIGHT_QUOTE) for line in CONTRACTIONS])
    assert RIGHT_QUOTE in curly[0]                                   # NFKC cleaning leaves U+2019 alone

    straight = score_lyrics_against_transcript(CONTRACTIONS, heard)
    typographic = score_lyrics_against_transcript(curly, heard)

    assert typographic.coverage == straight.coverage == 1.0
    assert audio_match_passes(typographic)


def test_accented_lyrics_match_a_transcript_written_without_accents():
    lyrics = ["zéphyra blörk canción mañana", "the señor dreams of piñata ríos", "olé olé the fjörd"]
    heard = "zephyra blork cancion manana the senor dreams of pinata rios ole ole the fjord"

    match = score_lyrics_against_transcript(lyrics, heard)

    assert match.coverage == 1.0 and audio_match_passes(match)


# --- L04: section headers are not lyrics ------------------------------------------------------------------------

GENIUS_STYLE = (
    "[Verse 1]\nalpha bravo charlie delta\necho foxtrot golf hotel\n\n"
    "[Chorus: Some Singer]\nindia juliett kilo lima\n(x2)\n\n"
    "Chorus:\n[Bridge] mike november oscar papa\n{Outro}\nRepeat chorus\n(ooh) quebec romeo sierra\n"
)


def test_section_markers_and_repeat_directives_are_dropped_and_a_leading_tag_is_taken_off():
    lines, times = _hit_to_lines_and_times((GENIUS_STYLE, False, 0.0), 0.0)

    assert lines == [
        "alpha bravo charlie delta", "echo foxtrot golf hotel", "india juliett kilo lima",
        "mike november oscar papa", "(ooh) quebec romeo sierra",
    ]
    assert times is None
    assert not any(line.startswith(("[", "{")) for line in lines)


def test_section_markers_in_a_synced_source_are_dropped_with_their_times():
    lrc = "[00:10.00][Verse 1]\n[00:11.00]alpha bravo charlie delta\n[00:15.00][Chorus]\n[00:16.00]echo foxtrot golf\n"

    lines, times = _hit_to_lines_and_times((lrc, True, 0.0), 60.0)

    assert lines == ["alpha bravo charlie delta", "echo foxtrot golf"] and times == [11.0, 16.0]


def test_a_bare_section_name_in_parentheses_is_dropped_but_a_backing_vocal_is_kept():
    rows = "(Chorus)\nalpha bravo charlie\n(Verse 2)\n(Instrumental)\n(zandor quimble) delta echo\n(zandor quimble)\n"

    lines, _ = _hit_to_lines_and_times((rows, False, 0.0), 0.0)

    assert lines == ["alpha bravo charlie", "(zandor quimble) delta echo", "(zandor quimble)"]


def test_a_real_lyric_that_mentions_a_section_word_is_kept():
    rows = "the chorus of the zandor sings\nverse and verse again we fold\nrepeat after me the quimble\n"

    lines, _ = _hit_to_lines_and_times((rows, False, 0.0), 0.0)

    assert lines == ["the chorus of the zandor sings", "verse and verse again we fold", "repeat after me the quimble"]


def test_genius_style_text_is_verified_without_its_headers(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: None)
    monkeypatch.setattr(
        "lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit",
        lambda title, artist, providers=None: (GENIUS_STYLE, False, 0.0) if providers == ["Genius"] else None,
    )
    heard = "alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo lima mike november oscar papa " \
            "quebec romeo sierra"

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 60.0, [], object(),
        audio_check=lambda ls: score_lyrics_against_transcript(ls, heard),
    )

    assert source == "Genius" and concern == ""
    assert not any("verse" in line.lower() or "chorus" in line.lower() for line in lines)


# --- L05: a judge's verdict cannot wave through a coverage failure it was never asked about ----------------------

HALF_HEARD = [f"w{n}a w{n}b w{n}c w{n}d" for n in range(10)]           # 4 distinct content words per line
HALF_HEARD_TRANSCRIPT = " ".join(f"w{n}a w{n}b" for n in range(10))    # only the first two of each line are heard


class _FakeJudge:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def _judged_recognizer_error(a, b):
    return json.dumps({"ranges": [{"lines": [a, b], "verdict": "recognizer_error", "reason": "loop"}],
                       "missing_section": False})


def test_one_excused_unheard_line_does_not_confirm_a_song_that_fails_on_coverage():
    lyrics = HALF_HEARD + ["zorblat quennick vandermoot plinth"]
    match = score_lyrics_against_transcript(lyrics, HALF_HEARD_TRANSCRIPT)
    assert match.unsupported_ranges == [(11, 11)] and match.coverage < MIN_COVERAGE

    result = arbitrate(_FakeJudge(_judged_recognizer_error(11, 11)), lyrics, match, [])

    assert result is not None and result.confirmed is False
    assert result.short_coverage is not None and result.short_coverage < MIN_COVERAGE
    assert "70%" in describe_arbitration(result)


def test_excusing_the_judged_ranges_still_confirms_a_song_that_only_failed_there():
    heard_lines = [f"w{n}a w{n}b w{n}c w{n}d" for n in range(6)]
    lyrics = heard_lines + ["zorblat quennick vandermoot plinth", "gorsley pim wandle frick", "mardle quisp fennow"]
    match = score_lyrics_against_transcript(lyrics, " ".join(heard_lines))
    assert match.coverage < MIN_COVERAGE and match.unsupported_ranges == [(7, 9)]

    result = parse_arbitration(_judged_recognizer_error(7, 9), match.unsupported_ranges, match=match)

    assert result.confirmed is True and result.short_coverage is None
    assert coverage_excusing(match, match.unsupported_ranges) == 1.0


def test_coverage_excusing_counts_only_the_given_lines():
    match = score_lyrics_against_transcript(HALF_HEARD, HALF_HEARD_TRANSCRIPT)

    assert match.coverage == pytest.approx(0.5)
    assert coverage_excusing(match, []) == pytest.approx(0.5)
    assert coverage_excusing(match, [(1, 2)]) == pytest.approx(24 / 40)


# --- L07: the text-only fallback never reads a bad reply as a pass ----------------------------------------------

class _Reply:
    def __init__(self, blocks=None, raises=None):
        self.blocks, self.raises, self.calls = blocks or [], raises, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(content=self.blocks)


def _text(text):
    return SimpleNamespace(type="text", text=text)


@pytest.mark.parametrize("blocks", [
    [SimpleNamespace(type="thinking", thinking="")],                   # thinking used the whole budget
    [_text("LOOKS_ACCURATE: NO\nCONCERN:")],                          # NO with no reason
    [_text("I cannot tell.")],                                         # no labels at all
])
def test_an_unclear_reply_is_not_accurate_and_always_carries_a_concern(blocks):
    from lyricvideo.lyric_accuracy import check_lyric_accuracy

    accurate, concern = check_lyric_accuracy(_Reply(blocks), "T", "A", ["alpha bravo"])

    assert accurate is False and concern


def test_markdown_bold_labels_are_read():
    from lyricvideo.lyric_accuracy import check_lyric_accuracy

    reply = _Reply([_text("**LOOKS_ACCURATE:** NO\n**CONCERN:** looks like a different song")])

    assert check_lyric_accuracy(reply, "T", "A", ["alpha"]) == (False, "looks like a different song")
    assert check_lyric_accuracy(_Reply([_text("**LOOKS_ACCURATE:** YES\n**CONCERN:**")]), "T", "A", ["a"]) == (True, "")


def test_an_api_error_holds_the_lyrics_instead_of_failing_the_song_and_thinking_is_off():
    from lyricvideo.lyric_accuracy import check_lyric_accuracy

    accurate, concern = check_lyric_accuracy(_Reply(raises=RuntimeError("529 overloaded")), "T", "A", ["alpha"])
    assert accurate is False and "could not run" in concern

    client = _Reply([_text("LOOKS_ACCURATE: YES\nCONCERN:")])
    check_lyric_accuracy(client, "T", "A", ["alpha"])
    assert client.calls[0]["thinking"] == {"type": "disabled"}


def test_the_fallback_path_never_returns_an_empty_concern_for_a_failed_check(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_lrclib_hit", lambda *a, **k: ("alpha bravo", False, 0.0))
    monkeypatch.setattr("lyricvideo.fetch_lyrics._fetch_syncedlyrics_hit", lambda *a, **k: None)

    monkeypatch.setattr("lyricvideo.fetch_lyrics.check_lyric_accuracy", lambda *a, **k: (False, ""))
    assert fetch_lyric_lines_verified(audio_path, "T", "A", 10.0, [], object())[2]

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("lyricvideo.fetch_lyrics.check_lyric_accuracy", boom)
    lines, _source, concern = fetch_lyric_lines_verified(audio_path, "T", "A", 10.0, [], object())
    assert lines == ["alpha bravo"] and "could not run" in concern


# --- L10 / L11 ----------------------------------------------------------------------------------------------------

def test_artist_matches_compares_whole_words_only():
    assert artist_matches("Florna Quartet", "Lorn") is False
    assert artist_matches("Mikail Brand", "Kai") is False
    assert artist_matches("Alpha Beta Gamma", "Alpha Beta") is True
    assert artist_matches("Alpha", "Alpha Beta") is True
    assert artist_matches("Alpha Beta feat. Gamma", "The Alpha Beta") is True


def test_a_self_titled_songs_first_line_that_is_just_the_title_is_kept():
    assert _drop_header_lines(["Alpha", "beta gamma delta"], [5.0, 9.0], "Alpha", "Alpha") == (
        ["Alpha", "beta gamma delta"], [5.0, 9.0])
    assert _drop_header_lines(["Alpha!", "beta gamma"], None, "Alpha", "The Alpha")[0] == ["Alpha!", "beta gamma"]
    assert _drop_header_lines(["Alpha - Alpha", "beta gamma"], None, "Alpha", "Alpha")[0] == ["beta gamma"]


def test_a_title_that_contains_the_artist_keeps_a_title_only_first_line():
    assert _drop_header_lines(["Alpha Beta", "gamma"], None, "Alpha Beta", "Alpha")[0] == ["Alpha Beta", "gamma"]
    assert _drop_header_lines(["Alpha - Alpha Beta", "gamma"], None, "Alpha Beta", "Alpha")[0] == ["gamma"]
    assert _drop_header_lines(["Alpha Beta - The Alpha", "gamma"], None, "Alpha Beta", "The Alpha")[0] == ["gamma"]


# --- L01 hook: a per-candidate trim is applied before the audio check -------------------------------------------

def test_a_trim_removes_credits_before_the_audio_check_so_the_song_passes(tmp_path, monkeypatch):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    credits = ["Strings : Zorblat Ensemble", "Piano : Quennick Vale", "Mixing : Brindle Oakum", "Horns : Pim Wandle"]
    lrc = "\n".join(f"[{int(t) // 60:02d}:{int(t) % 60:02d}.00]{line}"
                    for t, line in zip(REAL_STARTS + [200.0, 201.0, 202.0, 203.0], LINES + credits))
    (tmp_path / "song.lrc").write_text(lrc, encoding="utf-8")
    heard = _heard_at_real_times()
    heard_text = " ".join(h.word for h in heard)

    def trim(lines, times):
        return drop_unsung_trailing_lines(lines, times, heard, LOUD_100_TO_176, hop=0.5)

    untrimmed = fetch_lyric_lines_verified(
        audio_path, "T", "A", 240.0, [], object(),
        audio_check=lambda ls: score_lyrics_against_transcript(ls, heard_text),
    )
    assert untrimmed[2] != ""                                         # four unsung credit lines fail the check

    times_out: dict = {}
    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 240.0, [], object(),
        audio_check=lambda ls: score_lyrics_against_transcript(ls, heard_text), times_out=times_out, trim=trim,
    )

    assert (lines, source, concern) == (LINES, "sidecar", "")
    assert times_out["line_times"] == REAL_STARTS


def test_a_trim_that_raises_keeps_the_candidate_as_it_was(tmp_path):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    (tmp_path / "song.txt").write_text("\n".join(LINES), encoding="utf-8")

    def broken(lines, times):
        raise RuntimeError("no transcript")

    lines, source, concern = fetch_lyric_lines_verified(
        audio_path, "T", "A", 240.0, [], object(),
        audio_check=lambda ls: score_lyrics_against_transcript(ls, " ".join(LINES)), trim=broken,
    )

    assert (lines, source, concern) == (LINES, "sidecar", "")


# --- L06 / L08 / L09: verify_lyrics ---------------------------------------------------------------------------------

SUNG = LINES
WRONG = [
    "zorblat quennick vandermoot plinth", "gorsley pim wandle frick", "brindle oakum tessary vole",
    "mardle quisp fennow gratch", "splindy horrock valm tuzz", "yarrow blenk dimsy crooth",
]
GATE_ONLY = ("SET ASIDE FOR REVIEW -- the lyric timing is not precise enough: only 80% of the lines start within "
             "half a second of the singing (90% are needed).")


def _song_dir(tmp_path, slug, lines, concern="", source=""):
    work_dir = tmp_path / slug
    work_dir.mkdir()
    stem = work_dir / "vocals.wav"
    stem.write_bytes(b"x" * 10)
    song = Song(
        title=slug, audio_path=str(work_dir / "song.mp3"), vocal_stem_path=str(stem),
        lines=[LyricLine(words=[Word(word=w) for w in line.split()]) for line in lines],
        lyrics_accuracy_concern=concern, lyrics_source=source,
    )
    save_song(song, work_dir / "lyrics_timed.json")
    return work_dir


def _hears(text):
    return lambda vocals, work_dir: text


def _concern(work_dir):
    return load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern


def test_a_report_only_run_then_a_flag_run_releases_the_hold_on_a_song_that_passes(tmp_path, monkeypatch):
    from lyricvideo import verify_lyrics
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    root = tmp_path / "work"
    root.mkdir()
    held = _song_dir(root, "held", SUNG, concern=UNCHECKED_HOLD)
    report = tmp_path / "report.jsonl"
    real_verify_all = verify_lyrics.verify_all
    monkeypatch.setattr(verify_lyrics, "verify_all",
                        lambda *a, **k: real_verify_all(*a, transcribe=_hears(" ".join(SUNG)), **k))

    verify_lyrics.main(["--work-root", str(root), "--report", str(report), "--no-ai"])
    assert _concern(held) == UNCHECKED_HOLD                         # report-only writes nothing

    verify_lyrics.main(["--work-root", str(root), "--report", str(report), "--no-ai", "--flag"])
    assert _concern(held) == ""                                      # the --flag run applied the pass


def test_a_lyric_failure_is_recorded_in_front_of_a_timing_gate_concern(tmp_path):
    from lyricvideo.timing_gate import hold_if_timing_fails, is_gate_concern
    from lyricvideo.verify_lyrics import verify_song

    assert is_gate_concern(GATE_ONLY)
    work_dir = _song_dir(tmp_path, "gate", WRONG, concern=GATE_ONLY)

    verdict = verify_song(work_dir, transcribe=_hears(" ".join(SUNG)), flag=True)

    stored = _concern(work_dir)
    assert verdict.status == "flagged"
    assert "match what is sung" in stored and stored.endswith(GATE_ONLY)
    assert not is_gate_concern(stored)                               # the timing gate can no longer release it
    hold_if_timing_fails(work_dir, needed=0.5)
    assert _concern(work_dir) == stored


def test_a_timing_gate_concern_is_left_alone_when_the_lyrics_pass(tmp_path):
    from lyricvideo.verify_lyrics import verify_song

    work_dir = _song_dir(tmp_path, "gate-ok", SUNG, concern=GATE_ONLY)

    verdict = verify_song(work_dir, transcribe=_hears(" ".join(SUNG)), flag=True)

    assert verdict.status == "verified" and _concern(work_dir) == GATE_ONLY


def test_rechecking_a_combined_concern_that_now_passes_keeps_its_timing_part(tmp_path):
    from lyricvideo.verify_lyrics import verify_song

    combined = "Only 40% of these lyrics match what is sung; lines 1-6 don't match the audio. " + GATE_ONLY
    work_dir = _song_dir(tmp_path, "combined", SUNG, concern=combined)

    verdict = verify_song(work_dir, transcribe=_hears(" ".join(SUNG)), flag=True, recheck=True)

    assert verdict.status == "verified" and _concern(work_dir) == GATE_ONLY


def test_a_flag_run_rechecks_an_old_already_flagged_row_whose_concern_is_the_timing_gates_alone(tmp_path, monkeypatch):
    from lyricvideo import verify_lyrics

    root = tmp_path / "work"
    root.mkdir()
    _song_dir(root, "gate", WRONG, concern=GATE_ONLY)
    _song_dir(root, "lyric", WRONG, concern="Looks like a different edition.")
    report = tmp_path / "report.jsonl"
    report.write_text("\n".join(json.dumps({"slug": s, "status": "already-flagged"}) for s in ("gate", "lyric")) + "\n")
    seen = {}
    monkeypatch.setattr(verify_lyrics, "verify_all", lambda *a, **k: seen.update(skip=k["skip"]) or [])

    verify_lyrics.main(["--work-root", str(root), "--report", str(report), "--no-ai", "--flag"])

    assert seen["skip"] == {"lyric"}


def test_the_owners_own_lyrics_are_never_checked_flagged_or_held(tmp_path):
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD, verify_song

    def must_not_listen(vocals, work_dir):
        raise AssertionError("the owner's lyrics are final; nothing is transcribed for them")

    owner = _song_dir(tmp_path, "owner", WRONG, source="owner")
    verdict = verify_song(owner, transcribe=must_not_listen, flag=True)
    assert verdict.status == "owner-lyrics" and _concern(owner) == ""

    held = _song_dir(tmp_path, "owner-held", WRONG, concern=UNCHECKED_HOLD, source="owner")
    assert verify_song(held, transcribe=must_not_listen, flag=False).status == "owner-lyrics"
    assert _concern(held) == UNCHECKED_HOLD                          # report-only still writes nothing
    assert verify_song(held, transcribe=must_not_listen, flag=True).status == "owner-lyrics"
    assert _concern(held) == ""                                      # an older hold on them is released


def test_a_report_only_run_then_a_flag_run_releases_an_old_hold_on_the_owners_lyrics(tmp_path):
    from lyricvideo import verify_lyrics
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD

    root = tmp_path / "work"
    root.mkdir()
    held = _song_dir(root, "owner-held", WRONG, concern=UNCHECKED_HOLD, source="owner")
    report = tmp_path / "report.jsonl"

    verify_lyrics.main(["--work-root", str(root), "--report", str(report), "--no-ai"])
    assert _concern(held) == UNCHECKED_HOLD                          # report-only writes nothing ("owner-lyrics" row)

    verify_lyrics.main(["--work-root", str(root), "--report", str(report), "--no-ai", "--flag"])
    assert _concern(held) == ""                                      # the --flag run still releases it


def test_hold_pending_skips_songs_with_the_owners_own_lyrics(tmp_path):
    from lyricvideo.key_decision import KeyDecision, save_decision
    from lyricvideo.pipeline import slugify
    from lyricvideo.verify_lyrics import UNCHECKED_HOLD, hold_unchecked

    def rendered(work_dir):
        save_decision(work_dir, KeyDecision(status="confirmed", key="C major", source="agreed", chord_key="C major"))
        (work_dir / f"{slugify(work_dir.name)}.mp4").write_bytes(b"video")

    waiting = _song_dir(tmp_path, "waiting", SUNG)
    rendered(waiting)
    owner = _song_dir(tmp_path, "owner", SUNG, source="owner")
    rendered(owner)

    assert hold_unchecked(tmp_path) == ["waiting"]
    assert _concern(waiting) == UNCHECKED_HOLD and _concern(owner) == ""


# --- identify / text_clean follow-ups ---------------------------------------------------------------------------

def test_strip_title_noise_is_public_and_keeps_a_leading_number():
    from lyricvideo.text_clean import clean_title, strip_title_noise

    assert strip_title_noise("19-2000 (Official Video)") == "19-2000"
    assert strip_title_noise("01 Numbered On Purpose [HD]") == "01 Numbered On Purpose"
    assert clean_title("01 - Some Song (Lyric Video)") == "Some Song"
    assert clean_title("1-800-273-8255") == "1-800-273-8255"
    assert clean_title("747 - Some Song") == "747 - Some Song"


def test_identify_no_longer_uses_text_cleans_private_pattern():
    import lyricvideo.identify as identify

    assert not hasattr(identify, "_PAREN_NOISE")


class _MBResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_a_musicbrainz_resolved_artist_keeps_each_credited_name(tmp_path, monkeypatch):
    from lyricvideo.identify import extract_metadata

    recording = {
        "score": 100, "length": 200000, "title": "Some Title", "releases": [1, 2],
        "artist-credit": [
            {"name": "Alpha", "joinphrase": ", "},
            {"name": "Beta, Gamma and Delta", "joinphrase": " & "},
            {"name": "Epsilon"},
        ],
    }
    monkeypatch.setattr("lyricvideo.identify.read_tags", lambda path: ("Some Title", "", "", 200.0))
    monkeypatch.setattr("lyricvideo.identify.lrclib_artist_for_title", lambda title: None)
    monkeypatch.setattr("lyricvideo.identify.requests.get", lambda *a, **k: _MBResponse({"recordings": [recording]}))

    info = extract_metadata(tmp_path / "whatever.mp3")

    assert info.source == "musicbrainz"
    assert info.artist == "Alpha, Beta, Gamma and Delta & Epsilon"
    assert info.artists == ["Alpha", "Beta, Gamma and Delta", "Epsilon"]


def test_artists_stay_empty_unless_musicbrainz_resolved_the_artist(tmp_path, monkeypatch):
    from lyricvideo.identify import extract_metadata

    monkeypatch.setattr("lyricvideo.identify.read_tags", lambda path: ("Some Title", "Some Band", "", 200.0))
    assert extract_metadata(tmp_path / "x.mp3").artists == []

    # an older two-value lookup result still works
    monkeypatch.setattr("lyricvideo.identify.read_tags", lambda path: ("Some Title", "", "", 200.0))
    monkeypatch.setattr("lyricvideo.identify.lrclib_artist_for_title", lambda title: None)
    monkeypatch.setattr("lyricvideo.identify.musicbrainz_lookup", lambda *a, **k: ("Solo Act", "Some Title"))
    info = extract_metadata(tmp_path / "x.mp3")
    assert info.artist == "Solo Act" and info.artists == []
