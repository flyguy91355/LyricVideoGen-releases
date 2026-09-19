import json
from types import SimpleNamespace

import pytest

from lyricvideo.transcribe import transcribe_vocals


class _FakeModel:
    """Stands in for faster_whisper.WhisperModel: same .transcribe() shape."""

    def __init__(self, texts, language="en"):
        self.texts = texts
        self.language = language
        self.calls = 0

    def transcribe(self, path, **kwargs):
        self.calls += 1
        segments = (SimpleNamespace(start=i * 5.0, end=i * 5.0 + 4.0, text=t) for i, t in enumerate(self.texts))
        return segments, SimpleNamespace(language=self.language, language_probability=0.9)


def _vocals(tmp_path, size=100):
    path = tmp_path / "vocals.wav"
    path.write_bytes(b"x" * size)
    return path


def test_returns_the_heard_text_joined_across_segments(tmp_path):
    model = _FakeModel([" look at all the lonely people", " where do they all come from"])

    text = transcribe_vocals(_vocals(tmp_path), tmp_path, model=model)

    assert text == "look at all the lonely people where do they all come from"


def test_writes_a_transcript_cache_into_the_work_dir(tmp_path):
    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_FakeModel([" hello there"]))

    cached = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert cached["text"] == "hello there"
    assert cached["language"] == "en"
    assert cached["segments"] == [{"start": 0.0, "end": 4.0, "text": "hello there"}]


def test_a_second_call_reuses_the_cache_without_running_the_model_again(tmp_path):
    vocals = _vocals(tmp_path)
    transcribe_vocals(vocals, tmp_path, model=_FakeModel([" hello there"]))

    class _MustNotRun:
        def transcribe(self, *a, **k):
            raise AssertionError("cache should have been reused")

    assert transcribe_vocals(vocals, tmp_path, model=_MustNotRun()) == "hello there"


def test_a_changed_vocal_stem_invalidates_the_cache(tmp_path):
    transcribe_vocals(_vocals(tmp_path, size=100), tmp_path, model=_FakeModel([" old words"]))

    text = transcribe_vocals(_vocals(tmp_path, size=250), tmp_path, model=_FakeModel([" new words"]))

    assert text == "new words"


def test_a_corrupt_cache_file_is_just_recomputed(tmp_path):
    (tmp_path / "transcript.json").write_text("{not json", encoding="utf-8")

    text = transcribe_vocals(_vocals(tmp_path), tmp_path, model=_FakeModel([" fresh"]))

    assert text == "fresh"


def test_missing_vocal_stem_raises_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        transcribe_vocals(tmp_path / "nope.wav", tmp_path, model=_FakeModel([]))


def test_singing_is_transcribed_without_the_speech_vad_filter(tmp_path):
    """Real finding, 2026-09-19: Whisper's default VAD is trained on speech and threw
    away most of a sung song ('Like a Prayer': 61 words heard instead of ~660, so
    correct lyrics looked 92% wrong). Sung vocals must be transcribed with it off."""
    seen = {}

    class _Recording(_FakeModel):
        def transcribe(self, path, **kwargs):
            seen.update(kwargs)
            return super().transcribe(path, **kwargs)

    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_Recording([" hello there"]))

    assert seen["vad_filter"] is False
    assert seen["temperature"] == 0.0  # no re-decoding retries on silence: ~5x faster


def _recording_model(seen):
    class _Recording(_FakeModel):
        def transcribe(self, path, **kwargs):
            seen.update(kwargs)
            return super().transcribe(path, **kwargs)

    return _Recording([" hello there"])


def test_transcription_is_forced_to_english_by_default(tmp_path):
    """Real finding, 2026-09-19: Whisper auto-detected 'Billie Jean' as Portuguese and
    transcribed it in Portuguese, so its correct English lyrics scored 19%. The lyric
    sources this app uses are English."""
    seen = {}

    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_recording_model(seen))

    assert seen["language"] == "en"


def test_the_language_can_be_overridden_and_auto_means_detect(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("LYRICVIDEO_WHISPER_LANGUAGE", "es")
    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_recording_model(seen))
    assert seen["language"] == "es"

    seen.clear()
    monkeypatch.setenv("LYRICVIDEO_WHISPER_LANGUAGE", "auto")
    (tmp_path / "other").mkdir()
    transcribe_vocals(_vocals(tmp_path), tmp_path / "other", model=_recording_model(seen))
    assert seen["language"] is None


def test_a_cache_made_for_another_language_is_not_reused(tmp_path, monkeypatch):
    vocals = _vocals(tmp_path)
    monkeypatch.setenv("LYRICVIDEO_WHISPER_LANGUAGE", "pt")
    transcribe_vocals(vocals, tmp_path, model=_FakeModel([" ola amigo"]))
    monkeypatch.delenv("LYRICVIDEO_WHISPER_LANGUAGE")

    text = transcribe_vocals(vocals, tmp_path, model=_FakeModel([" hello friend"]))

    assert text == "hello friend"


def test_the_default_model_is_medium_because_small_cannot_hear_loud_rock_vocals():
    """Real finding, 2026-09-19: on six loud rock songs `small` scored 19-62% on lyrics that
    were correct (it looped on 'wild, wild, wild' / 'oh, oh, oh'); `medium` scored 53-78%."""
    from lyricvideo.transcribe import _model_name

    assert _model_name() == "medium"


def test_the_model_can_be_overridden_by_environment_variable(monkeypatch):
    from lyricvideo.transcribe import _model_name

    monkeypatch.setenv("LYRICVIDEO_WHISPER_MODEL", "small")

    assert _model_name() == "small"


def test_a_cache_made_by_a_different_model_is_not_reused(tmp_path, monkeypatch):
    vocals = _vocals(tmp_path)
    monkeypatch.setenv("LYRICVIDEO_WHISPER_MODEL", "small")
    transcribe_vocals(vocals, tmp_path, model=_FakeModel([" from the small model"]))
    monkeypatch.delenv("LYRICVIDEO_WHISPER_MODEL")

    text = transcribe_vocals(vocals, tmp_path, model=_FakeModel([" from the medium model"]))

    assert text == "from the medium model"


def test_the_saved_transcript_segments_can_be_read_back(tmp_path):
    from lyricvideo.transcribe import load_transcript_segments

    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_FakeModel([" first line", " second line"]))

    assert load_transcript_segments(tmp_path) == [
        {"start": 0.0, "end": 4.0, "text": "first line"},
        {"start": 5.0, "end": 9.0, "text": "second line"},
    ]


def test_missing_or_corrupt_transcript_segments_read_as_empty(tmp_path):
    from lyricvideo.transcribe import load_transcript_segments

    assert load_transcript_segments(tmp_path) == []
    (tmp_path / "transcript.json").write_text("{nope", encoding="utf-8")
    assert load_transcript_segments(tmp_path) == []
