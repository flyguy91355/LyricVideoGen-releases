"""The song's audio and its stems (issue #7 review, wave 2): a replaced source file is picked up instead of the stale copy
in the song's folder (F063); stems that only disagree with a misread song_info.json duration are accepted when the audio's
decoded length agrees with them (VBR-BACKSTOP); a Redo that dies after its new timing never leaves the old video as the
song's current one (F064)."""

import json
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile

from lyricvideo.models import load_song
from lyricvideo.pipeline import (
    HELD_MARKER, REDO_STOPPED_REASON, backup_song_outputs, held_before_video, list_flagged_songs, list_pending_uploads,
    review_concern, run_pipeline, song_video_path,
)
from tests.test_pipeline import _patch_common


def _record_separations(monkeypatch, tmp_path):
    seen = []

    def fake_separate(audio_path, out_dir, **kwargs):
        seen.append(Path(audio_path).read_bytes())
        return tmp_path / "vocals.wav"

    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", fake_separate)
    return seen


def _write_source(path: Path, data: bytes, mtime: float) -> None:
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


# --- F063: a replaced source file ------------------------------------------------------------------------------------------

def test_a_replaced_source_file_is_copied_again_and_its_old_stems_and_transcript_forgotten(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    separated = _record_separations(monkeypatch, tmp_path)
    source = tmp_path / "staging" / "Some Song.m4a"
    source.parent.mkdir()
    work_dir = tmp_path / "work"
    _write_source(source, b"DAMAGED", 1_000_000)
    run_pipeline(source, work_dir)
    stems = work_dir / "htdemucs" / "Some Song"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"old stem")
    (work_dir / "transcript.json").write_text("{}", encoding="utf-8")

    _write_source(source, b"GOOD-FULL-LENGTH", 2_000_000)                     # the owner puts a good copy in its place
    run_pipeline(source, work_dir)

    assert separated == [b"DAMAGED", b"GOOD-FULL-LENGTH"]
    assert (work_dir / "Some Song.m4a").read_bytes() == b"GOOD-FULL-LENGTH"
    assert not stems.exists() and not (work_dir / "transcript.json").exists()


def test_a_same_size_replacement_with_new_content_is_picked_up_too(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    separated = _record_separations(monkeypatch, tmp_path)
    source = tmp_path / "Some Song.mp3"
    work_dir = tmp_path / "work"
    _write_source(source, b"AAAA", 1_000_000)
    run_pipeline(source, work_dir)
    _write_source(source, b"BBBB", 2_000_000)

    run_pipeline(source, work_dir)

    assert separated == [b"AAAA", b"BBBB"]


def test_an_unchanged_or_merely_touched_source_keeps_the_copy_and_its_stems(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    source = tmp_path / "Some Song.mp3"
    work_dir = tmp_path / "work"
    _write_source(source, b"SAME AUDIO", 1_000_000)
    run_pipeline(source, work_dir)
    stems = work_dir / "htdemucs" / "Some Song"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"stem")
    monkeypatch.setattr("lyricvideo.pipeline.shutil.copy2", lambda *a, **k: pytest.fail("must not copy again"))

    run_pipeline(source, work_dir, start_stage="fetch_lyrics")                # unchanged
    os.utime(source, (3_000_000, 3_000_000))                                  # touched: same bytes, new time stamp
    run_pipeline(source, work_dir, start_stage="fetch_lyrics")

    assert (stems / "vocals.wav").read_bytes() == b"stem"


def test_a_redo_reading_the_copy_itself_never_refreshes_it(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    source = tmp_path / "Some Song.mp3"
    work_dir = tmp_path / "work"
    _write_source(source, b"AUDIO", 1_000_000)
    run_pipeline(source, work_dir)
    monkeypatch.setattr("lyricvideo.pipeline.shutil.copy2", lambda *a, **k: pytest.fail("must not copy"))

    run_pipeline(work_dir / "Some Song.mp3", work_dir, start_stage="fetch_lyrics")


def test_a_resume_past_align_keeps_the_audio_its_timing_was_made_from(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    source = tmp_path / "Some Song.mp3"
    work_dir = tmp_path / "work"
    _write_source(source, b"TIMED AUDIO", 1_000_000)
    run_pipeline(source, work_dir)
    _write_source(source, b"A DIFFERENT FILE", 2_000_000)
    rendered_from = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.assemble_video", lambda lines, chords, images, audio, *a, **k: rendered_from.append(Path(audio).read_bytes()),
    )

    run_pipeline(source, work_dir, start_stage="render")

    assert rendered_from == [b"TIMED AUDIO"]


# --- VBR-BACKSTOP: stems that only disagree with a misread duration --------------------------------------------------------------

def _write_stems(work_dir: Path, stem: str, seconds: float) -> Path:
    folder = work_dir / "htdemucs" / stem
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("vocals.wav", "no_vocals.wav"):
        soundfile.write(str(folder / name), np.zeros(int(seconds * 1000), dtype="float32"), 1000)
    return folder


def _info(work_dir: Path, duration: float) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "Test Song", "artist": "a", "duration": duration, "alt_titles": []}), encoding="utf-8",
    )


