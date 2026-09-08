# Update Available Feature — Design

**Date:** 2026-09-08
**Status:** Approved by owner, ready for implementation planning

## Purpose

LyricVideoGen is a local desktop Tkinter program (no dashboard, no server) that the
owner runs directly from its own git clone at `/home/doug/LyricVideoGen`. The owner
wants version numbers assigned to real revisions, and a way for the running program
itself to notice a newer version exists and let them apply it — the same experience
AITrading's dashboard gives ("Update Available" banner → Apply → new version running),
adapted for a program that's launched, used, and closed rather than always running.

This is explicitly modeled on AITrading's existing Update Available feature
(`src/update/{version,release_client,apply}.py`, `scripts/cut_release.sh`,
`/api/update-status` + `/api/apply-update` in `web/app.py`). Key differences from that
design, both deliberate owner decisions:

1. AITrading's production copy is *not* a git clone (it's a plain deployed directory on
   a separate Hetzner box), so it fetches packaged release archives from a separate
   public `AITrading-releases` repo rather than `git pull`-ing. LyricVideoGen's run copy
   *is* the dev git clone — a plain `git pull` would technically work — but the owner
   chose to mirror the releases-repo/packaged-archive pattern anyway, for consistency
   with the sibling trading projects and so this mechanism keeps working unchanged if
   LyricVideoGen is ever run from a second, non-dev machine later.
2. AITrading tags releases `critical`/`routine` severity. LyricVideoGen skips that
   distinction entirely — one plain "update available" notice. The owner is the only
   person who ever applies an update here, so urgency tiering buys nothing.
3. AITrading checks for updates continuously in the background (always-on server).
   LyricVideoGen checks once, on GUI launch, since it's a short-lived desktop session.

## Scope

**In scope:**
1. A `VERSION` file at the repo root, bumped only when an update is actually applied to
   a given checkout (never bumped automatically per-commit).
2. A new public, unlisted GitHub repo `flyguy91355/LyricVideoGen-releases` holding
   packaged release archives + plain-text release notes — no source code, no auth
   needed to read it (same shape as `AITrading-releases`).
3. A `lyricvideo/update/` package: pure version comparison, a GitHub Releases API
   client, and the download/extract/allow-listed-copy apply mechanics — ported from
   AITrading's `src/update/` and adapted to LyricVideoGen's own directory layout and
   trimmed feature set (no severity, no version-history endpoint).
4. GUI integration in `lyricvideo/gui.py`: window title shows the current version; a
   background check on launch; a small clickable "Update available" banner; a dialog
   showing the release notes with an "Apply Update" button; a "Relaunch Now" button
   after a successful apply.
5. `scripts/cut_release.sh <version-tag> <notes-file>` — pushes a fresh snapshot of the
   allow-listed paths to the releases repo and cuts a GitHub release there.

**Out of scope (explicitly deferred/rejected by the owner):**
- Critical/routine severity tiering.
- Periodic background re-checking while the GUI stays open — launch-time only.
- A manual "Check for Updates" button.
- A version-history / About panel listing past releases (AITrading has one; not asked
  for here).
- Automatic/scheduled release cutting — `cut_release.sh` is run by hand, same as
  AITrading's.

## Versioning

`VERSION` at the repo root, plain text (e.g. `v1.0.0`), read/written by
`lyricvideo/update/version.py`. This file means "the version last applied to *this*
checkout" — it is bootstrapped once (`v1.0.0`) and thereafter changes only via a
successful Apply Update, never hand-edited and never touched by ordinary development
commits. This exactly matches AITrading's own VERSION semantics: the owner's dev
checkout doesn't self-update by editing code, so its VERSION file only moves when
Apply Update actually runs.

`lyricvideo/update/version.py` is copied near-verbatim from
`AITrading/src/update/version.py`:
- `parse_version(tag) -> tuple[int, ...]` — tolerant of a `v` prefix and a pre-release/
  build suffix (`v1.5.0-rc1` parses the same as `v1.5.0`).
- `is_newer(current, latest) -> bool` — tuple comparison, zero-padded to equal length.
- `read_local_version(path) -> str | None` / `write_local_version(path, version)`.

