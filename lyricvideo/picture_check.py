"""Every background picture is looked at before it goes into a video (owner, 2026-11-11: "cut off hands laying on the
fender", "his neck looks broke"). Newly bought, copied from the shared library or already in the song's folder, a
picture is checked once: a good one is marked good in the library's database by its content id (the same id the
library files it under), so it is never checked again in any song; a bad one is moved to
<library dir>/bad_images/<id>.png with a note, removed from the library, and its line gets a new picture.

The check runs through Claude Code on the owner's Pro plan (`claude -p`), never the paid API: the API key is taken out
of the child's environment (with ANTHROPIC_API_KEY set, Claude Code would bill the API instead). --safe-mode skips the
owner's hooks, plugins, CLAUDE.md, MCP servers and memory, and only the Read tool is offered, so each check stays small.
Lettering is never a reason to reject (owner: re-buying pictures over wording costs too much).

Validated before use on 117 pictures judged by eye (docs/CLAUDE_HISTORY.md, 2026-11-11): Opus caught every armless
hand and the broken neck that Sonnet missed. When the check cannot run (Claude Code missing, not signed in, the Pro
limit reached), PictureCheckUnavailable is raised and the song is held (owner: "Hold the song")."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .image_library import ImageLibrary, content_id

# Owner, 2026-11-11: "start out with opus" -- "we can change the model if we need to". Any Claude Code model alias.
PICTURE_CHECK_MODEL = "opus"
BAD_IMAGES_DIRNAME = "bad_images"
_PARALLEL_CHECKS = 3
_CHECK_TIMEOUT_SECONDS = 300

_SYSTEM = "You inspect pictures for a music video. Read the picture file you are given, then answer in the required JSON."
_CHECK = (
    "Read the picture {name} and decide whether a viewer would see something wrong with a PERSON or BODY in it:\n"
    "- a hand, arm, leg, foot or head that is not attached to a visible body, such as a hand lying on or sticking "
    "out of an object (an arm that clearly runs off the edge of the frame toward its person is fine)\n"
    "- extra, missing, fused or badly twisted fingers or limbs\n"
    "- a pose that looks broken, painful or unreal to a viewer even if a body could just manage it, such as a "
    "head thrown so far back that the face points at the sky and the neck looks snapped; a warped or melted face\n"
    "- see-through or ghostly people, or a body merged into an object\n"
    "- a vehicle or object broken in an impossible way that stands out, such as a wheel floating off a tractor\n"
    "Do NOT count: any writing or lettering, hats or clothes lying around, the shape of steering wheels, levers or "
    "machine parts, artistic style, lighting, blur, or small details nobody would notice. "
    "If there are no people or body parts and nothing stands out as broken, the answer is good."
)
_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdict", "problem"],
           "properties": {"verdict": {"type": "string", "enum": ["good", "bad"]}, "problem": {"type": "string"}}}


class PictureCheckUnavailable(RuntimeError):
    """The check could not run, so the song must wait (it is held, never rendered with unchecked pictures)."""


@dataclass(frozen=True)
class Verdict:
    good: bool
    problem: str = ""


def _claude_executable() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"      # the installer's place; a desktop launch may lack it on PATH
    return str(local) if local.is_file() else None


def _run_claude(args: list[str], cwd: Path) -> str:
    """Runs Claude Code and returns its stdout. Tests replace this one function, so no test ever starts Claude Code."""
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=_CHECK_TIMEOUT_SECONDS)
    return result.stdout


def check_picture(path: Path, model: str = PICTURE_CHECK_MODEL) -> Verdict:
    """One picture, on the owner's Pro plan. Raises PictureCheckUnavailable when no verdict comes back."""
    path = Path(path)
    claude = _claude_executable()
    if claude is None:
        raise PictureCheckUnavailable("Claude Code is not installed (no `claude` command)")
    args = [claude, "-p", "--safe-mode", "--model", model, "--system-prompt", _SYSTEM, "--tools", "Read",
            "--allowedTools", "Read", "--max-turns", "4", "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(_SCHEMA), _CHECK.format(name=path.name)]
    try:
        out = json.loads(_run_claude(args, path.parent) or "{}")
    except subprocess.TimeoutExpired:
        raise PictureCheckUnavailable(f"Claude Code took over {_CHECK_TIMEOUT_SECONDS} s on {path.name}") from None
    except (OSError, ValueError) as e:
        raise PictureCheckUnavailable(f"Claude Code could not be run ({type(e).__name__}: {e})") from None
    answer = out.get("structured_output")
    if out.get("is_error") or not isinstance(answer, dict) or answer.get("verdict") not in ("good", "bad"):
        # e.g. not signed in, or the Pro usage limit reached: Claude Code says why in "result"
        raise PictureCheckUnavailable(str(out.get("result") or "Claude Code gave no answer").strip()[:300])
    return Verdict(good=answer["verdict"] == "good", problem=str(answer.get("problem", "")).strip())


@dataclass
class CheckSummary:
    checked: int = 0
    known_good: int = 0
    bad: int = 0

    def line(self, model: str = PICTURE_CHECK_MODEL) -> str:
        return (f"Picture check (Claude Code, Pro plan, {model}): {self.checked} checked, {self.known_good} already "
                f"known good, {self.bad} bad (moved to {BAD_IMAGES_DIRNAME}).")


def quarantine(path: Path, image_id: str, problem: str, song_title: str, line_text: str, library: ImageLibrary,
               work_dir: Path | None = None) -> Path:
    """Moves a bad picture to <library dir>/bad_images/<id>.png with a note, takes it out of the library (so it can never
    be reused), and removes byte-identical copies from the song's images_backup_* folders (which the images stage would
    otherwise copy straight back in). Returns where it went."""
    bad_dir = library.root / BAD_IMAGES_DIRNAME
    bad_dir.mkdir(parents=True, exist_ok=True)
    target = bad_dir / f"{image_id}.png"
    path = Path(path)
    if target.exists():
        path.unlink(missing_ok=True)
    else:
        shutil.move(str(path), str(target))
    library.remove(image_id)
    for backup in sorted(Path(work_dir).glob("images_backup_*")) if work_dir else []:
        twin = backup / path.name
        if twin.is_file() and content_id(twin) == image_id:
            twin.unlink()
    note = target.with_suffix(".txt")
    note.write_text(f"song: {song_title}\nline: {line_text}\nproblem: {problem}\n"
                    f"checked: {datetime.now().astimezone().isoformat(timespec='seconds')} by {PICTURE_CHECK_MODEL}\n",
                    encoding="utf-8")
    return target


def _by_content(pictures: Iterable[tuple[Path, str]]) -> dict[str, list[tuple[Path, str]]]:
    """content id -> every (picture, line text) holding it: the same picture under several lines is judged once."""
    copies: dict[str, list[tuple[Path, str]]] = {}
    for path, line_text in pictures:
        path = Path(path)
        if path.is_file():
            copies.setdefault(content_id(path), []).append((path, line_text))
    return copies


def _judge(copies: dict[str, list[tuple[Path, str]]], library: ImageLibrary, check: Callable[[Path], Verdict],
           summary: CheckSummary) -> tuple[dict[str, str], PictureCheckUnavailable | None]:
    """(content id -> problem for every bad picture, the first PictureCheckUnavailable). A picture already judged is never
    sent again; every new verdict is recorded in the library, also when another picture could not be checked."""
    bad: dict[str, str] = {}
    todo: list[tuple[Path, str, str]] = []
    for image_id, places in copies.items():
        known = library.check_verdict(image_id)
        if known is not None and known[0] == "good":
            summary.known_good += 1
        elif known is not None:
            bad[image_id] = known[1]
        else:
            todo.append((places[0][0], places[0][1], image_id))

    def one(item):
        try:
            return check(item[0])
        except PictureCheckUnavailable as e:
            return e

    with ThreadPoolExecutor(_PARALLEL_CHECKS) as pool:
        results = list(pool.map(one, todo))
    unavailable: PictureCheckUnavailable | None = None
    for (path, line_text, image_id), result in zip(todo, results):
        if isinstance(result, PictureCheckUnavailable):
            unavailable = unavailable or result
            continue
        summary.checked += 1
        library.record_check(image_id, "good" if result.good else "bad", result.problem, PICTURE_CHECK_MODEL)
        if not result.good:
            bad[image_id] = result.problem
            print(f"Bad picture for {line_text!r}: {result.problem}")
    return bad, unavailable


def check_song_pictures(pictures: Iterable[tuple[Path, str]], song_title: str, work_dir: Path | None = None,
                        library: ImageLibrary | None = None,
                        check: Callable[[Path], Verdict] = check_picture) -> tuple[list[Path], CheckSummary]:
    """Checks each (picture, line text) the video will use and moves every bad one out. Returns the pictures removed
    (their lines need new ones) and the counts. A picture already judged is never sent again: good ones are skipped, a
    known-bad one (say, copied back from an old backup) is moved out. Raises PictureCheckUnavailable (after recording
    every verdict that did come back, and moving out every bad picture found) when any picture could not be checked."""
    own = library is None
    library = library or ImageLibrary()
    summary = CheckSummary()
    removed: list[Path] = []
    try:
        copies = _by_content(pictures)
        bad, unavailable = _judge(copies, library, check, summary)
        for image_id, problem in bad.items():
            (first, line_text), *rest = copies[image_id]
            quarantine(first, image_id, problem, song_title, line_text, library, work_dir)
            for other, _ in rest:
                other.unlink(missing_ok=True)
            removed.extend(p for p, _ in copies[image_id])
            summary.bad += 1
        if unavailable is not None:
            raise unavailable
        return removed, summary
    finally:
        if own:
            library.close()


def bad_pictures_in(pictures: Iterable[tuple[Path, str]], library: ImageLibrary | None = None,
                    check: Callable[[Path], Verdict] = check_picture) -> list[tuple[str, str]]:
    """(line text, problem) for every bad picture among the (picture, line text) of a video that is already made -- known
    from the library or checked now. Nothing is moved: the render that replaces a bad picture (Render Anyway, Rebuild EASY
    version) finds it known-bad and moves it out then (check_song_pictures). When no picture is bad but one could not be
    checked, raises PictureCheckUnavailable (after recording every verdict that did come back)."""
    own = library is None
    library = library or ImageLibrary()
    try:
        copies = _by_content(pictures)
        bad, unavailable = _judge(copies, library, check, CheckSummary())
        if not bad and unavailable is not None:
            raise unavailable
        return [(copies[image_id][0][1], problem) for image_id, problem in bad.items()]
    finally:
        if own:
            library.close()
