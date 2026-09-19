"""replace_report answers "which already-uploaded videos may need replacing on YouTube?" -- read-only:
it never deletes, edits or uploads anything."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from lyricvideo.models import LyricLine, Song, Word, save_song
from lyricvideo.replace_report import (
    UploadedSong, build_report, fetch_video_facts, uploaded_songs_needing_review,
)
from lyricvideo.verify_lyrics import UNCHECKED_HOLD
from lyricvideo.youtube_state import YoutubeState, save_youtube_state


def make_song(work_root, slug, concern="", uploaded=True, video_id=None):
    work_dir = work_root / slug
    work_dir.mkdir()
    save_song(
        Song(title=slug.title(), audio_path="x.mp3", lines=[LyricLine(words=[Word(word="hi")])],
             lyrics_accuracy_concern=concern),
        work_dir / "lyrics_timed.json",
    )
    if uploaded:
        save_youtube_state(work_dir, YoutubeState(video_id=video_id or f"id-{slug}", uploaded_at="2026-09-01T00:00:00", title=slug))
    return work_dir


def test_only_uploaded_songs_with_a_real_concern_are_listed(tmp_path):
    make_song(tmp_path, "uploaded-bad", concern="Only 40% of these lyrics match what is sung.")
    make_song(tmp_path, "uploaded-fine", concern="")
    make_song(tmp_path, "pending-bad", concern="Only 40% match.", uploaded=False)          # not on YouTube
    make_song(tmp_path, "uploaded-held", concern=UNCHECKED_HOLD)                            # a hold is not a verdict

    songs = uploaded_songs_needing_review(tmp_path)

    assert [s.slug for s in songs] == ["uploaded-bad"]
    assert songs[0].video_id == "id-uploaded-bad" and "40%" in songs[0].concern


class _FakeVideos:
    def __init__(self, items):
        self.items, self.calls = items, []

    def list(self, part, id):
        self.calls.append((part, id.split(",")))
        wanted = set(id.split(","))
        return SimpleNamespace(execute=lambda: {"items": [i for i in self.items if i["id"] in wanted]})


class _FakeYoutube:
    def __init__(self, items):
        self._videos = _FakeVideos(items)

    def videos(self):
        return self._videos


def item(video_id, privacy="public", publish_at=None, published_at="2026-09-01T12:00:00Z", views="120"):
    status = {"privacyStatus": privacy}
    if publish_at:
        status["publishAt"] = publish_at
    return {"id": video_id, "status": status, "snippet": {"publishedAt": published_at}, "statistics": {"viewCount": views}}


def test_video_facts_report_privacy_publish_time_and_views_and_omit_missing_videos():
    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    client = _FakeYoutube([item("a", "public", views="1500"), item("b", "private", publish_at=future, views="0")])

    facts = fetch_video_facts(client, ["a", "b", "gone"])

    assert facts["a"].privacy == "public" and facts["a"].views == 1500
    assert facts["b"].privacy == "private" and facts["b"].publish_at is not None
    assert "gone" not in facts                                      # deleted on YouTube


def test_video_facts_are_fetched_in_batches_of_fifty():
    client = _FakeYoutube([item(f"v{i}") for i in range(120)])

    facts = fetch_video_facts(client, [f"v{i}" for i in range(120)])

    assert len(facts) == 120
    assert [len(ids) for _, ids in client._videos.calls] == [50, 50, 20]


def test_the_report_groups_scheduled_public_and_missing_videos_and_sorts_public_by_views():
    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    songs = [
        UploadedSong("quiet", "Quiet", "vq", "Only 50% of these lyrics match what is sung."),
        UploadedSong("popular", "Popular", "vp", "Only 55% match."),
        UploadedSong("soon", "Soon", "vs", "About 20 sung words in a row aren't in these lyrics."),
        UploadedSong("deleted", "Deleted", "vd", "Only 30% match."),
    ]
    facts = fetch_video_facts(_FakeYoutube([
        item("vq", "public", views="10"), item("vp", "public", views="9000"),
        item("vs", "private", publish_at=future, views="0"),
    ]), ["vq", "vp", "vs", "vd"])

    text = build_report(songs, facts)

    assert text.index("Still scheduled") < text.index("Already public") < text.index("no longer on YouTube")
    assert text.index("Popular") < text.index("Quiet")               # most-viewed public video first
    assert "https://www.youtube.com/watch?v=vp" in text and "9,000 views" in text
    assert "Deleted" in text.split("no longer on YouTube")[1]
    assert "not proof" in text.lower()                              # honest about what a flag means


def test_the_report_still_works_without_youtube_information():
    songs = [UploadedSong("a", "A", "va", "Only 50% match.")]

    text = build_report(songs, None)

    assert "A" in text and "https://www.youtube.com/watch?v=va" in text
    assert "could not be checked on YouTube" in text