def test_complete_stems_are_kept_when_the_decoded_audio_agrees_with_them_and_the_duration_is_corrected(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: pytest.fail("the stems are complete"))
    monkeypatch.setattr("lyricvideo.identify.decoded_duration", lambda path: 20.0)
    monkeypatch.setattr("lyricvideo.pipeline._length_may_be_a_header_guess", lambda path: True)
    work_dir = tmp_path / "work"
    _info(work_dir, 60.0)                                  # a header-less VBR MP3's misread length
    _write_stems(work_dir, "audio", 20.0)

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics", end_stage="align")

    assert json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))["duration"] == 20.0


def test_truly_truncated_stems_are_still_separated_again(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    separated = []
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: separated.append(1) or tmp_path / "vocals.wav")
    monkeypatch.setattr("lyricvideo.identify.decoded_duration", lambda path: 60.0)      # the audio really is 60 s
    monkeypatch.setattr("lyricvideo.pipeline._length_may_be_a_header_guess", lambda path: True)
    work_dir = tmp_path / "work"
    _info(work_dir, 60.0)
    _write_stems(work_dir, "audio", 20.0)

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics", end_stage="align")

    assert separated == [1]
    assert json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))["duration"] == 60.0


def test_a_fresh_separation_rejected_only_for_the_misread_duration_is_accepted(tmp_path, monkeypatch):
    from lyricvideo.separate import SeparationError

    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    def demucs(audio_path, out_dir, expected_seconds=None, **kwargs):
        _write_stems(Path(out_dir), "audio", 20.0)
        raise SeparationError(f"Demucs produced truncated stems (20 s of {expected_seconds:.0f} s).")

    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", demucs)
    monkeypatch.setattr(
        "lyricvideo.pipeline.extract_metadata",
        lambda path: type("Info", (), {"title": "Test Song", "artist": "a", "duration": 60.0, "alt_titles": []})(),
    )
    monkeypatch.setattr("lyricvideo.identify.decoded_duration", lambda path: 20.0)
    monkeypatch.setattr("lyricvideo.pipeline._length_may_be_a_header_guess", lambda path: True)

    run_pipeline(Path("audio.mp3"), work_dir, end_stage="separate")

    assert json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))["duration"] == 20.0


def test_a_fresh_separation_that_really_is_truncated_still_fails(tmp_path, monkeypatch):
    from lyricvideo.separate import SeparationError

    _patch_common(monkeypatch, tmp_path)

    def demucs(audio_path, out_dir, expected_seconds=None, **kwargs):
        _write_stems(Path(out_dir), "audio", 20.0)
        raise SeparationError("Demucs produced truncated stems.")

    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", demucs)
    monkeypatch.setattr("lyricvideo.identify.decoded_duration", lambda path: 0.0)       # cannot be decoded
    monkeypatch.setattr("lyricvideo.pipeline._length_may_be_a_header_guess", lambda path: True)

    with pytest.raises(SeparationError):
        run_pipeline(Path("audio.mp3"), tmp_path / "work")


