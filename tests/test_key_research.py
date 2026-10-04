import json
from types import SimpleNamespace

from lyricvideo.key_decision import decide_key, load_decision, settle_song_key
from lyricvideo.key_estimate import KeyEstimate
from lyricvideo.key_research import KeyResearch, parse_key_research, research_song_key
from lyricvideo.models import ChordEvent, ChordTrack

CANDS = ["D major", "G major", "B minor"]
D_MAJOR = KeyEstimate(tonic=2, mode="major", margin=0.3)


def reply(key="G major", cites=("musicnotes.com", "hooktheory.com"), confidence="high", notes="both list G major"):
    return json.dumps({"key": key, "citations": list(cites), "confidence": confidence, "notes": notes})


class Client:
    """Answers the second-opinion call (no tools) and the research call (web_search tool) differently."""
    def __init__(self, opinion, research, raises=False):
        self.opinion, self.research, self.raises, self.calls, self.messages = opinion, research, raises, [], self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if "tools" in kwargs:
            if self.raises:
                raise RuntimeError("boom")
            text = self.research
        else:
            text = self.opinion
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, server_tool_use=SimpleNamespace(web_search_requests=2))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage)


def song():
    ev, t = [], 0.0
    for label, secs in (("D", 8), ("G", 4), ("A", 4), ("D", 8)):
        ev.append(ChordEvent(start=t, end=t + secs, label=label))
        t += secs
    return ChordTrack(events=ev, key="D major", bpm=100.0)


def test_parse_accepts_a_confident_cited_answer_on_the_list():
    r = parse_key_research("sure!\n```json\n" + reply() + "\n```", CANDS)
    assert r.key == "G major" and r.confident and r.sources == ["musicnotes.com", "hooktheory.com"]


def test_one_source_low_confidence_or_off_list_is_not_confident():
    assert not parse_key_research(reply(cites=("musicnotes.com",)), CANDS).confident
    assert not parse_key_research(reply(confidence="low"), CANDS).confident
    off = parse_key_research(reply(key="Eb major"), CANDS)
    assert off.key == "" and not off.confident
    assert parse_key_research(reply(key="NONE"), CANDS).key == ""


def test_parse_rejects_garbage():
    assert parse_key_research("no json here", CANDS) is None
    assert parse_key_research("{not json}", CANDS) is None
    assert parse_key_research("[1]", CANDS) is None


def test_research_call_uses_web_search_and_reports_cost():
    c = Client("", reply())
    r = research_song_key(c, "x", "y", CANDS, "D 50%")
    assert c.calls[0]["tools"][0]["name"] == "web_search" and c.calls[0]["tools"][0]["max_uses"] == 2 and c.calls[0]["model"] == "claude-haiku-4-5"
    assert c.calls[0]["tools"][0]["type"] == "web_search_20250305" and "output_config" not in c.calls[0]
    assert r.key == "G major" and abs(r.cost_usd - (0.001 + 0.001 + 0.02)) < 1e-9


def test_research_failure_or_bad_reply_is_none():
    assert research_song_key(Client("", reply(), raises=True), "x", "y", CANDS, "") is None
    assert research_song_key(Client("", "nothing"), "x", "y", CANDS, "") is None


def test_confident_research_settles_a_disagreement():
    d = decide_key(D_MAJOR, "G major", None, CANDS, KeyResearch("G major", ["a", "b"], True, "n"))
    assert d.confirmed and d.key == "G major" and d.source == "researched" and d.research_sources == ["a", "b"]


def test_unconfident_research_leaves_review_and_the_reason_shows_it():
    d = decide_key(D_MAJOR, "G major", None, CANDS, KeyResearch("G major", ["a"], False, "only one site"))
    assert not d.confirmed
    assert "web research chose G major" in d.concern() and "only one site" in d.concern()


def test_owner_still_beats_research():
    d = decide_key(D_MAJOR, "G major", "B minor", CANDS, KeyResearch("G major", ["a", "b"], True, ""))
    assert d.key == "B minor" and d.source == "owner"


def test_settle_song_key_researches_only_on_disagreement(tmp_path):
    agree = Client("KEY: D major", reply())
    decision, _ = settle_song_key(tmp_path, song(), "x", "y", agree)
    assert decision.source == "agreed" and len(agree.calls) == 1          # no web search when the two already agree

    disagree = Client("KEY: G major", reply("G major"))
    decision, _ = settle_song_key(tmp_path, song(), "x", "y", disagree)
    assert decision.confirmed and decision.key == "G major" and decision.source == "researched"
    assert len(disagree.calls) == 2
    assert load_decision(tmp_path).research_sources == ["musicnotes.com", "hooktheory.com"]


def test_no_second_opinion_still_researches(tmp_path):
    decision, _ = settle_song_key(tmp_path, song(), "x", "y", Client("KEY: NONE", reply("D major")))
    assert decision.confirmed and decision.key == "D major" and decision.source == "researched"


def test_failed_research_keeps_the_song_for_the_owner(tmp_path):
    decision, _ = settle_song_key(tmp_path, song(), "x", "y", Client("KEY: G major", "", raises=True))
    assert not decision.confirmed


def test_old_decision_files_without_research_fields_still_load(tmp_path):
    (tmp_path / "key_decision.json").write_text(json.dumps({"status": "review", "chord_key": "D major"}), encoding="utf-8")
    assert load_decision(tmp_path).research_sources == []
