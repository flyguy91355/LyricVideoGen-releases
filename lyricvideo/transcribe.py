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
        condition_on_previous_text=False, beam_size=1,
    )
    segments = [s for s in segments if s.text.strip()]
    text = " ".join(s.text.strip() for s in segments)
    cache_path.write_text(
        json.dumps({
            "model": _model_name(),
            "vocals_bytes": vocals_size,
            "language_requested": language or "auto",
            "language": getattr(info, "language", ""),
            "text": text,
            "segments": [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments],
        }),
        encoding="utf-8",
    )
    return text
