"""schedule_upload refuses a cut-short video (issue #7: EASY CHORD mp4s with ~80 s of picture over 5-6 min of audio)
before the paid Claude description call and before any YouTube call. Fake clients only; nothing talks to YouTube."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyricvideo.chord_theory import save_easy_chord_capo_marker
from lyricvideo.key_decision import KeyDecision, save_decision
from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, save_song
from lyricvideo.youtube_schedule import IncompleteVideo, check_video_complete, is_cut_short, schedule_upload
from lyricvideo.youtube_state import load_youtube_state

_PUBLIC = dict(youtube_privacy="public", youtube_upload_times="15:00", youtube_category_id="27",
               youtube_made_for_kids=False, support_description_text="")


class _FakeYoutubeClient:
    """Records every resource touched; a whole video uploads as `video_id`."""

    def __init__(self, video_id="vid-new"):
        self.touched = []
        self.inserted = []
        self._video_id = video_id

    def videos(self):
        self.touched.append("videos")
        client = self

        class _Videos:
            def insert(self, **kwargs):
                client.inserted.append(kwargs)
                return SimpleNamespace(next_chunk=lambda: (None, {"id": client._video_id}))

            def list(self, **kwargs):
                return SimpleNamespace(execute=lambda: {"items": []})

        return _Videos()

    def channels(self):
        self.touched.append("channels")
        return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {
            "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "uploads-playlist"}}}]}))

    def playlistItems(self):
        self.touched.append("playlistItems")
        return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {"items": []}))


class _FakeAnthropic:
    def __init__(self):
        self.calls = 0
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls += 1
                block = SimpleNamespace(type="text", text="DESCRIPTION: Made-up words.\nTAGS: one, two")
                return SimpleNamespace(content=[block])

        self.messages = _Messages()


def _song_dir(tmp_path, video_bytes=b"fake video bytes") -> Path:
    work_dir = tmp_path / "zappo-flim"
    work_dir.mkdir()
    save_song(Song(title="Zappo Flim", audio_path="song.mp3", lines=[LyricLine(words=[Word(word="glorb")])]),
              work_dir / "lyrics_timed.json")
    save_decision(work_dir, KeyDecision(status="confirmed", key="D major", source="agreed", chord_key="D major"))
    if video_bytes is not None:
        (work_dir / "zappo-flim.mp4").write_bytes(video_bytes)
    return work_dir


def _easy_dir(tmp_path) -> Path:
    """A nested <song>/easychords version that is otherwise fine to upload (chords = the song's shifted by capo 1)."""
    def track(labels, key):
        return ChordTrack(key=key, events=[ChordEvent(start=float(i), end=float(i + 1), label=lab)
                                           for i, lab in enumerate(labels)])

    song_dir = tmp_path / "zappo-flim"
    song_dir.mkdir()
    save_song(Song(title="Zappo Flim", audio_path="a.mp3", lines=[LyricLine(words=[Word(word="glorb")])],
                   chord_track=track(["Eb", "Ab", "Bb"], "Eb major")), song_dir / "lyrics_timed.json")
    save_decision(song_dir, KeyDecision(status="confirmed", key="Eb major", source="agreed", chord_key="Eb major"))
    easy = song_dir / "easychords"
    easy.mkdir()
    save_song(Song(title="Zappo Flim EasyChords", audio_path="a.mp3", lines=[LyricLine(words=[Word(word="glorb")])],
                   chord_track=track(["D", "G", "A"], "D major")), easy / "lyrics_timed.json")
    (easy / "zappo-flim-easychords.mp4").write_bytes(b"fake video bytes")
    save_easy_chord_capo_marker(easy, capo_fret=1, shape_key="D", original_key="Eb major", original_title="Zappo Flim")
    return easy


def _probe(monkeypatch, result):
    calls = []

    def fake(path):
        calls.append(Path(path).name)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("lyricvideo.youtube_schedule.rendered_stream_seconds", fake)
    return calls


def _assert_nothing_spent_or_sent(youtube, anthropic, work_dir):
    assert youtube.inserted == []
    assert youtube.touched == []            # not even the channel's schedule was read
    assert anthropic.calls == 0             # no paid description call
    assert load_youtube_state(work_dir) is None


def test_a_cut_short_video_is_refused_before_the_description_call_and_any_youtube_call(tmp_path, monkeypatch):
    work_dir = _song_dir(tmp_path)
    probed = _probe(monkeypatch, (80.0, 300.0))
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo, match="cut short: 80 s of picture for 300 s of audio") as refused:
        schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC))

    assert refused.value.cut_short and refused.value.video_path == work_dir / "zappo-flim.mp4"
    assert probed == ["zappo-flim.mp4"]
    _assert_nothing_spent_or_sent(youtube, anthropic, work_dir)


def test_a_cut_short_easy_chord_version_is_refused_too(tmp_path, monkeypatch):
    easy = _easy_dir(tmp_path)
    _probe(monkeypatch, (80.0, 330.0))
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo, match="zappo-flim/easychords"):
        schedule_upload(youtube, anthropic, easy, SimpleNamespace(**_PUBLIC))

    _assert_nothing_spent_or_sent(youtube, anthropic, easy)


def test_a_pending_retry_of_a_cut_short_video_is_refused_the_same_way(tmp_path, monkeypatch):
    work_dir = _song_dir(tmp_path)
    _probe(monkeypatch, (80.0, 300.0))
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo):
        schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC), only_if_not_uploaded=True)

    _assert_nothing_spent_or_sent(youtube, anthropic, work_dir)


@pytest.mark.parametrize("privacy", ["unlisted", "private"])
def test_a_cut_short_video_is_refused_whatever_the_privacy(tmp_path, monkeypatch, privacy):
    work_dir = _song_dir(tmp_path)
    _probe(monkeypatch, (80.0, 300.0))
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo):
        schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**{**_PUBLIC, "youtube_privacy": privacy}))

    _assert_nothing_spent_or_sent(youtube, anthropic, work_dir)


def test_a_video_that_cannot_be_read_back_is_refused_fail_closed(tmp_path, monkeypatch):
    work_dir = _song_dir(tmp_path)
    _probe(monkeypatch, RuntimeError("Could not read back the rendered video: moov atom not found"))
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo, match="could not be checked") as refused:
        schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC))

    assert not refused.value.cut_short           # may be a passing problem (e.g. no ffmpeg): never treated as damage

    _assert_nothing_spent_or_sent(youtube, anthropic, work_dir)


def test_a_video_without_an_audio_track_is_refused(tmp_path, monkeypatch):
    work_dir = _song_dir(tmp_path)
    _probe(monkeypatch, (300.0, None))
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo, match="no readable audio"):
        schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC))

    _assert_nothing_spent_or_sent(youtube, anthropic, work_dir)


def test_a_missing_video_is_refused_before_the_description_call(tmp_path, monkeypatch):
    work_dir = _song_dir(tmp_path, video_bytes=None)
    probed = _probe(monkeypatch, (300.0, 300.0))
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo, match="does not exist") as refused:
        schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC))

    assert not refused.value.cut_short

    assert probed == []
    _assert_nothing_spent_or_sent(youtube, anthropic, work_dir)


def test_a_whole_video_within_the_one_second_allowance_uploads(tmp_path, monkeypatch):
    work_dir = _song_dir(tmp_path)
    _probe(monkeypatch, (299.2, 300.0))
    youtube, anthropic = _FakeYoutubeClient(video_id="vid-ok"), _FakeAnthropic()

    assert schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC)) == "vid-ok"
    assert len(youtube.inserted) == 1 and anthropic.calls == 1
    assert load_youtube_state(work_dir).video_id == "vid-ok"


def test_is_cut_short_uses_a_one_second_allowance_and_ignores_a_missing_audio_length():
    assert is_cut_short(80.0, 300.0)
    assert is_cut_short(298.9, 300.0)
    assert not is_cut_short(299.0, 300.0)
    assert not is_cut_short(300.0, 299.0)
    assert not is_cut_short(300.0, None)


# --- the same check on real (tiny, synthetic) mp4 files -----------------------------------------------------------

def _make_mp4(path: Path, picture_seconds: float, audio_seconds: float) -> Path:
    from lyricvideo.assemble import _ffmpeg_binary

    try:
        ffmpeg = _ffmpeg_binary()
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color=c=blue:s=64x48:r=24:d={picture_seconds}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_seconds}",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
            check=True, capture_output=True, timeout=60,
        )
    except Exception as e:  # no usable ffmpeg here: the monkeypatched tests above still cover the logic
        pytest.skip(f"could not make a synthetic mp4 ({type(e).__name__})")
    return path


def test_a_real_cut_short_mp4_is_refused(tmp_path):
    work_dir = _song_dir(tmp_path, video_bytes=None)
    _make_mp4(work_dir / "zappo-flim.mp4", picture_seconds=1, audio_seconds=5)
    youtube, anthropic = _FakeYoutubeClient(), _FakeAnthropic()

    with pytest.raises(IncompleteVideo, match="cut short: 1 s of picture for 5 s of audio"):
        schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC))

    _assert_nothing_spent_or_sent(youtube, anthropic, work_dir)


def test_a_real_whole_mp4_uploads(tmp_path):
    work_dir = _song_dir(tmp_path, video_bytes=None)
    _make_mp4(work_dir / "zappo-flim.mp4", picture_seconds=3, audio_seconds=3)
    youtube, anthropic = _FakeYoutubeClient(video_id="vid-real"), _FakeAnthropic()

    assert schedule_upload(youtube, anthropic, work_dir, SimpleNamespace(**_PUBLIC)) == "vid-real"
    assert len(youtube.inserted) == 1


def test_check_video_complete_refuses_a_file_that_is_not_a_video(tmp_path):
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"not a video at all")
    try:
        from lyricvideo.assemble import _ffmpeg_binary
        _ffmpeg_binary()
    except Exception:
        pytest.skip("no ffmpeg here")

    with pytest.raises(IncompleteVideo, match="could not be checked"):
        check_video_complete(bogus, "bogus")