No behavior changes from the AITrading version — this logic is generic and already
hardened against the real edge cases (GitHub #129, unparseable tags).

## Releases repo and release client

New repo: `flyguy91355/LyricVideoGen-releases`, public, unlisted, created the same way
as `AITrading-releases` (`gh repo create ... --public`). Holds nothing but a synced
snapshot of allow-listed source paths (see Apply Mechanics below) and tagged GitHub
Releases with plain-text notes as the release body — no `severity:` line, unlike
AITrading, since severity tiering is out of scope.

`lyricvideo/update/release_client.py`, adapted from AITrading's:
- `RELEASES_REPO = "flyguy91355/LyricVideoGen-releases"` — a module constant, not read
  from a config file (LyricVideoGen has no `config/settings.yaml` equivalent; a single
  hardcoded repo name is the right amount of configurability for a single-owner tool).
- `fetch_latest_release(repo=RELEASES_REPO, http_get=None) -> dict` returning
  `{"tag_name", "notes", "download_url"}` — same `_validated_tag()` guard against a
  malformed/malicious tag reaching a download URL, same `github.com/.../archive/refs/
  tags/....tar.gz` direct-archive URL (bypasses the API's tighter unauthenticated rate
  limit). No `parse_release_notes()` severity split — the whole release body is the
  notes, verbatim.
- No `fetch_recent_releases()` — not needed without a version-history panel.

## Apply mechanics

`lyricvideo/update/apply.py`, adapted from AITrading's `apply.py` — same generic
`extract_release_archive()`, `copy_updatable_files()`, `_safe_destination()` (symlink-
escape guard), and `requirements_changed()` functions, unchanged, since none of that
logic is AITrading-specific. Only the path lists change:

```python
ALLOWED_PATH_PREFIXES = (
    "lyricvideo/",
    "tests/",
    "docs/",
    "requirements.txt",
    "CLAUDE.md",
)
DENIED_PATH_PREFIXES = (
    ".env",
    "songs/",
    "work/",
    ".venv/",
)
DENIED_FILENAME_PREFIXES = (".env",)
```

Plus the same bare-top-level-`*.py`/`*.sh` fallback rule (catches `run_lyricvideogen.sh`
and any future top-level script without needing to enumerate them). Rationale for the
deny-list, same threat model as AITrading's: a release archive is downloaded and
unpacked automatically, so even though it's LyricVideoGen's own releases repo, a
corrupted or tampered archive must never be able to overwrite the owner's credentials
(`.env`), input songs (`songs/`), work-in-progress renders (`work/`), or the managed
virtualenv (`.venv/` — dependencies are installed via pip, never file-copied).

`is_path_updatable()` itself (traversal/absolute-path rejection → deny-list →
allow-list → bare-`.py`/`.sh` fallback) is copied unchanged from AITrading's version;
its logic doesn't reference any AITrading-specific path.

## GUI integration (`lyricvideo/gui.py`)

- **Window title** includes the current version, e.g. `LyricVideoGen v1.0.0`, read via
  `read_local_version` at startup.
- **Launch-time check**: `LyricVideoGUI.__init__` starts a daemon thread that calls
  `fetch_latest_release()` and, on success, compares against the local VERSION via
  `is_newer()`. The result is pushed onto the same `queue.Queue` + `root.after`-driven
  poll loop the GUI already uses for pipeline progress (`_poll_queue`), so no new
  cross-thread mechanism is introduced. A fetch failure (offline, GitHub hiccup) is
  silently swallowed — the GUI must never error or block on this check; it just doesn't
  show a banner that launch.
- **Banner**: if an update is available, a thin clickable label appears at the top of
  the main window (e.g. `Update available: v1.1.0 — click for details`). Clicking opens
  a `Toplevel` dialog showing the release notes (plain text, scrollable if long) and two
  buttons: **Apply Update** and **Close**.
- **Applying**: clicking Apply Update asks for confirmation
  (`messagebox.askyesno`), then runs the same worker-thread + queue pattern used for
  `run_pipeline`: download the archive → extract → diff `requirements.txt` → if changed,
  `sys.executable -m pip install -r requirements.txt` in the venv → `copy_updatable_files`
  into the repo root → `write_local_version`. Each stage posts a short status string to
  the dialog (reusing the existing log-widget append helper). Any exception at any stage
  aborts before the next one runs and shows the error in the dialog — no partial-apply
  cleanup needed since, like AITrading, `copy_updatable_files` only runs after a
  successful pip install, and pip install only runs after a successful download+extract.
- **Relaunch Now**: on success, the dialog swaps its buttons for a single **Relaunch
  Now** button. Clicking it spawns a fresh process (`subprocess.Popen([sys.executable,
  "-m", "lyricvideo.gui"], cwd=<repo root>)`, using the same venv interpreter this
  process is already running under) and then closes the current window
  (`root.destroy()`). No packaging/systemd concerns here — it's just a second local
  process replacing the first.

## `scripts/cut_release.sh`

```
Usage: scripts/cut_release.sh <version-tag> <notes-file>
Example: scripts/cut_release.sh v1.1.0 /tmp/release-notes.txt
```

Adapted from AITrading's `cut_release.sh`, minus the severity argument and the
config-file lookup (repo name is hardcoded, matching `release_client.py`'s constant):

1. Clone `flyguy91355/LyricVideoGen-releases` fresh into a temp dir, wipe everything
   except `.git/`.
2. Copy in the current `git ls-files -- lyricvideo/ tests/ docs/ requirements.txt
   CLAUDE.md run_lyricvideogen.sh` snapshot from the source repo.
3. Commit and push to the releases repo only if something actually changed (same
   guard AITrading's script has, after the incident where a stale releases repo got
   tagged as if it were current).
4. `gh release create <tag> --repo flyguy91355/LyricVideoGen-releases --title <tag>
   --notes-file <notes-file>` — release body is the notes file verbatim, no
   `severity:` line prepended.

Does **not** touch the source repo's own `VERSION` file — that file only changes on an
install that actually runs Apply Update, per the Versioning section above.

## Error handling

Matches AITrading's stated philosophy: a failure must never corrupt the live checkout
or crash the GUI.
- Launch-time check failure (network, GitHub down, malformed tag): swallowed, no
  banner shown, no dialog, no crash.
- Download failure: aborts before touching anything on disk in the repo.
- Extract failure: aborts before touching anything on disk in the repo (works in a
  `tempfile.TemporaryDirectory()`, same as AITrading).
- pip install failure: aborts before `copy_updatable_files` runs, so the live
  `lyricvideo/` tree and installed dependencies are left exactly as they were — the
  program is still fully usable, just not yet updated.
- No automatic rollback after a successful copy (same as AITrading v1) — not needed
  here since copy only ever runs after pip install already succeeded.

## Testing

`lyricvideo/update/version.py`, `release_client.py` (via an injectable `http_get`, same
pattern as AITrading), and `apply.py`'s pure path-classification/copy functions are all
unit-testable with no GUI or real network involved — port the shape of AITrading's
existing `tests/test_update_*` style tests, adapted to the new allow/deny lists. The
GUI wiring itself (banner, dialog, worker thread) is covered the way `tests/test_gui.py`
already covers `gui.py`'s other worker-thread logic — source-level/AST or direct
function tests rather than driving real Tkinter event loops.
