import json
from datetime import datetime
from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, load_song, save_song
from lyricvideo.pipeline import (
    run_pipeline,
    list_flagged_songs,
    list_redoable_songs,
    list_pending_uploads,
    list_rendered_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
    ordered_unique_chords,
    song_end_time,
    song_video_path,
)
from lyricvideo.settings import Settings


def _patch_common(monkeypatch, tmp_path):
    """Shared stubs for every run_pipeline test below: no real Demucs, alignment,
    Claude, or Replicate calls -- exercising the stage wiring, not the ported
    modules themselves (those have their own dedicated tests)."""
    import torch

    monkeypatch.setattr(
        "lyricvideo.pipeline.extract_metadata",
        lambda audio_path: type(
            "Info", (), {"title": "Test Song", "artist": "Test Artist", "duration": 10.0, "alt_titles": []}
        )(),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified",
        lambda *a, **k: (["hello there", "my friend"], "sidecar", ""),
    )
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: tmp_path / "vocals.wav")
    # Never start a real Whisper from a pipeline-wiring test (the audio-check tests below override this).
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: "hello there my friend")
    monkeypatch.setattr("lyricvideo.pipeline.torchaudio.load", lambda path: (torch.zeros(1, 16000 * 10), 16000))
    monkeypatch.setattr("lyricvideo.pipeline.align_words", lambda vocals_path, words: [
        (float(i), float(i) + 0.4) for i in range(len(words))
    ])
    monkeypatch.setattr(
        "lyricvideo.pipeline.detect_chords",
        lambda instrumental_stem_path, **kwargs: ChordTrack(
            events=[ChordEvent(0.0, 10.0, "C")], key="C major", bpm=100.0,
        ),
    )
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: "a fake song gist")
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: Path("x"))
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")


