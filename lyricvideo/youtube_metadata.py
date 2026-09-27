"""Claude-authored YouTube upload metadata and comment-reply drafts. See
docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations


class MetadataGenError(Exception):
    pass


_METADATA_ATTEMPTS = 3

# YouTube refuses a snippet.title over 100 characters or containing '<' or '>' (400 invalidTitle) -- a refusal that is
# not a quota stop, so without fitting the title first such a song failed on every retry, forever (issue #7 review, F142).
YOUTUBE_TITLE_MAX = 100
PLAY_ALONG_TITLE_SUFFIX = " - (Play Along Lyrics & Chords)"
_GENRE_MAX_CHARS = 60


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise MetadataGenError("Claude response contained no text content")
    return "".join(parts)


def _finished(response) -> bool:
    """Whether Claude finished its answer rather than being cut off (stop_reason "max_tokens") or refusing. A response
    object without a stop_reason (a test fake) counts as finished."""
    return getattr(response, "stop_reason", None) in (None, "end_turn", "stop_sequence")


def _ask_for_fields(anthropic_client, model: str, max_tokens: int, prompt: str, labels: list[str], required: list[str]):
    """Asks Claude (thinking OFF -- Sonnet 5's default adaptive thinking can spend the whole small budget and return no
    text at all; CLAUDE_HISTORY 2026-09-26) for a reply of labelled lines, retrying up to _METADATA_ATTEMPTS times until
    every `required` label came back non-empty in a finished answer. Returns the parsed fields; raises MetadataGenError
    after the last attempt -- never hands back a blank or cut-off field."""
    last_problem = ""
    for _attempt in range(_METADATA_ATTEMPTS):
        response = anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            text = _extract_text(response)
        except MetadataGenError as e:
            last_problem = str(e)
            continue
        if not _finished(response):
            last_problem = f"the reply was cut off (stop_reason={getattr(response, 'stop_reason', None)!r})"
            continue
        fields = _parse_labeled_fields(text, labels)
        missing = [label for label in required if not fields[label].strip()]
        if not missing:
            return fields
        last_problem = f"the reply had no usable {'/'.join(f'{m}:' for m in missing)} line ({text[:100]!r}...)"
    raise MetadataGenError(f"Claude did not return a usable answer after {_METADATA_ATTEMPTS} attempts: {last_problem}")


def _parse_labeled_fields(text: str, labels: list[str]) -> dict[str, str]:
    fields = {label: "" for label in labels}
    for line in text.splitlines():
        stripped = line.strip()
        for label in labels:
            prefix = f"{label}:"
            if stripped.upper().startswith(prefix):
                fields[label] = stripped[len(prefix):].strip()
    return fields


def _clean_title_part(text: str) -> str:
    """No '<' or '>' (YouTube rejects them in a title) and no stray runs of whitespace."""
    return " ".join((text or "").replace("<", "").replace(">", "").split())


def _fit_youtube_title(song_title: str, artist: str, suffix: str, limit: int = YOUTUBE_TITLE_MAX) -> str:
    """`"{title} - {artist}{suffix}"`, fitted to YouTube's title rules. The suffix always stays -- it is what keeps a
    play-along and its EASY CHORDS version apart (owner, 2026-09-23). Too long: the artist goes first, then the song
    title is shortened with an ellipsis. A title that already fits is returned exactly as before."""
    title, known_artist = _clean_title_part(song_title), _clean_title_part(artist)
    if known_artist:
        full = f"{title} - {known_artist}{suffix}"
        if len(full) <= limit:
            return full
    full = f"{title}{suffix}"
    if len(full) <= limit:
        return full
    room = max(0, limit - len(suffix) - 1)          # 1 for the ellipsis
    return f"{title[:room].rstrip()}…{suffix}"


def is_valid_youtube_title(title: str) -> bool:
    """YouTube's own rules for snippet.title: 1-100 characters, no '<' or '>'."""
    return 0 < len(title.strip()) <= YOUTUBE_TITLE_MAX and "<" not in title and ">" not in title


def build_play_along_title(song_title: str, artist: str) -> str:
    """Deterministic YouTube title -- replaces letting Claude phrase the
    title freely, which produced inconsistent wording across uploads (real
    owner complaint, 2026-09-14: "Play Along Lyric + Chord Video" on one
    song, "Lyrics & Chords Play Along" on another, "Play Along Lyrics &
    Chords" on a third -- confirmed by reading every work/*/youtube_state.json
    on disk). Every future upload now gets the exact same pattern, fitted to
    YouTube's 100-character limit (_fit_youtube_title)."""
    return _fit_youtube_title(song_title, artist, PLAY_ALONG_TITLE_SUFFIX)


def easy_chord_title_suffix(capo_fret: int) -> str:
    return f" - (EASY CHORDS Play Along - Capo {capo_fret})"


def build_easy_chord_title(song_title: str, artist: str, capo_fret: int) -> str:
    """Deterministic YouTube title for an EASY CHORD (capo) variant -- same "never Claude-authored" policy
    as build_play_along_title(), with the capo fret called out so this upload is never mistaken for the
    original hard-key one (owner, 2026-09-23: "it must have EASY CHORDS in the title... keeps the versions
    separate"). `song_title` must be the CLEAN original title, not the "EasyChords"-suffixed filename title."""
    return _fit_youtube_title(song_title, artist, easy_chord_title_suffix(capo_fret))


