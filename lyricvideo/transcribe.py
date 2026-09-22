"""Speech recognition of the isolated vocal stem (faster-whisper), used to check
candidate lyric text against what is actually sung -- see lyric_audio_match.py
and docs/superpowers/specs/2026-09-19-verify-lyrics-against-audio-design.md.

Measured on the owner's 4-core, no-usable-GPU machine: `medium` on CPU/int8
takes ~70 s a song (`small`: ~30 s). The result is cached in
`work_dir/transcript.json` (keyed by model + vocal-stem size) so a Redo or a
resumed stage never pays for it twice."""

from __future__ import annotations

import json
import os
from pathlib import Path

# `medium`, not `small` (2026-09-19): on six loud rock songs `small` looped on
# "wild, wild, wild" and scored 19-62% on CORRECT lyrics, `medium` 53-78% (~70 s a
# song on a 4-core CPU vs ~30 s). LYRICVIDEO_WHISPER_MODEL overrides it.
# NOT `large-v3` as the default (tried 2026-09-20): its word starts run ~0.35-0.5 s EARLIER than medium's, which breaks the 0.5 s
# timing gate's calibration (the same spot-on videos scored 98% -> 85% and 97% -> 66%) and it takes ~1.9x as long. On the words that
# matter the two hear about the same (With or Without You: 72 vs 75 lyric words, 70 shared); medium's much larger raw counts are
# long "ba ba ba" / "oh oh oh" runs the gate ignores. Any model change needs the gate re-validated on the owner's judged songs first.
_DEFAULT_MODEL = "medium"
_TRANSCRIPT_FILE = "transcript.json"

_loaded_model = None
_loaded_model_name = None


def _model_name() -> str:
    return os.environ.get("LYRICVIDEO_WHISPER_MODEL", "").strip() or _DEFAULT_MODEL


def _load_model():
    """The shared WhisperModel, built once per process (the first call downloads the
    weights if they aren't cached yet). Imported here, not at module top, so the
    rest of the app still runs if the package is missing."""
    global _loaded_model, _loaded_model_name
    if _loaded_model is None or _loaded_model_name != _model_name():
        from faster_whisper import WhisperModel

        _loaded_model_name = _model_name()
        _loaded_model = WhisperModel(
            _loaded_model_name, device="cpu", compute_type="int8", cpu_threads=min(8, os.cpu_count() or 4),
        )
    return _loaded_model


def _language() -> str | None:
    """English unless LYRICVIDEO_WHISPER_LANGUAGE says otherwise ("auto" = let Whisper
    detect). Forced because auto-detect misjudged real songs: it heard 'Billie Jean' as
    Portuguese and transcribed it in Portuguese (2026-09-19)."""
    value = os.environ.get("LYRICVIDEO_WHISPER_LANGUAGE", "en").strip().lower()
    return None if value in ("", "auto") else value


def _read_cache(cache_path: Path, vocals_size: int, language: str | None) -> str | None:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            data["model"] == _model_name()
            and data["vocals_bytes"] == vocals_size
            and data["language_requested"] == (language or "auto")
            and data.get("word_timestamps") is True   # caches from before word timings existed are redone
        ):
            return str(data["text"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def transcribe_vocals(vocals_path: Path, work_dir: Path, model=None) -> str:
    """The words heard in `vocals_path`, as one lowercase-insensitive string. `model` is
    injectable (tests pass a fake with the same .transcribe() shape)."""
    vocals_path = Path(vocals_path)
    if not vocals_path.exists():
        raise FileNotFoundError(f"Vocal stem not found: {vocals_path}")
    vocals_size = vocals_path.stat().st_size
    cache_path = Path(work_dir) / _TRANSCRIPT_FILE

    language = _language()
    cached = _read_cache(cache_path, vocals_size, language)
    if cached is not None:
        return cached

    model = model or _load_model()
    segments, info = model.transcribe(
        str(vocals_path), language=language, vad_filter=False, temperature=0.0,
        condition_on_previous_text=False, beam_size=1, word_timestamps=True,
    )
    segments = [s for s in segments if s.text.strip()]
    text = " ".join(s.text.strip() for s in segments)
    words = [
        {"word": w.word.strip(), "start": float(w.start), "end": float(w.end)}
        for s in segments for w in (getattr(s, "words", None) or []) if w.word.strip()
    ]
    cache_path.write_text(
        json.dumps({
            "model": _model_name(),
            "vocals_bytes": vocals_size,
            "language_requested": language or "auto",
            "language": getattr(info, "language", ""),
            "text": text,
            "word_timestamps": True,
            "words": words,
            "segments": [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments],
        }),
        encoding="utf-8",
    )
    return text


def load_transcript_segments(work_dir: Path) -> list[dict]:
    """The cached transcript's [{start, end, text}] segments, or [] if there is none (or it
    is unreadable) -- used by the lyric repair step, which shows Claude what was heard."""
    try:
        data = json.loads((Path(work_dir) / _TRANSCRIPT_FILE).read_text(encoding="utf-8"))
        return [
            {"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"])}
            for s in data["segments"]
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def load_transcript_text(work_dir: Path) -> str | None:
    """The cached transcript's whole recognized text, or None when there is no cache yet (or it is unreadable) --
    lets a caller (the GUI's Whisper Text review button) tell "already have it" from "need to transcribe" without
    running Whisper just to check."""
    try:
        data = json.loads((Path(work_dir) / _TRANSCRIPT_FILE).read_text(encoding="utf-8"))
        return str(data["text"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_transcript_words(work_dir: Path) -> list[dict]:
    """The cached transcript's [{word, start, end}] word timings, or [] if there are none (older cache,
    unreadable file) -- the anchors the lyric aligner uses to keep every line inside its own stretch of the
    song (see anchors.py)."""
    try:
        data = json.loads((Path(work_dir) / _TRANSCRIPT_FILE).read_text(encoding="utf-8"))
        return [
            {"word": str(w["word"]), "start": float(w["start"]), "end": float(w["end"])}
            for w in data.get("words", [])
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []
