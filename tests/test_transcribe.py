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


def test_the_default_model_is_large_v3_and_an_environment_setting_still_goes_back_to_medium(monkeypatch):
    """Owner, 2026-09-20: a slower build is fine if it hears more of the singing (`small` looped on loud rock
    vocals on 2026-09-19; `medium` scored 53-78% on correct lyrics). LYRICVIDEO_WHISPER_MODEL=medium reverts."""
    from lyricvideo.transcribe import _model_name

    monkeypatch.delenv("LYRICVIDEO_WHISPER_MODEL", raising=False)
    assert _model_name() == "large-v3"
    monkeypatch.setenv("LYRICVIDEO_WHISPER_MODEL", "medium")
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


# --- word-level timestamps (anchors for lyric alignment, 2026-09-19) --------------------------------

class _WordModel(_FakeModel):
    """A fake WhisperModel whose segments carry word timings, like word_timestamps=True."""

    def transcribe(self, path, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        words = [SimpleNamespace(word=" hello", start=1.0, end=1.4), SimpleNamespace(word=" there", start=1.5, end=2.0)]
        segs = [SimpleNamespace(start=0.9, end=2.1, text=" hello there", words=words)]
        return iter(segs), SimpleNamespace(language="en", language_probability=0.9)


def test_word_timestamps_are_requested_and_saved(tmp_path):
    from lyricvideo.transcribe import load_transcript_words

    model = _WordModel([])
    transcribe_vocals(_vocals(tmp_path), tmp_path, model=model)

    assert model.kwargs["word_timestamps"] is True
    assert load_transcript_words(tmp_path) == [
        {"word": "hello", "start": 1.0, "end": 1.4},
        {"word": "there", "start": 1.5, "end": 2.0},
    ]


def test_a_cache_without_word_timings_is_not_reused(tmp_path):
    """Caches written before 2026-09-19 have segments only; the aligner needs the words, so they are redone."""
    vocals = _vocals(tmp_path)
    transcribe_vocals(vocals, tmp_path, model=_FakeModel([" old segments only"]))
    cache = json.loads((tmp_path / "transcript.json").read_text())
    cache.pop("words", None)
    cache.pop("word_timestamps", None)
    (tmp_path / "transcript.json").write_text(json.dumps(cache))

    model = _WordModel([])
    transcribe_vocals(vocals, tmp_path, model=model)

    assert model.calls == 1


def test_missing_word_timings_read_as_empty(tmp_path):
    from lyricvideo.transcribe import load_transcript_words

    assert load_transcript_words(tmp_path) == []
    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_FakeModel([" no word timings here"]))
    assert load_transcript_words(tmp_path) == []


# --- the large model is used only once it is on this computer (2026-09-20: its host was unreachable from the owner's network) ---

def test_the_default_large_model_falls_back_to_medium_until_it_has_been_downloaded(monkeypatch, capsys):
    from lyricvideo import transcribe
    monkeypatch.delenv("LYRICVIDEO_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(transcribe, "_is_downloaded", lambda name: name != "large-v3")

    assert transcribe._effective_model_name() == "medium"
    assert "large-v3" in capsys.readouterr().err                  # says why, and how to get it


def test_the_default_large_model_is_used_as_soon_as_it_is_on_disk(monkeypatch):
    from lyricvideo import transcribe
    monkeypatch.delenv("LYRICVIDEO_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(transcribe, "_is_downloaded", lambda name: True)

    assert transcribe._effective_model_name() == "large-v3"


def test_a_model_the_owner_chose_explicitly_is_never_swapped_for_medium(monkeypatch):
    from lyricvideo import transcribe
    monkeypatch.setenv("LYRICVIDEO_WHISPER_MODEL", "small")
    monkeypatch.setattr(transcribe, "_is_downloaded", lambda name: False)

    assert transcribe._effective_model_name() == "small"


def test_a_transcript_is_labelled_with_the_model_that_really_made_it(tmp_path, monkeypatch):
    import json
    from lyricvideo import transcribe
    monkeypatch.delenv("LYRICVIDEO_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(transcribe, "_is_downloaded", lambda name: False)      # large-v3 missing: medium does the work

    transcribe_vocals(_vocals(tmp_path), tmp_path, model=_recording_model({}))

    assert json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))["model"] == "medium"