def generate_video_metadata(
    anthropic_client, song_title: str, artist: str, full_lyrics: str, model: str = "claude-sonnet-5",
) -> tuple[str, str, list[str]]:
    # The artist is a known, already-resolved fact (identify.py), never
    # something Claude should have to guess -- stated explicitly when known,
    # and simply omitted (never a fabricated placeholder) when identify.py
    # itself couldn't resolve one.
    known_artist = artist.strip()
    artist_line = f'It is performed by "{known_artist}".\n' if known_artist else ""
    prompt = (
        f'A song titled "{song_title}" has these lyrics:\n\n{full_lyrics}\n\n'
        f"{artist_line}"
        "The lyrics come from an automatic lyrics service and may differ slightly from the recording, so do not "
        "comment on, correct, or question them -- just write the two lines below.\n"
        "Write YouTube upload metadata for a 'play along' lyric+chord video of "
        "this song. Reply with EXACTLY two lines, each prefixed with its label "
        "and nothing else before or after:\n"
        "DESCRIPTION: <a 2-4 sentence description of the song>\n"
        "TAGS: <5-8 relevant search tags, comma-separated>"
    )
    # Real incident, 2026-09-25 ("Blackbird" went up with no description and no tags): Claude sometimes answered
    # with a paragraph instead of the two labelled lines -- which parsed to "" and "" and was uploaded as-is -- and
    # Sonnet 5's default adaptive thinking could spend the whole 300-token budget and return no text at all. So:
    # thinking is off (this is a tiny formatted-output task), a reply without BOTH a description and at least one tag
    # is retried, and after _METADATA_ATTEMPTS the call raises -- a blank description is never handed back.
    last_problem = ""
    for _attempt in range(_METADATA_ATTEMPTS):
        response = anthropic_client.messages.create(
            model=model,
            max_tokens=300,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            text = _extract_text(response)
        except MetadataGenError as e:
            last_problem = str(e)
            continue
        if not _finished(response):
            last_problem = f"the reply was cut off (stop_reason={getattr(response, 'stop_reason', None)!r})"
            continue
        fields = _parse_labeled_fields(text, ["DESCRIPTION", "TAGS"])
        description = fields["DESCRIPTION"].strip()
        tags = [t.strip() for t in fields["TAGS"].split(",") if t.strip()]
        if description and tags:
            return build_play_along_title(song_title, artist), description, tags
        last_problem = f"the reply had no usable DESCRIPTION:/TAGS: lines ({text[:100]!r}...)"
    raise MetadataGenError(
        f"Claude did not return usable YouTube metadata for {song_title!r} after {_METADATA_ATTEMPTS} attempts: "
        f"{last_problem}"
    )


def draft_comment_reply(
    anthropic_client, comment_text: str, song_title: str, model: str = "claude-sonnet-5",
) -> tuple[str, bool]:
    """(reply draft, looks like an error report). Thinking off and retried like generate_video_metadata (issue #7
    review, F084: with default thinking this returned no text 5 of 6 times for one song); raises MetadataGenError --
    never returns a blank draft -- when every attempt fails, so the caller can skip just this comment."""
    prompt = (
        f'Someone left this comment on a "{song_title}" play-along video:\n\n'
        f'"{comment_text}"\n\n'
        "Reply with EXACTLY two lines, each prefixed with its label:\n"
        "IS_ERROR_REPORT: <YES or NO -- is this reporting a mistake in the video, "
        "like wrong chords, sync issues, or wrong lyrics?>\n"
        "REPLY: <a short, friendly, genuine-sounding reply, written as the channel owner>"
    )
    fields = _ask_for_fields(anthropic_client, model, 300, prompt, ["IS_ERROR_REPORT", "REPLY"], ["REPLY"])
    is_error_report = fields["IS_ERROR_REPORT"].strip().upper().startswith("YES")
    return fields["REPLY"].strip(), is_error_report


def classify_genre(
    anthropic_client, song_title: str, artist: str, full_lyrics: str,
    known_genres: list[str], model: str = "claude-sonnet-5",
) -> str:
    """Picks one genre for this song's Genre playlist. The list is shared
    and growing (see youtube_playlist_state.py) -- Claude is told to reuse
    an existing entry whenever one reasonably fits, and only mint a new one
    when the song genuinely doesn't fit anything already there, so the
    channel's genre vocabulary doesn't fragment into near-duplicates.

    Returns "" -- never a guess, never a cut-off fragment -- when no clean
    answer came back (issue #7 review, F085: a thinking-only reply used to
    raise and stop the whole playlist organization, and a reply cut off at
    the old 50-token budget, e.g. "Classic Ro", became a permanent public
    playlist and a new entry in the shared genre list). API errors still
    propagate; organize_video treats a blank genre as "no genre playlist"."""
    known_artist = artist.strip()
    artist_line = f'It is performed by "{known_artist}".\n' if known_artist else ""
    genre_list = "\n".join(f"- {g}" for g in known_genres if g and g.strip())
    prompt = (
        f'A song titled "{song_title}" has these lyrics:\n\n{full_lyrics}\n\n'
        f"{artist_line}"
        "Classify this song's musical genre for a YouTube playlist. Here is the "
        f"list of genres already used on this channel:\n{genre_list}\n\n"
        "If one of these already fits reasonably well, reuse it EXACTLY as written. "
        "Only propose a new genre name if none of them fit. Reply with EXACTLY one line:\n"
        "GENRE: <the genre name>"
    )
    try:
        fields = _ask_for_fields(anthropic_client, model, 100, prompt, ["GENRE"], ["GENRE"])
    except MetadataGenError:
        return ""
    genre = fields["GENRE"].strip()
    if len(genre) > _GENRE_MAX_CHARS or "\n" in genre:
        return ""
    return genre


def draft_engagement_comment(anthropic_client, song_title: str, model: str = "claude-sonnet-5") -> str:
    """The engagement comment draft; thinking off and retried, raises MetadataGenError rather than returning ""."""
    prompt = (
        f'This is a "play along" lyric+chord tutorial video for the song "{song_title}". '
        "Write a short, friendly comment, as the channel owner, to post on the video, "
        "inviting musicians to engage -- e.g. asking which instrument they're playing "
        "along with (guitar, piano, bass, etc.) or what song/chord progression they'd "
        "like to see covered next. Reply with EXACTLY one line:\n"
        "COMMENT: <the comment text>"
    )
    fields = _ask_for_fields(anthropic_client, model, 200, prompt, ["COMMENT"], ["COMMENT"])
    return fields["COMMENT"].strip()
