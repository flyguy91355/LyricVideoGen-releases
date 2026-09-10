import json
from datetime import datetime
from pathlib import Path

from lyricvideo.models import ChordEvent, ChordTrack, LyricLine, Song, Word, save_song
from lyricvideo.pipeline import (
    run_pipeline,
    list_redoable_songs,
    load_redo_inputs,
    backup_song_outputs,
    prepare_images_for_fresh_regeneration,
    _instrumental_chord_labels,
    ordered_unique_chords,
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
        "lyricvideo.pipeline.fetch_lyric_lines", lambda *a, **k: ["hello there", "my friend"]
    )
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: tmp_path / "vocals.wav")
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


def test_run_pipeline_detect_chords_writes_chord_track_onto_song(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"

    run_pipeline(Path("audio.mp3"), work_dir)

    from lyricvideo.models import load_song

    saved = load_song(work_dir / "lyrics_timed.json")
    assert saved.chord_track.key == "C major"
    assert saved.chord_track.events[0].label == "C"


def test_instrumental_chord_labels_skips_chords_covered_by_a_sung_line():
    lines = [LyricLine(start_time=0.0, end_time=2.0)]
    chord_track = ChordTrack(events=[ChordEvent(0.0, 2.0, "C"), ChordEvent(2.0, 4.0, "G")])

    assert _instrumental_chord_labels(lines, chord_track) == ["G"]


def test_instrumental_chord_labels_dedupes_repeated_labels():
    lines = []
    chord_track = ChordTrack(events=[
        ChordEvent(0.0, 2.0, "Am"), ChordEvent(2.0, 4.0, "F"), ChordEvent(4.0, 6.0, "Am"),
    ])

    assert _instrumental_chord_labels(lines, chord_track) == ["Am", "F"]


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
