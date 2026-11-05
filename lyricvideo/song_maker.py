"""Create a Song (owner, 2026-10-31: "create a song thru the program ... female-male, country, rock, and write a short
sentence of what the song is about.. then have it automatically created").

Claude writes a title, key, tempo, chords and section-tagged lyrics from the owner's sentence; MiniMax Music 2.6 on
Replicate (about $0.15-0.20 a song) sings them in the band's sound. The song gets its own work folder holding the audio
(tagged with title and band), its plain lyrics beside it (fetch_lyrics' sidecar), AI_GENERATED.txt and ai_song.json; the
GUI then runs the normal pipeline on it. Every act is a BAND from the roster (~/PlayAlongVideoProductionArtists/
roster.json): MiniMax cannot repeat a voice, so a band name makes a different singer sound like another member. The band is
the roster's pick for the voice and style (owner: "Auto-pick, can change"), a new name the owner types ("I want to be able
to pick the band name as well"; the GUI asks first when MusicBrainz knows it), or a new name from Claude that MusicBrainz
does not know."""

from __future__ import annotations

import json
import random
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from .ai_generated import AI_SONG_FILE, mark_ai_generated
from .key_estimate import parse_key
from .models import atomic_write_text

ROSTER_PATH = Path.home() / "PlayAlongVideoProductionArtists" / "roster.json"
SCHEDULE_FILE = Path.home() / ".playalongvideoproduction" / "scheduled_songs.json"   # today's scheduled songs
MAX_SCHEDULED_FAILURES = 3             # a day's scheduled songs stop after this many failed tries (no endless spend)
MUSIC_MODEL = "minimax/music-2.6"
MUSIC_TIMEOUT_SECONDS = 900.0          # a song took about 150 s (2026-10-30)
MAX_LYRICS_CHARS = 3500                # MiniMax's own limits (Replicate's input schema, 2026-10-31)
MAX_PROMPT_CHARS = 2000
NEW_BAND = "New band (Claude names it)"
VOICES = ("Female", "Male")
# Each style's sound, for a band that has none of its own (a new one Claude names gets its own as well).
STYLES = {
    "Acoustic": "Acoustic guitar-led song, warm and intimate, light percussion.",
    "Folk": "Folk song, strummed and fingerpicked acoustic guitars, soft harmonies.",
    "Soft rock": "Soft rock, clean electric and acoustic guitars, smooth and melodic.",
    "Country": "Country song, acoustic and electric guitars, steel guitar, warm and heartfelt.",
    "Country rock": "Country rock, twangy electric guitars, driving drums, big singalong chorus.",
    "Southern rock": "Southern rock, gritty electric guitars, swaggering groove.",
    "Classic rock": "Classic rock, crunchy electric guitars, steady driving beat.",
    "Hard rock": "Hard rock, crunchy overdriven guitars strumming full chords and power chords, hard-hitting drums.",
    "Blues rock": "Blues rock, gritty electric guitar, shuffle groove.",
    "Pop rock": "Pop rock, bright guitars, catchy hook, upbeat.",
    "Alternative rock": "Alternative rock, layered electric guitars, dynamic verses and big choruses.",
    "80s arena rock": "1980s arena rock, big drums, soaring guitars, anthemic chorus.",
    "Bluegrass": "Bluegrass and Americana: acoustic guitar, banjo, mandolin, fiddle, upright bass.",
    "Indie pop": "Indie pop, jangly guitars, bright and catchy.",
}
_ATTEMPTS = 3
_SECTION = re.compile(r"\[[^\]]*\]")


@dataclass
class SongPlan:
    title: str
    key: str
    bpm: int
    chords: list[str]
    lyrics: str                        # with [Verse]/[Chorus]... tags, as MiniMax takes them


@dataclass
class CreatedSong:
    title: str
    band: str
    work_dir: Path
    audio_path: Path


# ---------------------------------------------------------------- roster

def load_roster(path: Path = ROSTER_PATH) -> dict:
    try:
        roster = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"artists": []}
    if not isinstance(roster, dict) or not isinstance(roster.get("artists"), list):
        raise ValueError(f"{path} is not a band roster (no 'artists' list)")
    return roster