def test_an_incomplete_download_with_a_real_header_length_is_never_accepted_as_complete(tmp_path, monkeypatch):
    """The 'Ironic' case: a cut-off download keeps its full-length (Xing) header but decodes to only the part that arrived,
    so its decoded length AGREES with its truncated stems. That must still fail -- only a header-less VBR MP3's recorded
    length can be a guess worth second-guessing."""
    from lyricvideo.separate import SeparationError

    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    def demucs(audio_path, out_dir, expected_seconds=None, **kwargs):
        _write_stems(Path(out_dir), "audio", 20.0)
        raise SeparationError("Demucs produced truncated stems.")

    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", demucs)
    monkeypatch.setattr(
        "lyricvideo.pipeline.extract_metadata",
        lambda path: type("Info", (), {"title": "Test Song", "artist": "a", "duration": 60.0, "alt_titles": []})(),
    )
    monkeypatch.setattr("lyricvideo.identify.decoded_duration", lambda path: 20.0)     # only 20 s arrived
    monkeypatch.setattr("lyricvideo.pipeline._length_may_be_a_header_guess", lambda path: False)

    with pytest.raises(SeparationError):
        run_pipeline(Path("audio.mp3"), work_dir, end_stage="separate")
    assert json.loads((work_dir / "song_info.json").read_text(encoding="utf-8"))["duration"] == 60.0


def test_only_a_headerless_mp3s_length_counts_as_a_guess(tmp_path):
    import subprocess

    from lyricvideo.audio_decode import FFmpegNotFound, find_ffmpeg
    from lyricvideo.pipeline import _length_may_be_a_header_guess

    try:
        ffmpeg = find_ffmpeg()
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except (FFmpegNotFound, OSError):
        pytest.skip("no FFmpeg")
    if "libmp3lame" not in encoders:
        pytest.skip("this FFmpeg build has no libmp3lame encoder")
    source = ["-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=r=22050:color=pink:d=3", "-c:a", "libmp3lame", "-q:a", "0"]
    headerless, with_header = tmp_path / "headerless.mp3", tmp_path / "with_header.mp3"
    subprocess.run([ffmpeg, *source, "-write_xing", "0", str(headerless)], check=True)
    subprocess.run([ffmpeg, *source, str(with_header)], check=True)
    wav = tmp_path / "song.wav"
    soundfile.write(str(wav), np.zeros(1000, dtype="float32"), 1000)

    assert _length_may_be_a_header_guess(headerless)
    assert not _length_may_be_a_header_guess(with_header)
    assert not _length_may_be_a_header_guess(wav)
    assert not _length_may_be_a_header_guess(tmp_path / "missing.mp3")


# --- F064: a Redo that dies after its new timing ------------------------------------------------------------------------------

def test_a_redo_that_stops_after_its_new_timing_sets_the_old_video_aside_and_holds_the_song(tmp_path, monkeypatch):
    from lyricvideo.gui import _stage_to_resume

    _patch_common(monkeypatch, tmp_path)
    work_root = tmp_path / "root"
    work_dir = work_root / "test-song"
    run_pipeline(Path("audio.mp3"), work_dir)
    (work_dir / "test-song.mp4").write_bytes(b"OLD VIDEO")
    assert list_pending_uploads(work_root) == ["test-song"]

    backup_song_outputs(work_dir, "test-song")
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", _replicate_down)
    with pytest.raises(RuntimeError, match="Replicate 502"):
        run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics")

    assert song_video_path(work_dir) is None
    assert (work_dir / "test-song.previous.mp4").read_bytes() == b"OLD VIDEO"
    assert held_before_video(work_dir)
    assert list_pending_uploads(work_root) == [] and list_flagged_songs(work_root) == ["test-song"]
    assert review_concern(work_dir) == REDO_STOPPED_REASON                    # the Flagged row says why it is held

    rendered = []
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: Path("x"))
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: rendered.append(1))
    run_pipeline(Path("audio.mp3"), work_dir, start_stage=_stage_to_resume(work_dir))     # Render Anyway

    assert rendered == [1] and not (work_dir / HELD_MARKER).exists()


def _replicate_down(*args, **kwargs):
    raise RuntimeError("Replicate 502")


def test_a_first_run_that_stops_after_align_writes_no_hold(tmp_path, monkeypatch):
    """No earlier video, nothing to set aside: a brand-new song that dies mid-run is simply unfinished, as before."""
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", _replicate_down)
    work_dir = tmp_path / "work"

    with pytest.raises(RuntimeError):
        run_pipeline(Path("audio.mp3"), work_dir)

    assert not held_before_video(work_dir)
    assert load_song(work_dir / "lyrics_timed.json").lines
