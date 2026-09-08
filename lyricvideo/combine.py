from __future__ import annotations

from .models import ChordWord, LyricLine

MONOTONIC_TOLERANCE = 0.05


class AlignmentSanityError(Exception):
    pass


def combine_alignment(
    parsed_lines: list[LyricLine],
    word_times: list[tuple[float, float]],
    audio_duration: float,
) -> list[LyricLine]:
    flat_words = [w for line in parsed_lines for w in line.words]
    if len(flat_words) != len(word_times):
        raise AlignmentSanityError(
            f"word count mismatch: {len(flat_words)} lyric words vs "
            f"{len(word_times)} aligned timestamps"
        )

    idx = 0
    prev_end = -1.0
    timed_lines: list[LyricLine] = []
    for line in parsed_lines:
        new_words: list[ChordWord] = []
        for w in line.words:
            start, end = word_times[idx]
            if start < 0 or end > audio_duration or end < start or start < prev_end - MONOTONIC_TOLERANCE:
                raise AlignmentSanityError(
                    f"invalid timestamp for word {idx} ('{w.word}'): start={start}, "
                    f"end={end}, audio_duration={audio_duration}, prev_end={prev_end}"
                )
            new_words.append(ChordWord(word=w.word, chord=w.chord, start_time=start, end_time=end))
            prev_end = end
            idx += 1
        timed_lines.append(
            LyricLine(words=new_words, start_time=new_words[0].start_time, end_time=new_words[-1].end_time)
        )
    return timed_lines
