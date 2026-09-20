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
import sys
from pathlib import Path

# `large-v3` (owner, 2026-09-20: a slower build is fine if it hears more of the singing; about twice
# medium's time on this 4-core CPU, ~3 min for a 4-minute song). Before that `medium`, not `small`
# (2026-09-19): `small` looped on "wild, wild, wild" and scored 19-62% on CORRECT lyrics, `medium` 53-78%.
# LYRICVIDEO_WHISPER_MODEL (e.g. in .env) overrides it -- `medium` goes back with no code change. A saved
# transcript.json is tied to the model that made it, so a Redo/Batch re-transcribes; the timing gate just reads it.
_DEFAULT_MODEL = "large-v3"
_TRANSCRIPT_FILE = "transcript.json"

_loaded_model = None
_loaded_model_name = None


_FALLBACK_MODEL = "medium"


def _model_name() -> str:
    return os.environ.get("LYRICVIDEO_WHISPER_MODEL", "").strip() or _DEFAULT_MODEL


def _is_downloaded(name: str) -> bool:
    """True when the weights of `name` are already on this computer (no network involved). Anything this cannot
    tell (an unknown name, a folder path) counts as downloaded, so the library decides exactly as before."""
    try:
        from faster_whisper.utils import _MODELS
        from huggingface_hub import try_to_load_from_cache

        repo = _MODELS.get(name)
        return repo is None or isinstance(try_to_load_from_cache(repo, "model.bin"), str)
    except Exception:
        return True


def _effective_model_name(warn: bool = True) -> str:
    """The model actually used. The DEFAULT model waits until its weights are on this computer (2026-09-20: large-v3's
    file host was unreachable from the owner's network, and a download that never starts would hang every Redo/Batch), so
    until then `medium` is used and the reason is printed; a model the owner chose in LYRICVIDEO_WHISPER_MODEL is never
    swapped. The transcript cache records this name, so a medium transcript is redone once large-v3 arrives."""
    name = _model_name()
    if os.environ.get("LYRICVIDEO_WHISPER_MODEL", "").strip() or _is_downloaded(name):
        return name
    if warn:
        print(
            f"WARNING: the Whisper model '{name}' is not downloaded on this computer yet, so '{_FALLBACK_MODEL}' is used for "
            f"now. Download it once (about 3 GB) with: .venv/bin/python -c \"from faster_whisper.utils import download_model; "
            f"download_model('{name}')\" -- it is picked up automatically after that.", file=sys.stderr,
        )
    return _FALLBACK_MODEL


def _load_model():
    """The shared WhisperModel, built once per process (the first call downloads the
    weights if they aren't cached yet). Imported here, not at module top, so the
    rest of the app still runs if the package is missing."""
    global _loaded_model, _loaded_model_name
    if _loaded_model is None or _loaded_model_name != _effective_model_name(warn=False):
        from faster_whisper import WhisperModel

        _loaded_model_name = _effective_model_name()
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
            data["model"] == _effective_model_name(warn=False)
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
            "model": _effective_model_name(warn=False),
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
