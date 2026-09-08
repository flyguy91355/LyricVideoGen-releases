import json
from pathlib import Path

from lyricvideo.models import ChordWord, InstrumentalChord, LyricLine, Song, save_song
from lyricvideo.pipeline import line_to_dict, dict_to_line, _parsed_to_dict, run_pipeline


def test_line_dict_round_trip():
    line = LyricLine(
        words=[ChordWord(word="hi", chord="G", start_time=0.1, end_time=0.5)],
        start_time=0.1, end_time=0.5,
    )
    restored = dict_to_line(line_to_dict(line))
    assert restored == line


def test_run_pipeline_requires_pdf_or_chords_text_file():
    import pytest

    with pytest.raises(ValueError):
        run_pipeline(Path("audio.mp3"), None, Path("work"), "t", start_stage="parse")


def test_run_pipeline_uses_chords_text_file_and_skips_pdf_entirely(tmp_path, monkeypatch):
    calls = []

    def fail_if_called(*a, **k):
        calls.append("pdf_or_vision_called")
        raise AssertionError("should not be called when chords_text_file is given")

    monkeypatch.setattr("lyricvideo.pipeline.parse_tab_pdf", fail_if_called)
    monkeypatch.setattr("lyricvideo.pipeline.map_chords_with_vision", fail_if_called)
    monkeypatch.setattr(
        "lyricvideo.pipeline.parse_plaintext_chords",
        lambda text: (calls.append("plaintext_parsed") or ([LyricLine(words=[ChordWord(word="hi")])], [])),
    )
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: tmp_path / "vocals.wav")
    import torch

    monkeypatch.setattr("lyricvideo.pipeline.torchaudio.load", lambda path: (torch.zeros(1, 16000), 16000))
    monkeypatch.setattr("lyricvideo.pipeline.align_words", lambda *a, **k: [(0.0, 0.5)])
    monkeypatch.setattr("lyricvideo.pipeline.combine_alignment", lambda lines, times, dur: lines)
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: "gist")
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: Path("x"))
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")

    chords_text_file = tmp_path / "chords.txt"
    chords_text_file.write_text("G\nhi\n", encoding="utf-8")
    work_dir = tmp_path / "work"

    run_pipeline(
        Path("audio.mp3"), None, work_dir, "t",
        start_stage="parse", chords_text_file=chords_text_file,
    )

    assert "plaintext_parsed" in calls
    assert "pdf_or_vision_called" not in calls


def test_run_pipeline_reports_progress_per_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lyricvideo.pipeline.parse_plaintext_chords",
        lambda text: ([LyricLine(words=[ChordWord(word="hi")])], []),
    )
    monkeypatch.setattr("lyricvideo.pipeline.separate_vocals", lambda *a, **k: tmp_path / "vocals.wav")
    import torch

    monkeypatch.setattr("lyricvideo.pipeline.torchaudio.load", lambda path: (torch.zeros(1, 16000), 16000))
    monkeypatch.setattr("lyricvideo.pipeline.align_words", lambda *a, **k: [(0.0, 0.5)])
    monkeypatch.setattr("lyricvideo.pipeline.combine_alignment", lambda lines, times, dur: lines)
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: "gist")
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: Path("x"))
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")

    chords_text_file = tmp_path / "chords.txt"
    chords_text_file.write_text("G\nhi\n", encoding="utf-8")
    work_dir = tmp_path / "work"

    reported = []
    run_pipeline(
        Path("audio.mp3"), None, work_dir, "t",
        chords_text_file=chords_text_file,
        progress_callback=reported.append,
    )

    assert reported == ["separate", "parse", "align", "images", "render", "done"]


def test_run_pipeline_skips_earlier_stages(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        "lyricvideo.pipeline.separate_vocals",
        lambda *a, **k: calls.append("separate") or (tmp_path / "vocals.wav"),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.parse_tab_pdf",
        lambda *a, **k: calls.append("parse") or ([], []),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.align_words",
        lambda *a, **k: calls.append("align") or [],
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.combine_alignment",
        lambda *a, **k: calls.append("align") or [],
    )
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: "a fake song gist")
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image",
        lambda *a, **k: calls.append("images") or Path("x"),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.assemble_video",
        lambda *a, **k: calls.append("render"),
    )
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    seeded_line = LyricLine(
        words=[ChordWord(word="hi", chord="G", start_time=0.0, end_time=1.0)],
        start_time=0.0, end_time=1.0,
    )
    save_song(
        Song(title="t", audio_path="a.mp3", lines=[seeded_line]),
        work_dir / "lyrics_timed.json",
    )

    run_pipeline(Path("audio.mp3"), Path("tab.pdf"), work_dir, "t", start_stage="images")

    assert calls == ["images", "render"]


