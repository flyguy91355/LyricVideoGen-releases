from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from lyricvideo.pdf_parse import (
    parse_tab_pdf,
    is_chord_token,
    NoTextLayerError,
    NoChordLyricPairsError,
)


def _make_chord_lyric_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=(400, 200))
    c.setFont("Courier", 12)
    c.drawString(50, 150, "G")
    c.drawString(110, 150, "D")
    c.drawString(50, 130, "hello")
    c.drawString(110, 130, "there")
    c.save()


def test_parse_chord_over_lyric_pdf(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_chord_lyric_pdf(pdf_path)

    lines, instrumental_blocks = parse_tab_pdf(pdf_path)

    assert len(lines) == 1
    words = lines[0].words
    assert [w.word for w in words] == ["hello", "there"]
    assert words[0].chord == "G"
    assert words[1].chord == "D"
    assert instrumental_blocks == []


def test_no_text_layer_raises(tmp_path):
    pdf_path = tmp_path / "scanned.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 200))
    c.rect(10, 10, 380, 180, fill=1)  # a shape, no text at all
    c.save()

    with pytest.raises(NoTextLayerError):
        parse_tab_pdf(pdf_path)


def test_no_chord_lyric_pairs_raises(tmp_path):
    pdf_path = tmp_path / "chords_only.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 200))
    c.setFont("Courier", 12)
    c.drawString(50, 150, "G")
    c.drawString(110, 150, "D")
    c.drawString(50, 130, "Em")
    c.drawString(110, 130, "C")
    c.save()

    with pytest.raises(NoChordLyricPairsError):
        parse_tab_pdf(pdf_path)


def test_prose_only_pdf_produces_lines_with_no_chords(tmp_path):
    pdf_path = tmp_path / "prose.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 200))
    c.setFont("Courier", 12)
    c.drawString(50, 150, "just some ordinary")
    c.drawString(50, 130, "sentence with no chords")
    c.save()

    lines, instrumental_blocks = parse_tab_pdf(pdf_path)

    assert all(w.chord is None for line in lines for w in line.words)
    assert instrumental_blocks == []


def test_leading_instrumental_chord_block_captured_before_first_lyric_line(tmp_path):
    pdf_path = tmp_path / "intro.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 300))
    c.setFont("Courier", 12)
    # a chord-only line (intro progression), then a real chord-over-lyric verse
    c.drawString(50, 250, "Em7")
    c.drawString(110, 250, "G")
    c.drawString(50, 200, "G")
    c.drawString(110, 200, "D")
    c.drawString(50, 180, "hello")
    c.drawString(110, 180, "there")
    c.save()

    lines, instrumental_blocks = parse_tab_pdf(pdf_path)

    assert len(lines) == 1
    assert len(instrumental_blocks) == 1
    assert instrumental_blocks[0].before_line_index == 0
    assert instrumental_blocks[0].chords == ["Em7", "G"]


def test_trailing_instrumental_chord_block_captured_after_last_lyric_line(tmp_path):
    pdf_path = tmp_path / "outro.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(400, 300))
    c.setFont("Courier", 12)
    c.drawString(50, 250, "G")
    c.drawString(110, 250, "D")
    c.drawString(50, 230, "hello")
    c.drawString(110, 230, "there")
    # trailing chord-only line (outro progression) after the verse
    c.drawString(50, 180, "Em7")
    c.drawString(110, 180, "G")
    c.save()

    lines, instrumental_blocks = parse_tab_pdf(pdf_path)

    assert len(lines) == 1
    assert len(instrumental_blocks) == 1
    assert instrumental_blocks[0].before_line_index == 1  # after the only lyric line
    assert instrumental_blocks[0].chords == ["Em7", "G"]


def test_is_chord_token():
    for tok in ["G", "Em7", "C/G", "A#dim", "Asus4", "Bm"]:
        assert is_chord_token(tok)
    for tok in ["hello", "the", "Wish"]:
        assert not is_chord_token(tok)