def save_roster(roster: dict, path: Path = ROSTER_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(Path(path), json.dumps(roster, indent=2, ensure_ascii=False) + "\n")


def bands_for(roster: dict, voice: str, style: str) -> list[str]:
    """Roster bands of this style that sing with this voice: a band of this very voice before a mixed-voice one, then active
    before proposed."""
    voice = voice.lower()
    fitting = [a for a in roster["artists"] if a.get("style") == style and a.get("voice") in (voice, "mixed")]
    fitting.sort(key=lambda a: (a.get("voice") != voice, a.get("status") != "active"))
    return [a["name"] for a in fitting]


def band_choices(roster: dict, voice: str, style: str) -> list[str]:
    """The Band dropdown: the fitting bands (the first is the pick), a new band, then every other band."""
    fitting = bands_for(roster, voice, style)
    return fitting + [NEW_BAND] + sorted(a["name"] for a in roster["artists"] if a["name"] not in fitting)


def find_band(roster: dict, name: str) -> dict | None:
    return next((a for a in roster["artists"] if a["name"].lower() == name.lower()), None)


def named_band(name: str, voice: str, style: str) -> dict:
    """A new roster entry for a name the owner typed: the style's sound and the chosen voice."""
    return {
        "name": name, "genre": f"{style} ({voice.lower()} lead)", "style": style, "voice": voice.lower(), "status": "active",
        "kind": "band", "sound": STYLES.get(style, style), "vocal": f"{voice} lead vocal.", "songs": [],
    }


def musicbrainz_has_artist(name: str, http_get=httpx.get, sleep=time.sleep) -> bool:
    """True when MusicBrainz knows an artist by exactly this name (any case). Raises when it cannot be asked: a name is
    never used unchecked."""
    from .identify import HTTP_HEADERS
    last: Exception | None = None
    for attempt in range(3):
        if attempt:
            sleep(2.0)                      # MusicBrainz answers 503 to more than one request a second
        try:
            response = http_get(
                "https://musicbrainz.org/ws/2/artist/", params={"query": f'artist:"{name}"', "fmt": "json", "limit": 25},
                headers=HTTP_HEADERS, timeout=20.0,
            )
            response.raise_for_status()
            return any(a.get("name", "").lower() == name.lower() for a in response.json().get("artists", []))
        except (httpx.HTTPError, ValueError) as e:
            last = e
    raise RuntimeError(f"could not check the band name {name!r} with MusicBrainz: {type(last).__name__}: {last}")


# ---------------------------------------------------------------- Claude

def _ask_json(anthropic_client, prompt: str, max_tokens: int, model: str) -> dict | None:
    response = anthropic_client.messages.create(
        model=model, max_tokens=max_tokens, thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(getattr(block, "text", "") for block in response.content)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def name_new_band(
    anthropic_client, voice: str, style: str, roster: dict, *, is_taken: Callable[[str], bool] = musicbrainz_has_artist,
    model: str = "claude-sonnet-5",
) -> dict:
    """A new roster entry for this voice and style, named by Claude: the first suggestion that is neither on the roster
    nor a real act MusicBrainz knows."""
    known = ", ".join(a["name"] for a in roster["artists"]) or "none"
    prompt = (
        f"Invent a name for a new {style} band with a {voice.lower()} lead singer. It must be a BAND name (not a person's "
        "name), easy to say, and must not be the name of any real band or artist. Our bands already are: "
        f"{known}.\n\nAlso describe the band's sound (instruments, feel; one or two sentences, with guitar chords a "
        "guitarist can play along with) and its lead vocal (one sentence).\n\n"
        'Reply with ONLY JSON: {"names": [8 suggestions, best first], "sound": "...", "vocal": "..."}'
    )
    for _attempt in range(_ATTEMPTS):
        data = _ask_json(anthropic_client, prompt, 600, model)
        names = data.get("names") if data else None
        if not isinstance(names, list):
            continue
        for name in names:
            name = str(name).strip().strip('"')
            if name and find_band(roster, name) is None and not is_taken(name):
                return {
                    "name": name, "genre": f"{style} ({voice.lower()} lead)", "style": style, "voice": voice.lower(),
                    "status": "active", "kind": "band",
                    "sound": str(data.get("sound") or STYLES.get(style, style)).strip(),
                    "vocal": str(data.get("vocal") or f"{voice} lead vocal.").strip(),
                    "songs": [],
                }
    raise RuntimeError(f"Claude suggested no free band name for a {voice.lower()} {style} band; try again.")


_CHORD = re.compile(r"[A-G][#b]?(?:maj|min|m|dim|aug|sus|add)?\d*(?:sus\d|add\d)?(?:/[A-G][#b]?)?")
_CHORD_SEPARATORS = re.compile(r"[\s|,.\-–—>→]+")


def _is_chord_line(line: str) -> bool:
    tokens = [t for t in _CHORD_SEPARATORS.split(line.strip()) if t]
    return bool(tokens) and all(_CHORD.fullmatch(t) for t in tokens)


def drop_chord_lines(tagged_lyrics: str) -> str:
    """The lyrics without lines made only of chord names ("G C D Em" under [Intro]): MiniMax sings every line it gets,
    so it sang the chord names (2026-11-05, Hands Like My Father's)."""
    return "\n".join(line for line in tagged_lyrics.splitlines() if not _is_chord_line(line))


def sung_lines(tagged_lyrics: str) -> list[str]:
    """The lyric lines without the section tags: the sidecar the lyrics check and the video use."""
    return [line.strip() for line in tagged_lyrics.splitlines() if line.strip() and not _SECTION.fullmatch(line.strip())]


def _plan_problem(data: dict | None) -> str:
    if not data:
        return "no JSON"
    if not str(data.get("title") or "").strip():
        return "no title"
    if parse_key(str(data.get("key") or "")) is None:
        return f"not a key: {data.get('key')!r}"
    if not isinstance(data.get("bpm"), (int, float)) or not 60 <= data["bpm"] <= 200:
        return f"tempo {data.get('bpm')!r}"
    if not isinstance(data.get("chords"), list) or not data["chords"]:
        return "no chords"
    lyrics = str(data.get("lyrics") or "")
    if len(lyrics) > MAX_LYRICS_CHARS:
        return f"lyrics {len(lyrics)} characters long"
    if len(sung_lines(lyrics)) < 8:
        return "too few lyric lines"
    return ""


def write_about(anthropic_client, voice: str, style: str, recent: list[str], model: str = "claude-sonnet-5") -> str:
    """One sentence about a new song (a scheduled song has no owner's sentence), unlike the recent ones."""
    used = "\n".join(f"- {a}" for a in recent[-20:]) or "- (none yet)"
    prompt = (
        f"Suggest what a new original {style} song with a {voice.lower()} lead singer could be about: one sentence, "
        "everyday and relatable (love, home, the road, friends, hard times, hope...). Never about a real person, "
        f"place name or brand. Make it clearly different from these earlier songs:\n{used}\n\n"
        'Reply with ONLY JSON: {"about": "..."}'
    )
    for _attempt in range(_ATTEMPTS):
        data = _ask_json(anthropic_client, prompt, 300, model)
        about = str((data or {}).get("about") or "").strip()
        if about and len(about) <= 400:
            return about
    raise RuntimeError("Claude did not suggest what the song is about; try again.")


def plan_scheduled_song(anthropic_client, roster: dict, rng=random) -> tuple[str, str, str, str]:
    """(voice, style, band, about) for a scheduled song (owner, 2026-10-31: random, "we want to add new bands as well"):
    a random style and voice, then one of its roster bands or a new band, each equally likely -- so a style with no band
    gets one, and new bands come less often as a style fills up."""
    style, voice = rng.choice(list(STYLES)), rng.choice(VOICES)
    band = rng.choice(bands_for(roster, voice, style) + [NEW_BAND])
    recent = [s.get("about", "") for a in roster["artists"] for s in a.get("songs", []) if s.get("about")]
    return voice, style, band, write_about(anthropic_client, voice, style, recent)


def _schedule_state(path: Path, today: str) -> dict:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or state.get("date") != today:
        state = {"date": today, "made": 0, "failed": 0}
    return state


def scheduled_song_due(per_day: int, today: str | None = None, path: Path | None = None) -> bool:
    """True while today has made fewer than `per_day` scheduled songs and has not failed MAX_SCHEDULED_FAILURES times."""
    if per_day <= 0:
        return False
    state = _schedule_state(path or SCHEDULE_FILE, today or datetime.now().date().isoformat())
    return state.get("made", 0) < per_day and state.get("failed", 0) < MAX_SCHEDULED_FAILURES


def record_scheduled_song(made: bool, today: str | None = None, path: Path | None = None) -> None:
    path = Path(path or SCHEDULE_FILE)
    state = _schedule_state(path, today or datetime.now().date().isoformat())
    state["made" if made else "failed"] = state.get("made" if made else "failed", 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state))


def write_song(anthropic_client, voice: str, style: str, about: str, band: dict, model: str = "claude-sonnet-5") -> SongPlan:
    prompt = (
        f"Write an original {style} song for the band {band['name']}, sung by a {voice.lower()} lead singer.\n"
        f"What it is about: {about.strip()}\n\n"
        "Rules:\n"
        "- A fresh title, and a key, tempo and 3-5 chords a guitarist can play along with (a common guitar key).\n"
        "- Lyrics with section tags on their own lines: [Intro], [Verse], [Pre Chorus], [Chorus], [Bridge], [Interlude], "
        "[Solo], [Outro]. Shape: Intro, Verse, Chorus, Verse, Chorus, Bridge, Chorus, Outro (a Pre Chorus is fine). An "
        "instrumental section is just its tag. About 3 minutes long.\n"
        "- Never write chord names in the lyrics: every line under a tag is sung, so chords go only in \"chords\".\n"
        "- Short lines (at most 9 words), plain words that are easy to sing and to understand.\n"
        "- Write every repeat out in full: never '(x2)', 'Repeat chorus' or lines in parentheses (no backing vocals).\n"
        f"- Under 2,500 characters in all. Never name a real artist, song or brand, never copy real lyrics.\n\n"
        'Reply with ONLY JSON: {"title": "...", "key": "E minor", "bpm": 120, "chords": ["Em", "C", "G", "D"], '
        '"lyrics": "[Intro]\\n\\n[Verse]\\n..."}'
    )
    problem = ""
    for _attempt in range(_ATTEMPTS):
        data = _ask_json(anthropic_client, prompt, 3000, model)
        if isinstance(data, dict) and isinstance(data.get("lyrics"), str):
            data["lyrics"] = drop_chord_lines(data["lyrics"])
        problem = _plan_problem(data)
        if not problem:
            return SongPlan(
                title=str(data["title"]).strip().strip('"'), key=str(data["key"]).strip(), bpm=int(round(data["bpm"])),
                chords=[str(c).strip() for c in data["chords"]], lyrics=str(data["lyrics"]).strip(),
            )
    raise RuntimeError(f"Claude's song did not come back usable ({problem}); try again.")


def music_prompt(voice: str, style: str, band: dict, plan: SongPlan) -> str:
    """MiniMax's style prompt: the band's own vocal and sound, the plan's tempo, key and chords."""
    band_voice = band.get("voice", "")
    if band_voice == voice.lower():
        vocal = band.get("vocal") or f"{voice} lead vocal."
    elif band_voice == "mixed":
        vocal = f"{band.get('vocal', '')} {voice} lead.".strip()
    else:
        vocal = f"{voice} lead vocal."                 # the owner chose a band of the other voice for this song
    sound = band.get("sound") or STYLES.get(style, style)
    prompt = (
        f"{style} song by a {voice.lower()}-fronted band. {vocal} {sound} Tempo around {plan.bpm} BPM, key of {plan.key}, "
        f"simple chord progression ({', '.join(plan.chords)}) that a guitarist can play along with. Every sung word easy "
        "to understand."
    )
    return prompt[:MAX_PROMPT_CHARS]


# ---------------------------------------------------------------- music

def generate_music(prompt: str, tagged_lyrics: str, out_path: Path, replicate_token: str, http_client=httpx) -> Path:
    from .imagery import _atomic_write_bytes, _create_prediction, _download_output, _poll_prediction
    data = _create_prediction(http_client, replicate_token, MUSIC_MODEL, prompt, inputs={
        "prompt": prompt, "lyrics": tagged_lyrics, "audio_format": "mp3", "sample_rate": 44100, "bitrate": 256000,
    })
    urls = data.get("urls") or {}
    status, output = _poll_prediction(
        http_client, replicate_token, urls["get"], data["status"], data.get("output"), urls.get("cancel"),
        timeout_seconds=MUSIC_TIMEOUT_SECONDS,
    )
    if status != "succeeded":
        raise RuntimeError(f"MiniMax could not make the song (Replicate status {status!r}).")
    url = output[0] if isinstance(output, list) else output
    _atomic_write_bytes(out_path, _download_output(http_client, str(url), what="song"))
    return out_path


def _tag_audio(path: Path, title: str, band: str) -> None:
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError
    from mutagen.mp3 import MP3
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        audio = MP3(path)
        audio.add_tags()
        audio.save()
        tags = EasyID3(path)
    tags["title"] = title
    tags["artist"] = band
    tags.save()


def new_song_folder(work_root: Path, title: str) -> Path:
    from .pipeline import slugify
    base = slugify(title)
    folder, n = Path(work_root) / base, 2
    while folder.exists():
        folder, n = Path(work_root) / f"{base}-{n}", n + 1
    return folder


def create_song(
    work_root: Path, voice: str, style: str, about: str, band_name: str, anthropic_client, replicate_token: str, *,
    roster_path: Path = ROSTER_PATH, is_taken: Callable[[str], bool] = musicbrainz_has_artist, http_client=httpx,
    scheduled: bool = False,
) -> CreatedSong:
    """Writes and makes the song into a new work folder (nothing rendered yet). The folder is removed again if the song
    could not be made. The roster records the band (a new one is added) and the song."""
    if not about.strip():
        raise ValueError("Say in a sentence what the song is about.")
    if not replicate_token:
        raise RuntimeError("REPLICATE_API_TOKEN is not set -- add it to the .env file at the repo root.")
    band_name = band_name.strip()
    if not band_name:
        raise ValueError("Pick a band or type a band name.")
    roster = load_roster(roster_path)
    band = None if band_name == NEW_BAND else find_band(roster, band_name)
    if band is None:
        if band_name == NEW_BAND:
            band = name_new_band(anthropic_client, voice, style, roster, is_taken=is_taken)
        else:
            band = named_band(band_name, voice, style)
        roster["artists"].append(band)
        print(f"New band: {band['name']} ({band['genre']}).")
    print(f"Writing the song for {band['name']}...")
    plan = write_song(anthropic_client, voice, style, about, band)
    prompt = music_prompt(voice, style, band, plan)
    print(f'"{plan.title}" -- {plan.key}, {plan.bpm} BPM, chords {", ".join(plan.chords)}.')

    work_dir = new_song_folder(work_root, plan.title)
    work_dir.mkdir(parents=True)
    try:
        # The record comes first: from now on the song waits for the owner to watch it (ai_generated.created_song).
        record = {
            "title": plan.title, "band": band["name"], "voice": voice, "style": style, "about": about.strip(),
            "key": plan.key, "bpm": plan.bpm, "chords": plan.chords, "prompt": prompt, "lyrics": plan.lyrics,
            "generator": f"{MUSIC_MODEL} (Replicate)", "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scheduled": scheduled,
        }
        atomic_write_text(work_dir / AI_SONG_FILE, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        mark_ai_generated(
            work_dir, f"Original AI song by {band['name']}: music and vocals made with MiniMax Music 2.6 (Replicate), "
            "lyrics written by Claude.",
        )
        file_stem = re.sub(r'[<>:"/\\|?*]', "", plan.title).strip() or work_dir.name
        audio_path = work_dir / f"{file_stem}.mp3"
        audio_path.with_suffix(".txt").write_text("\n".join(sung_lines(plan.lyrics)) + "\n", encoding="utf-8")
        print("MiniMax is making the song (about 2-3 minutes)...")
        generate_music(prompt, plan.lyrics, audio_path, replicate_token, http_client=http_client)
        _tag_audio(audio_path, plan.title, band["name"])
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    band["status"] = "active"
    band.setdefault("songs", []).append({
        "title": plan.title, "work_dir": f"work/{work_dir.name}", "about": about.strip(), "prompt": prompt,
        "generator": f"{MUSIC_MODEL} (Replicate)", "created": record["created"], "scheduled": scheduled,
    })
    save_roster(roster, roster_path)
    print(f"Song made: {audio_path}")
    return CreatedSong(title=plan.title, band=band["name"], work_dir=work_dir, audio_path=audio_path)