def test_run_pipeline_resumes_align_stage_with_correct_vocals_path(tmp_path, monkeypatch):
    captured = {}
    import torch

    monkeypatch.setattr(
        "lyricvideo.pipeline.torchaudio.load",
        lambda path: (captured.update(load_path=path)) or (torch.zeros(1, 16000), 16000),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.align_words",
        lambda vocals_path, words: (captured.update(align_vocals_path=vocals_path)) or [(0.0, 0.5)],
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.combine_alignment",
        lambda parsed_lines, word_times, audio_duration: parsed_lines,
    )
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: "a fake song gist")
    monkeypatch.setattr(
        "lyricvideo.pipeline.get_or_generate_image",
        lambda *a, **k: Path("x"),
    )
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    parsed_lines = [LyricLine(words=[ChordWord(word="hi")])]
    (work_dir / "parsed_tab.json").write_text(
        json.dumps(_parsed_to_dict(parsed_lines, [])), encoding="utf-8"
    )

    audio_path = Path("songs/original.mp3")
    run_pipeline(audio_path, Path("tab.pdf"), work_dir, "t", start_stage="align")

    expected_vocals_path = work_dir / "htdemucs" / "original" / "vocals.wav"
    assert captured["load_path"] == str(expected_vocals_path)
    assert captured["align_vocals_path"] == expected_vocals_path


def test_run_pipeline_computes_instrumental_chords_from_gaps(tmp_path, monkeypatch):
    import torch

    monkeypatch.setattr(
        "lyricvideo.pipeline.torchaudio.load",
        lambda path: (torch.zeros(1, 16000 * 10), 16000),
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.align_words",
        lambda vocals_path, words: [(3.0, 3.5)],
    )
    monkeypatch.setattr(
        "lyricvideo.pipeline.combine_alignment",
        lambda parsed_lines, word_times, audio_duration: [
            LyricLine(words=[ChordWord(word="hi", start_time=3.0, end_time=3.5)], start_time=3.0, end_time=3.5)
        ],
    )

    captured_detect_args = {}

    def fake_detect(instrumental_stem_path, gap_start, gap_end, num_chords):
        captured_detect_args["args"] = (gap_start, gap_end, num_chords)
        step = (gap_end - gap_start) / num_chords
        return [(gap_start + i * step, gap_start + (i + 1) * step) for i in range(num_chords)]

    monkeypatch.setattr("lyricvideo.pipeline.detect_chord_change_times", fake_detect)
    monkeypatch.setattr("lyricvideo.pipeline.summarize_song_gist", lambda *a, **k: "a fake song gist")
    monkeypatch.setattr("lyricvideo.pipeline.get_or_generate_image", lambda *a, **k: Path("x"))
    monkeypatch.setattr("lyricvideo.pipeline.assemble_video", lambda *a, **k: None)
    monkeypatch.setattr("lyricvideo.pipeline.anthropic", type("M", (), {"Anthropic": lambda: object()}))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    parsed_lines = [LyricLine(words=[ChordWord(word="hi")])]
    from lyricvideo.models import InstrumentalBlock

    instrumental_blocks = [InstrumentalBlock(chords=["Em7", "G"], before_line_index=0)]
    (work_dir / "parsed_tab.json").write_text(
        json.dumps(_parsed_to_dict(parsed_lines, instrumental_blocks)), encoding="utf-8"
    )

    run_pipeline(Path("songs/original.mp3"), Path("tab.pdf"), work_dir, "t", start_stage="align")

    # gap is [0.0 (intro), 3.0 (first line's start)) since before_line_index == 0
    assert captured_detect_args["args"] == (0.0, 3.0, 2)

    saved_song = json.loads((work_dir / "lyrics_timed.json").read_text(encoding="utf-8"))
    assert len(saved_song["instrumental_chords"]) == 2
    assert saved_song["instrumental_chords"][0]["chord"] == "Em7"
    assert saved_song["instrumental_chords"][1]["chord"] == "G"