def test_run_pipeline_reports_progress_per_stage(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    reported = []
    run_pipeline(Path("audio.mp3"), work_dir, progress_callback=reported.append)

    assert reported == [
        "identify", "separate", "fetch_lyrics", "align", "detect_chords", "images", "render", "done",
    ]


def test_run_pipeline_uses_auto_identified_title_when_none_given(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    out_path = run_pipeline(Path("audio.mp3"), work_dir)

    assert out_path.name == "test-song.mp4"


def test_run_pipeline_title_override_replaces_identified_title(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    out_path = run_pipeline(Path("audio.mp3"), work_dir, title="My Custom Title")

    assert out_path.name == "my-custom-title.mp4"


def test_run_pipeline_only_needs_the_audio_file_no_pdf_or_chords_text_argument(tmp_path, monkeypatch):
    """The whole point of this merge: run_pipeline() takes no tab/chords-text
    argument at all anymore -- calling it with just (audio_path, work_dir) must
    complete without raising, producing a path inside work_dir."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    out_path = run_pipeline(Path("audio.mp3"), work_dir)

    assert out_path.parent == work_dir


def test_run_pipeline_carries_lyrics_source_and_concern_onto_the_song(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified",
        lambda *a, **k: (["hello there"], "Genius", "This looks like a different edition of the song."),
    )
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    song = load_song(work_dir / "lyrics_timed.json")
    assert song.lyrics_source == "Genius"
    assert song.lyrics_accuracy_concern == "This looks like a different edition of the song."


def test_run_pipeline_align_stage_tolerates_a_legacy_plain_list_lyric_lines_json(tmp_path, monkeypatch):
    """lyric_lines.json written before fetch_lyric_lines_verified() existed
    is a bare list of strings, not {"lines":..., "source":..., "concern":...}
    -- a --stage align resume against one of these must not crash, and the
    resulting Song just has no source/concern to report."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "t", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    (work_dir / "lyric_lines.json").write_text(json.dumps(["hello there"]), encoding="utf-8")

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="align")

    song = load_song(work_dir / "lyrics_timed.json")
    assert song.lyrics_source == ""
    assert song.lyrics_accuracy_concern == ""


def test_run_pipeline_copies_the_source_audio_into_work_dir(tmp_path, monkeypatch):
    """So Redo has a reliable local copy to fall back on even after a batch
    run's original staging-folder file is gone -- see load_redo_inputs()."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"fake audio bytes")

    run_pipeline(audio_path, work_dir)

    assert (work_dir / "song.mp3").read_bytes() == b"fake audio bytes"


def test_run_pipeline_fetches_lyrics_against_the_work_dir_copy_not_the_original(tmp_path, monkeypatch):
    """Real bug, 2026-09-15: fetch_lyric_lines_verified()'s sidecar .lrc/.txt
    lookup checks next to whatever audio_path it's called with. run_pipeline()
    copies the source audio into work_dir (the test above) but kept calling
    every later stage, including the lyrics fetch, with the ORIGINAL
    external audio_path -- so a sidecar file the owner dropped next to the
    work_dir copy (following the error message's own filename hint) was
    never actually found; only a sidecar next to the original, possibly
    transient, external location ever counted. Every stage must receive
    the work_dir copy from here on."""
    _patch_common(monkeypatch, tmp_path)
    seen_audio_paths = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified",
        lambda audio_path, *a, **k: seen_audio_paths.append(audio_path) or (["hello there"], "sidecar", ""),
    )
    work_dir = tmp_path / "work"
    original_dir = tmp_path / "external" / "staging"
    original_dir.mkdir(parents=True)
    audio_path = original_dir / "song.mp3"
    audio_path.write_bytes(b"fake audio bytes")

    run_pipeline(audio_path, work_dir)

    assert seen_audio_paths == [work_dir / "song.mp3"]


def test_run_pipeline_skips_earlier_stages(tmp_path, monkeypatch):
    calls = []
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image",
        lambda *a, **k: calls.append("images") or Path("x"),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.assemble_video",
        lambda *a, **k: calls.append("render"),
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "t", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    seeded_line = LyricLine(
        words=[Word(word="hi", start_time=0.0, end_time=1.0)], start_time=0.0, end_time=1.0,
    )
    save_song(
        Song(title="t", audio_path="a.mp3", lines=[seeded_line],
             chord_track=ChordTrack(events=[ChordEvent(0.0, 1.0, "C")])),
        work_dir / "lyrics_timed.json",
    )

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images")

    assert calls == ["images", "render"]


def test_run_pipeline_resuming_past_identify_uses_saved_song_info(tmp_path, monkeypatch):
    """A redo resuming at 'fetch_lyrics' or later never re-runs identify -- it must
    read the original run's title/artist back from song_info.json instead."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "Original Title", "artist": "Original Artist", "duration": 10.0, "alt_titles": []}),
        encoding="utf-8",
    )

    out_path = run_pipeline(Path("songs/original.mp3"), work_dir, start_stage="fetch_lyrics")

    assert out_path.name == "original-title.mp4"


def test_run_pipeline_resuming_past_identify_bootstraps_missing_song_info(tmp_path, monkeypatch):
    """Real bug found live 2026-09-09: a Redo of a song created BEFORE this merge
    (no song_info.json was ever written for it, since the identify stage didn't
    exist yet) must not crash trying to read a file that was never there --
    it should fall back to running identify anyway, honoring any title override."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # Deliberately NOT writing song_info.json here, unlike the "resuming past
    # identify" test above -- this simulates a pre-merge song's work dir.

    out_path = run_pipeline(
        Path("songs/original.mp3"), work_dir, title="Legacy Song Title", start_stage="fetch_lyrics",
    )

    assert out_path.name == "legacy-song-title.mp4"
    assert (work_dir / "song_info.json").exists()  # bootstrapped for next time


def test_run_pipeline_calls_substitute_fallback_images_with_every_generated_path(tmp_path, monkeypatch):
    """A flat placeholder color left in the final video was a real live
    complaint -- run_pipeline must hand every image it generated this run
    (lines + instrumental captions, in order) to substitute_fallback_images
    so any fallback gets a real neighboring image substituted in instead."""
    _patch_common(monkeypatch, tmp_path)
    counter = {"n": 0}

    def _fake_generate(*a, **k):
        counter["n"] += 1
        return Path(f"generated-{counter['n']}.png")

    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", _fake_generate)
    substitute_calls = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.substitute_fallback_images",
        lambda paths: substitute_calls.append(paths),
    )
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    assert len(substitute_calls) == 1
    assert len(substitute_calls[0]) == counter["n"]
    assert substitute_calls[0] == [Path(f"generated-{i}.png") for i in range(1, counter["n"] + 1)]


def test_run_pipeline_detect_chords_writes_chord_track_onto_song(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    from lyricvideo.models import load_song

    saved = load_song(work_dir / "lyrics_timed.json")
    assert saved.chord_track.key == "C major"
    assert saved.chord_track.events[0].label == "C"


def test_song_end_time_is_the_later_of_last_chord_and_last_line():
    song = Song(
        title="t", audio_path="a.mp3",
        lines=[LyricLine(words=[Word(word="hi", start_time=0.0, end_time=12.5)], start_time=0.0, end_time=12.5)],
        chord_track=ChordTrack(events=[ChordEvent(0.0, 10.0, "C")]),
    )
    assert song_end_time(song) == 12.5

    song.chord_track = ChordTrack(events=[ChordEvent(0.0, 20.0, "C")])
    assert song_end_time(song) == 20.0


def test_song_end_time_is_zero_for_an_empty_song():
    assert song_end_time(Song(title="t", audio_path="a.mp3")) == 0.0


def test_run_pipeline_images_stage_generates_every_instrumental_caption_the_timeline_needs(tmp_path, monkeypatch):
    """The images stage must generate an image for every instrumental key the
    render's own image timeline can look up -- including a chord that only
    OVERLAPS a gap's edge (its midpoint inside a sung line), which the old
    midpoint rule skipped, leaving that stretch a flat placeholder color."""
    _patch_common(monkeypatch, tmp_path)
    # Sung 0-2s; chords C 0-3 (midpoint 1.5 is inside the line, but 2-3 is a
    # real instrumental stretch) and G 3-10.
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified", lambda *a, **k: (["hello there"], "sidecar", ""),
    )
    monkeypatch.setattr("lyricvideo.pipeline.align_words", lambda vocals_path, words: [(0.0, 1.0), (1.0, 2.0)])
    monkeypatch.setattr(
        "lyricvideo.pipeline.detect_chords",
        lambda instrumental_stem_path, **kwargs: ChordTrack(
            events=[ChordEvent(0.0, 3.0, "C"), ChordEvent(3.0, 10.0, "G")], key="C major", bpm=100.0,
        ),
    )
    captions = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image",
        lambda client, token, gist, text, images_dir, **k: captions.append(text) or Path("x"),
    )

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert captions == ["hello there", "[Instrumental — chord: C]", "[Instrumental — chord: G]"]


def test_run_pipeline_resuming_past_separate_reruns_demucs_when_stems_are_missing(tmp_path, monkeypatch):
    """A resume at fetch_lyrics/align/detect_chords reads the Demucs stems; if
    they aren't on disk (htdemucs/ deleted, or never produced here) the run
    must re-separate rather than crash on the missing file."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "t", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    reported = []

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics", progress_callback=reported.append)

    assert reported[:2] == ["separate", "fetch_lyrics"]


def test_run_pipeline_resuming_past_separate_skips_demucs_when_stems_exist(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    stems = work_dir / "htdemucs" / "audio"
    stems.mkdir(parents=True)
    import numpy as np
    import soundfile

    for name in ("vocals.wav", "no_vocals.wav"):        # real audio of the song's length (10 s)
        soundfile.write(str(stems / name), np.zeros(10 * 8000), 8000)
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "t", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    reported = []

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics", progress_callback=reported.append)

    assert "separate" not in reported


def test_run_pipeline_resuming_at_images_never_needs_the_stems(tmp_path, monkeypatch):
    """images/render never read the stems, so a resume there must not trigger
    a (slow) re-separation just because htdemucs/ is gone."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(
        json.dumps({"title": "t", "artist": "a", "duration": 10.0, "alt_titles": []}), encoding="utf-8",
    )
    save_song(Song(title="t", audio_path="a.mp3"), work_dir / "lyrics_timed.json")
    reported = []

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="images", progress_callback=reported.append)

    assert "separate" not in reported


def test_run_pipeline_images_stage_explains_a_missing_replicate_token(tmp_path, monkeypatch):
    import pytest

    _patch_common(monkeypatch, tmp_path)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="REPLICATE_API_TOKEN"):
        run_pipeline(Path("audio.mp3"), tmp_path / "work")


def test_ordered_unique_chords_preserves_first_seen_order():
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "G"), ChordEvent(2.0, 4.0, "D"),
        ChordEvent(4.0, 6.0, "Am"), ChordEvent(6.0, 8.0, "G"),
    ])

    assert ordered_unique_chords(chord_track) == ["G", "D", "Am"]


def test_ordered_unique_chords_excludes_no_chord_label():
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "N"), ChordEvent(2.0, 4.0, "C"), ChordEvent(4.0, 6.0, "N"),
    ])

    assert ordered_unique_chords(chord_track) == ["C"]


def test_ordered_unique_chords_empty_track_returns_empty_list():
    assert ordered_unique_chords(ChordTrack()) == []


def test_list_redoable_songs_finds_folders_with_a_completed_run(tmp_path):
    work_root = tmp_path / "work"
    (work_root / "angie-rolling-stones").mkdir(parents=True)
    save_song(
        Song(title="Angie", audio_path="a.mp3"),
        work_root / "angie-rolling-stones" / "lyrics_timed.json",
    )
    (work_root / "still-in-progress").mkdir(parents=True)

    assert list_redoable_songs(work_root) == ["angie-rolling-stones"]


def test_list_redoable_songs_returns_empty_list_when_work_dir_missing(tmp_path):
    assert list_redoable_songs(tmp_path / "does-not-exist") == []


def test_list_redoable_songs_sorted_alphabetically(tmp_path):
    work_root = tmp_path / "work"
    for slug in ["wish-you-were-here", "angie-rolling-stones", "eye-in-the-sky"]:
        (work_root / slug).mkdir(parents=True)
        save_song(Song(title=slug, audio_path="a.mp3"), work_root / slug / "lyrics_timed.json")

    assert list_redoable_songs(work_root) == [
        "angie-rolling-stones", "eye-in-the-sky", "wish-you-were-here",
    ]


def test_load_redo_inputs_reads_audio_path_and_title(tmp_path):
    song_dir = tmp_path / "angie-rolling-stones"
    song_dir.mkdir()
    save_song(
        Song(title="Angie", audio_path="/home/doug/songs/angie.mp3"),
        song_dir / "lyrics_timed.json",
    )

    audio_path, title = load_redo_inputs(song_dir)

    assert audio_path == Path("/home/doug/songs/angie.mp3")
    assert title == "Angie"


def test_load_redo_inputs_prefers_a_local_copy_in_work_dir_over_the_original_path(tmp_path):
    """A batch-run song's original audio_path can point into a staging
    folder the owner has since emptied -- run_pipeline() now copies the
    source audio into the song's own work_dir precisely so Redo has
    something reliable to fall back on. This is that fallback taking
    priority once the local copy exists."""
    song_dir = tmp_path / "angie-rolling-stones"
    song_dir.mkdir()
    save_song(
        Song(title="Angie", audio_path="/batch/staging/angie.mp3"),
        song_dir / "lyrics_timed.json",
    )
    (song_dir / "angie.mp3").write_bytes(b"local copy")

    audio_path, title = load_redo_inputs(song_dir)

    assert audio_path == song_dir / "angie.mp3"
    assert title == "Angie"


def test_song_video_path_returns_the_rendered_mp4(tmp_path):
    song_dir = tmp_path / "angie-rolling-stones"
    song_dir.mkdir()
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")
    (song_dir / "angie.mp4").write_bytes(b"video")

    assert song_video_path(song_dir) == song_dir / "angie.mp4"


def test_song_video_path_returns_none_when_not_yet_rendered(tmp_path):
    song_dir = tmp_path / "angie-rolling-stones"
    song_dir.mkdir()
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")

    assert song_video_path(song_dir) is None


def test_song_video_path_returns_none_with_no_lyrics_timed_json(tmp_path):
    song_dir = tmp_path / "angie-rolling-stones"
    song_dir.mkdir()

    assert song_video_path(song_dir) is None


def test_list_rendered_songs_includes_an_already_uploaded_song(tmp_path):
    """Unlike list_pending_uploads(), this backs the single-song Upload
    dropdown -- it must include a song that already uploaded, so the owner
    can force a fresh re-upload (a correction/re-post) for any past song,
    not just their most-recently-generated one."""
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")
    (song_dir / "angie.mp4").write_bytes(b"video")
    (song_dir / "youtube_state.json").write_text("{}", encoding="utf-8")

    assert list_rendered_songs(work_root) == ["angie-rolling-stones"]


def test_list_rendered_songs_excludes_a_song_with_no_rendered_video_yet(tmp_path):
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")

    assert list_rendered_songs(work_root) == []


def test_list_rendered_songs_returns_empty_list_when_work_dir_missing(tmp_path):
    assert list_rendered_songs(tmp_path / "does-not-exist") == []


def test_list_pending_uploads_finds_a_rendered_song_with_no_youtube_state(tmp_path):
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")
    (song_dir / "angie.mp4").write_bytes(b"video")

    assert list_pending_uploads(work_root) == ["angie-rolling-stones"]


def test_list_pending_uploads_excludes_a_song_already_recorded_as_uploaded(tmp_path):
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")
    (song_dir / "angie.mp4").write_bytes(b"video")
    (song_dir / "youtube_state.json").write_text("{}", encoding="utf-8")

    assert list_pending_uploads(work_root) == []


def test_list_pending_uploads_excludes_a_song_with_no_rendered_video_yet(tmp_path):
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")

    assert list_pending_uploads(work_root) == []


def test_list_pending_uploads_returns_empty_list_when_work_dir_missing(tmp_path):
    assert list_pending_uploads(tmp_path / "does-not-exist") == []


def test_list_flagged_songs_finds_a_rendered_song_with_a_concern(tmp_path):
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(
        Song(title="Angie", audio_path="a.mp3", lyrics_accuracy_concern="looks like the wrong song"),
        song_dir / "lyrics_timed.json",
    )
    (song_dir / "angie.mp4").write_bytes(b"video")

    assert list_flagged_songs(work_root) == ["angie-rolling-stones"]


def test_list_flagged_songs_excludes_a_song_with_no_concern(tmp_path):
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(Song(title="Angie", audio_path="a.mp3"), song_dir / "lyrics_timed.json")
    (song_dir / "angie.mp4").write_bytes(b"video")

    assert list_flagged_songs(work_root) == []


def test_list_flagged_songs_excludes_a_song_already_uploaded(tmp_path):
    """A flagged song the owner already uploaded anyway (Upload Anyway)
    drops off this list on its own -- no separate "dismiss" needed."""
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(
        Song(title="Angie", audio_path="a.mp3", lyrics_accuracy_concern="looks wrong"),
        song_dir / "lyrics_timed.json",
    )
    (song_dir / "angie.mp4").write_bytes(b"video")
    (song_dir / "youtube_state.json").write_text("{}", encoding="utf-8")

    assert list_flagged_songs(work_root) == []


def test_list_flagged_songs_excludes_a_song_with_no_rendered_video_yet(tmp_path):
    work_root = tmp_path / "work"
    song_dir = work_root / "angie-rolling-stones"
    song_dir.mkdir(parents=True)
    save_song(
        Song(title="Angie", audio_path="a.mp3", lyrics_accuracy_concern="looks wrong"),
        song_dir / "lyrics_timed.json",
    )

    assert list_flagged_songs(work_root) == []


def test_list_flagged_songs_returns_empty_list_when_work_dir_missing(tmp_path):
    assert list_flagged_songs(tmp_path / "does-not-exist") == []


def test_backup_song_outputs_copies_video_and_timed_json(tmp_path):
    work_dir = tmp_path / "angie-rolling-stones"
    work_dir.mkdir()
    (work_dir / "angie.mp4").write_bytes(b"old video bytes")
    save_song(Song(title="Angie", audio_path="a.mp3"), work_dir / "lyrics_timed.json")

    backup_dir = backup_song_outputs(work_dir, "angie", now=datetime(2026, 9, 8, 12, 0, 0))

    assert backup_dir == work_dir / "redo_backup_20260908-120000"
    assert (backup_dir / "angie.mp4").read_bytes() == b"old video bytes"
    assert (backup_dir / "lyrics_timed.json").exists()
    assert (work_dir / "angie.mp4").exists()
    assert (work_dir / "lyrics_timed.json").exists()


def test_backup_song_outputs_returns_none_when_nothing_to_back_up(tmp_path):
    work_dir = tmp_path / "brand-new-song"
    work_dir.mkdir()

    assert backup_song_outputs(work_dir, "brand-new-song", now=datetime(2026, 9, 8, 12, 0, 0)) is None


def test_backup_song_outputs_backs_up_whichever_of_the_two_exists(tmp_path):
    work_dir = tmp_path / "angie-rolling-stones"
    work_dir.mkdir()
    (work_dir / "angie.mp4").write_bytes(b"old video bytes")

    backup_dir = backup_song_outputs(work_dir, "angie", now=datetime(2026, 9, 8, 12, 0, 0))

    assert (backup_dir / "angie.mp4").exists()
    assert not (backup_dir / "lyrics_timed.json").exists()


def test_prepare_images_for_fresh_regeneration_moves_existing_dir_aside(tmp_path):
    work_dir = tmp_path / "angie-rolling-stones"
    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "abc123.png").write_bytes(b"old image bytes")

    moved_to = prepare_images_for_fresh_regeneration(images_dir, now=datetime(2026, 9, 8, 12, 0, 0))

    assert moved_to == work_dir / "images_prior_20260908-120000"
    assert (moved_to / "abc123.png").read_bytes() == b"old image bytes"
    assert not images_dir.exists()
    assert not moved_to.name.startswith("images_backup_")


def test_prepare_images_for_fresh_regeneration_returns_none_when_no_images_dir(tmp_path):
    images_dir = tmp_path / "angie-rolling-stones" / "images"

    assert prepare_images_for_fresh_regeneration(images_dir, now=datetime(2026, 9, 8, 12, 0, 0)) is None


def test_run_pipeline_no_settings_argument_uses_all_defaults(tmp_path, monkeypatch):
    """Backward-compatibility contract: omitting settings entirely must call
    detect_chords/assemble_video with exactly the same Settings-derived values
    as before this feature existed. chord_legend_labels is the one exception --
    it's derived from the song's own chord_track, not from Settings, so it's
    always passed regardless of whether settings is None."""
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_detect_chords(path, **kwargs):
        captured["detect_chords_kwargs"] = kwargs
        return ChordTrack(events=[ChordEvent(0.0, 1.0, "C")])

    def spying_assemble_video(*args, **kwargs):
        captured["assemble_video_kwargs"] = kwargs

    monkeypatch.setattr("lyricvideo.pipeline.detect_chords", spying_detect_chords)
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", spying_assemble_video)

    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)

    assert captured["detect_chords_kwargs"] == {}
    assert captured["assemble_video_kwargs"] == {"chord_legend_labels": ["C"]}


def test_run_pipeline_settings_reach_detect_chords(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_detect_chords(path, **kwargs):
        captured["kwargs"] = kwargs
        return ChordTrack(events=[ChordEvent(0.0, 1.0, "C")])

    monkeypatch.setattr("lyricvideo.pipeline.detect_chords", spying_detect_chords)

    settings = Settings(snap_chords_to_key=False, prefer_flats=False,
                        include_seventh_chords=True, min_chord_seconds=1.0)
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir, settings=settings)

    assert captured["kwargs"] == {
        "snap_chords_to_key": False, "prefer_flats": False,
        "include_seventh_chords": True, "min_chord_seconds": 1.0,
    }


def test_run_pipeline_settings_reach_assemble_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_assemble_video(*args, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", spying_assemble_video)

    settings = Settings(
        resolution="720p (1280x720)", fps=30, encoder="libx265", crf=24,
        lyric_size=52, text_color="#ff0000", accent_color="#00ff00",
        dim_text_color="#0000ff", panel_color="#111111", panel_alpha=100,
        chord_now_size=70, chord_next_size=36, show_chord_timeline=False,
        show_key_bpm=False, timeline_window_sec=8.0,
    )
    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir, settings=settings)

    assert captured["kwargs"]["frame_size"] == (1280, 720)
    assert captured["kwargs"]["fps"] == 30
    assert captured["kwargs"]["encoder"] == "libx265"
    assert captured["kwargs"]["crf"] == 24
    assert captured["kwargs"]["lyric_size"] == 52
    assert captured["kwargs"]["text_color"] == (255, 0, 0)
    assert captured["kwargs"]["accent_color"] == (0, 255, 0)
    assert captured["kwargs"]["dim_text_color"] == (0, 0, 255)
    assert captured["kwargs"]["panel_color"] == (17, 17, 17)
    assert captured["kwargs"]["panel_alpha"] == 100
    assert captured["kwargs"]["chord_now_size"] == 70
    assert captured["kwargs"]["chord_next_size"] == 36
    assert captured["kwargs"]["show_chord_timeline"] is False
    assert captured["kwargs"]["show_key_bpm"] is False
    assert captured["kwargs"]["timeline_window_sec"] == 8.0


def test_run_pipeline_passes_ordered_unique_chords_to_assemble_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    captured = {}

    def spying_assemble_video(*args, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", spying_assemble_video)
    monkeypatch.setattr(
        "lyricvideo.pipeline.detect_chords",
        lambda path, **kwargs: ChordTrack(events=[
            ChordEvent(0.0, 1.0, "G"), ChordEvent(1.0, 2.0, "D"), ChordEvent(2.0, 3.0, "G"),
        ]),
    )

    work_dir = tmp_path / "work"
    run_pipeline(Path("audio.mp3"), work_dir)

    assert captured["kwargs"]["chord_legend_labels"] == ["G", "D"]


def test_run_pipeline_explains_a_song_with_no_lyric_text_instead_of_dying_in_the_aligner(tmp_path, monkeypatch):
    """fetch_lyric_lines_verified returns [] when nothing was found anywhere
    (or the track is flagged instrumental); the align stage then aborted
    inside the aligner with "no words to align", which says nothing about
    what to do next."""
    import pytest

    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified",
        lambda *a, **k: ([], "", "No lyrics found from any source."),
    )

    with pytest.raises(RuntimeError, match=r"No lyrics were found for 'Test Song'.*audio\.lrc"):
        run_pipeline(Path("audio.mp3"), tmp_path / "work")


def test_run_pipeline_gives_the_lyrics_fetch_an_audio_check_built_from_the_transcript(tmp_path, monkeypatch):
    from lyricvideo.lyric_audio_match import audio_match_passes

    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda vocals, work_dir: "pale morning harbor lantern")
    seen = {}

    def fake_fetch(*args, **kwargs):
        seen["audio_check"] = kwargs.get("audio_check")
        return ["pale morning harbor lantern"], "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fake_fetch)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    check = seen["audio_check"]
    assert callable(check)
    assert audio_match_passes(check(["pale morning harbor lantern"]))
    assert not audio_match_passes(check(["thunder rolling over concrete valleys"]))


def test_run_pipeline_falls_back_to_the_text_check_when_transcription_fails(tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path)

    def broken(*a, **k):
        raise RuntimeError("model download blocked")

    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", broken)
    seen = {}

    def fake_fetch(*args, **kwargs):
        seen["audio_check"] = kwargs.get("audio_check", "missing")
        return ["hello there"], "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fake_fetch)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")  # must not raise

    assert seen["audio_check"] is None
    assert "model download blocked" in capsys.readouterr().err


def test_run_pipeline_tells_the_log_when_lyrics_were_verified_against_the_audio(tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified", lambda *a, **k: (["hello there"], "lrclib", ""),
    )

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert "verified against the audio (source: lrclib)" in capsys.readouterr().out


def test_run_pipeline_warns_in_the_log_when_lyrics_could_not_be_confirmed(tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified",
        lambda *a, **k: (["hello there"], "lrclib", "Only 41% of these lyrics match what is sung; lines 3-9 don't match the audio."),
    )

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    err = capsys.readouterr().err
    assert "could not be confirmed against the audio" in err
    assert "lines 3-9" in err
    assert "Flagged for Lyrics Review" in err


def test_run_pipeline_says_it_is_listening_before_the_slow_transcription(tmp_path, monkeypatch, capsys):
    """Transcribing takes about a minute; the log must not sit silent meanwhile."""
    _patch_common(monkeypatch, tmp_path)
    order = []
    monkeypatch.setattr(
        "lyricvideo.pipeline.transcribe_vocals",
        lambda *a, **k: order.append("transcribe") or "hello there my friend",
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.print",
        lambda *a, **k: order.append(("print", " ".join(str(x) for x in a))),
        raising=False,
    )

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    first_listening = next(i for i, x in enumerate(order) if isinstance(x, tuple) and "Listening" in x[1])
    assert first_listening < order.index("transcribe")


def test_run_pipeline_gives_the_lyrics_fetch_a_repair_step_that_saves_its_suggestion(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    segments = [{"start": 1.0, "end": 3.0, "text": "hello there my friend"}]
    monkeypatch.setattr("lyricvideo.pipeline.load_transcript_segments", lambda work_dir: segments)
    calls = {}

    def fake_reconcile_lyrics(client, lines, match, segs):
        calls.update(client=client, lines=lines, segments=segs)
        return ["hello there", "my friend"], ["line 2 replaced"]

    monkeypatch.setattr("lyricvideo.pipeline.reconcile_lyrics", fake_reconcile_lyrics)
    seen = {}

    def fake_fetch(*args, **kwargs):
        seen["reconcile"] = kwargs.get("reconcile")
        return ["hello there"], "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fake_fetch)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    result = seen["reconcile"](["some line"], object())
    assert result == (["hello there", "my friend"], ["line 2 replaced"])
    assert calls["lines"] == ["some line"] and calls["segments"] == segments
    # The suggestion is saved for the owner to read; it is never fed back into the video.
    assert (work_dir / "lyrics_suggested.txt").read_text(encoding="utf-8").splitlines() == ["hello there", "my friend"]


def test_run_pipeline_offers_no_repair_step_when_the_audio_could_not_be_checked(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    seen = {}

    def fake_fetch(*args, **kwargs):
        seen["reconcile"] = kwargs.get("reconcile", "missing")
        return ["hello there"], "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fake_fetch)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert seen["reconcile"] is None


def test_run_pipeline_gives_the_lyrics_fetch_an_ai_judge_that_reads_the_saved_transcript(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    segments = [{"start": 1.0, "end": 3.0, "text": "hello there my friend"}]
    monkeypatch.setattr("lyricvideo.pipeline.load_transcript_segments", lambda work_dir: segments)
    calls = {}

    def fake_arbitrate(client, lines, match, segs):
        calls.update(client=client, lines=lines, segments=segs)
        return "the-judgement"

    monkeypatch.setattr("lyricvideo.pipeline.arbitrate", fake_arbitrate)
    seen = {}

    def fake_fetch(*args, **kwargs):
        seen["arbiter"] = kwargs.get("arbiter")
        return ["hello there"], "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fake_fetch)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert seen["arbiter"](["some line"], object()) == "the-judgement"
    assert calls["lines"] == ["some line"] and calls["segments"] == segments


def test_run_pipeline_offers_no_ai_judge_when_the_audio_could_not_be_checked(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    seen = {}

    def fake_fetch(*args, **kwargs):
        seen["arbiter"] = kwargs.get("arbiter", "missing")
        return ["hello there"], "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fake_fetch)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert seen["arbiter"] is None


def test_run_pipeline_says_when_ai_review_accepted_lyrics_the_recognizer_could_not_confirm(tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified", lambda *a, **k: (["hello there"], "lrclib+ai-confirmed", ""),
    )

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    out = capsys.readouterr().out
    assert "AI review" in out and "speech-recognition" in out and "lrclib" in out


def _truncated_stems(work_dir, audio_stem="audio", seconds=5):
    import numpy as np
    import soundfile

    stem_dir = work_dir / "htdemucs" / audio_stem
    stem_dir.mkdir(parents=True)
    for name in ("vocals.wav", "no_vocals.wav"):
        soundfile.write(str(stem_dir / name), np.zeros(seconds * 8000), 8000)


def test_run_pipeline_reruns_separation_when_the_stems_on_disk_are_truncated(tmp_path, monkeypatch):
    """Real ('Ironic'): 43-second stems for a 230-second song were used silently, for both the lyric
    alignment and the chords. Resuming past separation must notice and run Demucs again."""
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(json.dumps({"title": "Test Song", "artist": "A", "duration": 60.0, "alt_titles": []}))
    _truncated_stems(work_dir)
    calls = []
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: calls.append(k) or tmp_path / "vocals.wav")

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics")

    assert len(calls) == 1
    assert calls[0]["expected_seconds"] == 60.0


def test_run_pipeline_keeps_complete_stems_and_does_not_rerun_demucs(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "song_info.json").write_text(json.dumps({"title": "Test Song", "artist": "A", "duration": 5.0, "alt_titles": []}))
    _truncated_stems(work_dir, seconds=5)               # 5 s stems for a 5 s song: complete
    monkeypatch.setattr(
        "lyricvideo.pipeline.separate_vocals",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("complete stems must not be re-separated")),
    )

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics")


def test_run_pipeline_tells_the_separation_stage_how_long_the_song_is(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(
        "lyricvideo.pipeline.separate_vocals",
        lambda *a, **k: seen.update(k) or tmp_path / "vocals.wav",
    )

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert seen["expected_seconds"] == 10.0             # the stubbed identify stage reports 10 s


# --- anchored alignment + the sync check (2026-09-19) -----------------------------------------------

_SYNC_LINES = [
    "the river runs beside the old stone mill", "and morning fog lies heavy on the hill",
    "a lantern swings above the wooden door", "i wait for you like i have waited before",
    "the winter came and covered every road", "we traded all our dreams for heavy loads",
    "a whisper crossed the empty market square", "and told me you were waiting for me there",
]
_TRUE_STARTS = [10.0, 16.0, 22.0, 28.0, 34.0, 40.0, 46.0, 52.0]


def _heard_at(starts):
    return [
        {"word": w, "start": s + i * 0.4, "end": s + i * 0.4 + 0.35}
        for s, line in zip(starts, _SYNC_LINES) for i, w in enumerate(line.split())
    ]


def _times_starting_at(starts):
    times = []
    for s, line in zip(starts, _SYNC_LINES):
        times.extend((s + i * 0.4, s + i * 0.4 + 0.35) for i in range(len(line.split())))
    return times


def _sync_setup(monkeypatch, tmp_path, whole_starts, anchored_starts, heard=True):
    import torch

    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr("lyricvideo.pipeline.torchaudio.load", lambda path: (torch.zeros(1, 16000 * 60), 16000))  # a 60 s stem
    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", lambda *a, **k: (list(_SYNC_LINES), "lrclib", ""))
    monkeypatch.setattr("lyricvideo.pipeline.load_transcript_words", lambda work_dir: _heard_at(_TRUE_STARTS) if heard else [])
    used = {"whole": 0, "anchored": 0, "prepared": 0}
    monkeypatch.setattr("lyricvideo.pipeline.prepare_alignment", lambda *a, **k: used.__setitem__("prepared", used["prepared"] + 1) or object())

    def whole(vocals_path, words, prepared=None):
        used["whole"] += 1
        return _times_starting_at(whole_starts)

    def anchored(vocals_path, lines_words, anchors, prepared=None, **k):
        used["anchored"] += 1
        return _times_starting_at(anchored_starts), []

    monkeypatch.setattr("lyricvideo.pipeline.align_words", whole)
    monkeypatch.setattr("lyricvideo.pipeline.align_words_anchored", anchored)
    return used


def test_a_whole_song_alignment_that_agrees_with_what_was_heard_is_kept(tmp_path, monkeypatch, capsys):
    used = _sync_setup(monkeypatch, tmp_path, _TRUE_STARTS, [s + 99 for s in _TRUE_STARTS])
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    song = load_song(work_dir / "lyrics_timed.json")
    assert abs(song.lines[0].words[0].start_time - 10.0) < 0.01
    assert "sync" not in song.lyrics_accuracy_concern.lower() and "timing" not in song.lyrics_accuracy_concern.lower()
    assert "whole-song" in capsys.readouterr().out


def test_a_drifted_whole_song_alignment_is_replaced_by_the_anchored_one(tmp_path, monkeypatch, capsys):
    """Real ('Girls Just Want to Have Fun'): the whole-song pass put lines 20-50 s early."""
    drifted = [max(0.0, s - 25.0) for s in _TRUE_STARTS]
    _sync_setup(monkeypatch, tmp_path, drifted, _TRUE_STARTS)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    song = load_song(work_dir / "lyrics_timed.json")
    assert abs(song.lines[3].words[0].start_time - 28.0) < 0.01           # the anchored time, not the drifted one
    assert song.lyrics_accuracy_concern == ""
    assert "anchored" in capsys.readouterr().out


def test_a_song_whose_timing_cannot_be_fixed_is_set_aside_for_review(tmp_path, monkeypatch):
    _sync_setup(monkeypatch, tmp_path, [max(0.0, s - 25.0) for s in _TRUE_STARTS], [s + 30.0 for s in _TRUE_STARTS])
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    concern = load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern
    assert "timing" in concern.lower() and "review" in concern.lower()


def test_without_word_timings_the_original_whole_song_alignment_is_used_unchanged(tmp_path, monkeypatch):
    used = _sync_setup(monkeypatch, tmp_path, _TRUE_STARTS, _TRUE_STARTS, heard=False)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert used == {"whole": 1, "anchored": 0, "prepared": 0}


def test_the_timing_concern_is_added_to_an_existing_lyrics_concern(tmp_path, monkeypatch):
    _sync_setup(monkeypatch, tmp_path, [max(0.0, s - 25.0) for s in _TRUE_STARTS], [s + 30.0 for s in _TRUE_STARTS])
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified",
        lambda *a, **k: (list(_SYNC_LINES), "lrclib", "Only 60% of these lyrics match what is sung."),
    )
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    concern = load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern
    assert concern.startswith("Only 60%") and "timing" in concern.lower()


def test_words_heard_in_a_silent_stretch_do_not_anchor_any_line(tmp_path, monkeypatch):
    """Real ('Girls Just Want to Have Fun'): Whisper 'heard' words after the vocals had gone silent, which
    anchored a line at the wrong time and dragged the rest into the silent tail."""
    used = _sync_setup(monkeypatch, tmp_path, _TRUE_STARTS, _TRUE_STARTS)
    ghost_starts = _TRUE_STARTS[:6] + [50.0, 56.0]                        # the last two lines "heard" in silence
    monkeypatch.setattr("lyricvideo.pipeline.load_transcript_words", lambda work_dir: _heard_at(ghost_starts))
    monkeypatch.setattr("lyricvideo.pipeline.vocal_loudness", lambda path: [0.5] * 90 + [0.0] * 30)   # voiced to 45 s
    seen = {}

    def anchored(vocals_path, lines_words, anchors, prepared=None, **k):
        seen["anchors"] = sorted(anchors)
        return _times_starting_at(_TRUE_STARTS), []

    monkeypatch.setattr("lyricvideo.pipeline.align_words", lambda vocals_path, words, prepared=None: _times_starting_at([s - 30 for s in _TRUE_STARTS]))
    monkeypatch.setattr("lyricvideo.pipeline.align_words_anchored", anchored)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert seen["anchors"] == [0, 1, 2, 3, 4, 5]


def test_the_lyrics_stage_saves_the_sources_line_times_for_the_aligner(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)

    def fetch(*args, **kwargs):
        kwargs["times_out"]["line_times"] = [1.5, 4.0]
        return ["hello there", "my friend"], "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fetch)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    assert json.loads((work_dir / "lyric_lines.json").read_text())["line_times"] == [1.5, 4.0]


def test_lrclib_line_times_repair_whisper_anchors_that_picked_the_wrong_chorus_copy(tmp_path, monkeypatch):
    """Real ('Girls Just Want to Have Fun'): repeated lines were anchored to the wrong occurrence, 20-30 s off."""
    _sync_setup(monkeypatch, tmp_path, _TRUE_STARTS, _TRUE_STARTS)
    wrong = list(_TRUE_STARTS)
    wrong[5] = 100.0                                     # line 6 "heard" at the wrong copy
    monkeypatch.setattr("lyricvideo.pipeline.load_transcript_words", lambda work_dir: _heard_at(wrong))
    monkeypatch.setattr("lyricvideo.pipeline.vocal_loudness", lambda path: [])

    def fetch(*args, **kwargs):
        kwargs["times_out"]["line_times"] = [s - 3.0 for s in _TRUE_STARTS]     # lrclib's edition runs 3 s earlier
        return list(_SYNC_LINES), "lrclib", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fetch)
    seen = {}

    def anchored(vocals_path, lines_words, anchors, prepared=None, **k):
        seen["anchors"] = anchors
        return _times_starting_at(_TRUE_STARTS), []

    monkeypatch.setattr("lyricvideo.pipeline.align_words", lambda vocals_path, words, prepared=None: _times_starting_at([s - 30 for s in _TRUE_STARTS]))
    monkeypatch.setattr("lyricvideo.pipeline.align_words_anchored", anchored)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert abs(seen["anchors"][5].start - _TRUE_STARTS[5]) < 0.6         # lrclib + the offset, not the bad 100.0


def _loud_song_setup(monkeypatch, tmp_path, source_times):
    """Whisper hears lines 1-3 about 9 s late (a consistent run, so combine_anchors keeps it) -- only ~62% agree.
    Precision (lyricvideo/precision.py) needs plenty of heard words; with too few the line-level gate is used."""
    _sync_setup(monkeypatch, tmp_path, _TRUE_STARTS, _TRUE_STARTS)
    monkeypatch.setattr("lyricvideo.precision.MIN_MATCHED_WORDS", 10**6)
    misheard = [s + 9.0 if i < 3 else s for i, s in enumerate(_TRUE_STARTS)]
    monkeypatch.setattr("lyricvideo.pipeline.load_transcript_words", lambda work_dir: _heard_at(misheard))
    monkeypatch.setattr("lyricvideo.pipeline.vocal_loudness", lambda path: [])

    def fetch(*args, **kwargs):
        kwargs["times_out"]["line_times"] = source_times
        return list(_SYNC_LINES), "netease", ""

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", fetch)


def test_a_loud_song_whisper_only_half_confirms_is_kept_when_the_sources_own_line_times_agree(tmp_path, monkeypatch):
    """Real ('Back in the Saddle', 2026-09-19): NetEase's timestamps agreed on 31 of 33 lines, Whisper on ~70%."""
    _loud_song_setup(monkeypatch, tmp_path, [s - 0.5 for s in _TRUE_STARTS])
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    assert load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern == ""


def test_the_same_song_is_set_aside_when_the_source_has_no_line_times(tmp_path, monkeypatch):
    _loud_song_setup(monkeypatch, tmp_path, None)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    assert "review" in load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern.lower()


def test_the_better_alignment_is_used_line_by_line_and_reported_as_blended(tmp_path, monkeypatch, capsys):
    """Real (Go Your Own Way, Money, The Chain 2026-09-19): the whole-song pass was exact on some lines and 2 s late
    on others, the anchored pass the reverse; neither alone was right."""
    whole = [s if i < 4 else s + 2.0 for i, s in enumerate(_TRUE_STARTS)]
    anchored = [s + 2.0 if i < 4 else s for i, s in enumerate(_TRUE_STARTS)]
    _sync_setup(monkeypatch, tmp_path, whole, anchored)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    song = load_song(work_dir / "lyrics_timed.json")
    assert all(abs(line.words[0].start_time - s) < 0.01 for line, s in zip(song.lines, _TRUE_STARTS))
    assert song.lyrics_accuracy_concern == ""
    assert "blended" in capsys.readouterr().out


def test_a_song_whose_lines_are_close_but_not_precise_is_set_aside_naming_the_lines(tmp_path, monkeypatch):
    """The old line check tolerated ~2 s (+2.5 s of bias); on screen a line 1.9 s late means the singer is a line ahead."""
    late = [s + 1.9 for s in _TRUE_STARTS]
    _sync_setup(monkeypatch, tmp_path, late, late)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    concern = load_song(work_dir / "lyrics_timed.json").lyrics_accuracy_concern
    assert "half a second" in concern and "lines 1, 2" in concern and "review" in concern.lower()


# --- the owner's own edited lyrics (2026-09-19) ---------------------------------------------------

def test_the_owners_edited_lyrics_are_used_instead_of_any_online_source(tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "lyrics_owner.txt").write_text("my corrected first line\nmy second line\n", encoding="utf-8")

    def must_not_run(*a, **k):
        raise AssertionError("no online lookup or AI check may override the owner's lyrics")

    monkeypatch.setattr("lyricvideo.pipeline.fetch_lyric_lines_verified", must_not_run)

    run_pipeline(Path("audio.mp3"), work_dir)

    saved = json.loads((work_dir / "lyric_lines.json").read_text())
    assert saved["lines"] == ["my corrected first line", "my second line"]
    assert saved["source"] == "owner" and saved["concern"] == ""
    song = load_song(work_dir / "lyrics_timed.json")
    assert song.lyrics_source == "owner" and song.lyrics_accuracy_concern == ""
    assert "lyrics you edited" in capsys.readouterr().out


def test_the_owners_lyrics_still_get_whisper_word_timings_for_the_aligner(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "lyrics_owner.txt").write_text("a line\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: calls.append(a) or "words")

    run_pipeline(Path("audio.mp3"), work_dir)

    assert len(calls) == 1


def test_a_failing_transcription_does_not_stop_a_song_with_owner_lyrics(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "lyrics_owner.txt").write_text("a line\n", encoding="utf-8")
    monkeypatch.setattr("lyricvideo.pipeline.transcribe_vocals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))

    run_pipeline(Path("audio.mp3"), work_dir)          # must not raise

    assert load_song(work_dir / "lyrics_timed.json").lyrics_source == "owner"


def test_list_flagged_songs_can_include_songs_already_uploaded(tmp_path):
    """The review panel also shows set-aside songs that are already on YouTube (e.g. the damaged 'Ironic'), since
    replacing those there is the owner's call; the auto-upload paths keep using the default (pending only)."""
    work_root = tmp_path / "work"
    for slug, uploaded in (("pending-song", False), ("uploaded-song", True)):
        song_dir = work_root / slug
        song_dir.mkdir(parents=True)
        save_song(Song(title=slug, audio_path="a.mp3", lyrics_accuracy_concern="looks wrong"), song_dir / "lyrics_timed.json")
        (song_dir / f"{slug}.mp4").write_bytes(b"video")
        if uploaded:
            (song_dir / "youtube_state.json").write_text("{}", encoding="utf-8")

    assert list_flagged_songs(work_root) == ["pending-song"]
    assert list_flagged_songs(work_root, include_uploaded=True) == ["pending-song", "uploaded-song"]


# --- the permanent record of redone songs -----------------------------------------------------------

def test_backing_up_a_song_for_a_redo_starts_a_redo_record(tmp_path):
    from lyricvideo.redo_log import redone_songs

    work_dir = tmp_path / "work" / "angie"
    work_dir.mkdir(parents=True)
    (work_dir / "angie.mp4").write_bytes(b"video")
    (work_dir / "youtube_state.json").write_text(json.dumps({"video_id": "abc", "uploaded_at": "2026-09-01T00:00:00", "title": "t"}))

    backup = backup_song_outputs(work_dir, "angie")

    records = redone_songs()
    assert len(records) == 1 and records[0]["slug"] == "angie" and records[0]["status"] == "started"
    assert records[0]["on_youtube"] is True and records[0]["video_id"] == "abc"
    assert records[0]["backup_dir"] == str(backup)


def test_a_redo_that_finishes_completes_its_record_with_the_songs_concern(tmp_path, monkeypatch):
    from lyricvideo.redo_log import redone_songs

    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lyricvideo.pipeline.fetch_lyric_lines_verified",
        lambda *a, **k: (["hello there"], "lrclib", "Only 60% of these lyrics match what is sung."),
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "work.mp4").write_bytes(b"video")
    backup_song_outputs(work_dir, "work")                       # what every redo path does first

    run_pipeline(Path("audio.mp3"), work_dir, start_stage="fetch_lyrics")

    record = redone_songs()[0]
    assert record["status"] == "done" and record["set_aside"] is True and "60%" in record["concern"]


def test_a_first_run_of_a_new_song_leaves_no_redo_record(tmp_path, monkeypatch):
    from lyricvideo.redo_log import redone_songs

    _patch_common(monkeypatch, tmp_path)

    run_pipeline(Path("audio.mp3"), tmp_path / "work")

    assert redone_songs() == []
