# YouTube Upload + Channel Management — Design

**Date:** 2026-09-10
**Status:** Approved by owner, ready for implementation planning

## Purpose

Owner wants finished videos to optionally upload straight to their YouTube
channel instead of staying local-only, with the channel side (title,
description, comment replies) managed by the app too — without turning this
desktop app into an always-on server, and without creating duplicate/cluttered
videos on the channel.

## Scope

**In scope:**
1. One-time OAuth connection to the owner's own YouTube channel via the
   YouTube Data API v3, credentials stored locally.
2. Optional auto-upload of a finished video (Generate, Redo, or a batch item),
   gated by a single Settings checkbox, defaulting to Public visibility,
   category "Howto & Style," and NOT made for kids. When the target
   visibility is Public, the video uploads immediately as YouTube-Private
   with a computed future `publishAt` timestamp, spaced at most one every N
   days at a preferred hour (both Settings-tunable, default 2 days / 3 PM
   local time) — research-backed cadence, avoids a batch run dumping several
   videos live on the channel at once (owner's explicit concern), AND lets
   YouTube's own servers do the actual publishing, so it no longer depends
   on this app being open at the right moment at all (confirmed against
   YouTube's real API docs: `publishAt` requires `privacyStatus: "private"`
   at upload time, and YouTube auto-flips it public at that moment — even a
   past `publishAt` just publishes immediately, which is exactly the
   "caught up after a reboot" behavior wanted). Unlisted/Private targets
   have no such "publish later" concept, so those upload immediately with
   that literal status and no scheduling at all.
3. Per-song upload tracking (`youtube_state.json` in that song's work dir) so
   the app knows which of its own uploads exist and can skip auto-re-upload
   for a song it already posted.
4. Claude-written title + description + search tags per upload (one more
   small Claude call, same cost profile as the existing image-prompt calls),
   description ending with an invitation to report errors in the comments.
   No promise of an automatic corrected video.
5. Comment monitoring, scoped ONLY to videos this app uploaded, running only
   while the GUI is open: a periodic background check (every ~20 minutes) plus
   a manual "Check Now" button.
6. For each new comment: one Claude call drafts a reply and flags whether the
   comment looks like an error report. Every draft is queued in a new GUI
   panel for the owner to Approve (posts to YouTube), Edit-then-approve, or
   Dismiss. Nothing ever posts to YouTube without an explicit approve click.
7. A manual "Upload to YouTube" button appears after any Generate/Redo/batch
   item finishes, regardless of the auto-upload setting or whether that song
   was already uploaded — the owner's own deliberate override.

**Out of scope (explicitly, per owner discussion):**
- Any automated "corrected video" mechanism — no automatic re-upload, no
  automatic linking between an old and a new video, no automated reply
  specifically about a correction. Owner was concerned about accumulating
  duplicate videos on the channel and chose to drop this entirely; if a
  correction is ever wanted, the owner decides and acts manually (using the
  manual "Upload to YouTube" button like any other upload).
- Playlists, custom thumbnails, tags/category tuning beyond a sane fixed
  default (YouTube category "Music").
- Monitoring or replying on any video not uploaded by this app (no scanning
  of the owner's whole channel/comment history).
- Deleting or unpublishing videos.
- Any always-on/background-service mode — comment monitoring only runs while
  the GUI process is open, matching how every other feature in this app
  already works (no server, no auto-start).

## Architecture

### New dependencies (`requirements.txt`)
`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib` —
Google's own official libraries for OAuth + the Data API v3, the standard
pairing for exactly this use case.

### New file: `lyricvideo/youtube_auth.py`
Owns the OAuth connection lifecycle, isolated from the API-calling code so
`youtube.py`'s functions can be tested against a fake client without ever
touching real OAuth machinery.

- `CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"` (same
  convention as `settings.py`'s `CONFIG_DIR`).
- `TOKEN_FILE = CREDENTIALS_DIR / "youtube_token.json"` — the stored refresh
  token, written by `google-auth-oauthlib`'s own credential-saving helpers.
- `connect(client_secrets_path: Path) -> Credentials`: runs
  `google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(...).run_local_server()`,
  which opens the owner's browser for one-time consent, then saves the
  resulting credentials (including refresh token) to `TOKEN_FILE`. Called only
  when the owner clicks "Connect to YouTube" in Settings.
- `load_credentials() -> Credentials | None`: reads `TOKEN_FILE` if present,
  refreshes the access token via the stored refresh token if expired, returns
  `None` if never connected or the stored token is unusable (revoked,
  corrupted) — callers treat `None` as "not connected," never raise.
- `get_channel_title(credentials) -> str`: one `channels.list(mine=True)` call,
  used only to show "Connected as: <channel name>" in Settings so the owner
  can confirm they authorized the right account.

### New file: `lyricvideo/youtube.py`
All actual Data API v3 calls, each taking an already-built API client object
(`googleapiclient.discovery.build("youtube", "v3", credentials=...)`) as its
first argument — exactly the same "inject the client, fake it in tests"
pattern this project already uses for `anthropic_client` and `http_client` in
`imagery.py`.

- `upload_video(youtube_client, video_path: Path, title: str, description: str, tags: list[str], privacy: str, publish_at: datetime | None, category_id: str, made_for_kids: bool) -> str`:
  calls `videos.insert` with a resumable `MediaFileUpload`, returns the new
  video's id. `privacy` is one of `"public"`, `"unlisted"`, `"private"`
  (matches the YouTube API's own `status.privacyStatus` values directly, so
  the Settings dropdown's values ARE the API's own enum — no translation
  layer to drift out of sync). `category_id` is YouTube's own numeric
  category id as a string (`"26"` = Howto & Style, the chosen default).
  When `publish_at` is given, it's converted from local time to a UTC
  `YYYY-MM-DDThh:mm:ss.0Z` string and set as `status.publishAt` — per
  YouTube's real documented requirement, this is only valid alongside
  `privacy="private"` (verified against the official `videos.insert` docs);
  callers (see `youtube_schedule.py` below) never pass a `publish_at` with
  any other privacy value.
- `list_new_comments(youtube_client, video_id: str, seen_comment_ids: set[str]) -> list[Comment]`:
  calls `commentThreads.list(videoId=video_id)`, returns only top-level
  comments whose id isn't already in `seen_comment_ids`. `Comment` is a small
  frozen dataclass: `comment_id: str`, `video_id: str`, `author: str`,
  `text: str`, `published_at: str`.
- `post_reply(youtube_client, comment_id: str, text: str) -> None`: calls
  `comments.insert` to reply to a top-level comment thread.

### `lyricvideo/settings.py` additions
```python
youtube_auto_upload: bool = False
youtube_client_secrets_path: str = ""   # path to the downloaded client_secret_*.json
youtube_privacy: str = "public"         # "public" | "unlisted" | "private"
youtube_category_id: str = "26"         # YouTube's own category id -- 26 = "Howto & Style"
youtube_made_for_kids: bool = False      # COPPA declaration, required on every upload
youtube_min_days_between_uploads: int = 2
youtube_preferred_upload_hour: int = 15  # 24h local time (0-23); 3 PM matches
                                          # research on peak engagement windows
```
Seven plain fields, same pattern as every existing `Settings` field — they're
read directly by the scheduling code (`settings.youtube_auto_upload`,
`youtube_privacy`, `youtube_category_id`, `youtube_made_for_kids`,
`youtube_min_days_between_uploads`, `youtube_preferred_upload_hour`), not
through `render_kwargs()`, since none of them are render parameters.

### New file: `lyricvideo/youtube_metadata.py`
- `generate_video_metadata(anthropic_client, song_title: str, full_lyrics: str, model="claude-sonnet-5") -> tuple[str, str, list[str]]`:
  one Claude call, returns `(title, description, tags)`. Prompt supplies the
  real song title and lyrics, asks for a labeled `TITLE:`/`DESCRIPTION:`/
  `TAGS:` three-line reply (comma-separated tags on the last line) — labeled
  lines parsed independent of order, robust against Claude reordering or
  lightly rewording the format. The description always ends with a fixed,
  code-appended sentence (not part of Claude's own output, so its exact
  wording never drifts): `"Spot an error in this video? Let me know in the
  comments!"` Reuses the same `_extract_text` pattern as `imagery.py`
  (module-private there; a small copy here rather than a cross-module
  import, keeping the two Claude-prompt modules independent).

### Per-song state: `youtube_state.json`
Written into the same work dir as `lyrics_timed.json`, one per song:
```json
{"video_id": "dQw4w9WgXcQ", "uploaded_at": "2026-09-10T14:32:00", "title": "..."}
```
- `lyricvideo/youtube_state.py` (new, pure/testable):
  - `load_youtube_state(work_dir: Path) -> YoutubeState | None`
  - `save_youtube_state(work_dir: Path, state: YoutubeState) -> None`
  - `YoutubeState` frozen dataclass: `video_id: str`, `uploaded_at: str`, `title: str`.

### New file: `lyricvideo/youtube_schedule.py`
**Redesigned from an earlier local-queue draft of this spec** after
confirming YouTube's own documented `publishAt` behavior (see
`videos.insert` docs): setting `status.publishAt` requires
`status.privacyStatus = "private"` at upload time, and YouTube's own
servers automatically flip the video public at that moment — including
publishing immediately if `publishAt` is already in the past. This means
YouTube itself can do the actual spacing/publishing, which is strictly
better than a local scheduler: a finished video uploads (and is scheduled)
immediately, so publishing no longer depends on this app being open at the
right moment AT ALL — only the upload step does, and that already happens
right when a video finishes. No local "wait and release later" queue is
needed.

- `NEXT_SLOT_FILE = CREDENTIALS_DIR / "youtube_next_slot.json"` —
  `{"next_slot": "<isoformat, local time>"}`, the next reserved publish
  slot. Updated every time a Public-bound video is scheduled (auto OR
  manual — the manual button also reserves a slot, so a deliberate manual
  upload still counts toward the spacing).
- `compute_next_publish_slot(now: datetime, reserved_slot: datetime | None, min_days_between: int, preferred_hour: int) -> datetime`:
  pure function, the one piece of real scheduling logic and the most
  important thing to unit-test thoroughly.
  ```python
  if reserved_slot is None:
      return now  # nothing scheduled yet -- the first video publishes right away
  candidate = reserved_slot + timedelta(days=min_days_between)
  return candidate.replace(hour=preferred_hour, minute=0, second=0, microsecond=0)
  ```
  Spacing is computed from the last *reserved* slot, not from `now` — so
  scheduling several videos back-to-back (a batch run) still lands them one
  every `min_days_between` days in the order they were scheduled, rather
  than all clustering near `now + min_days_between`. There is no "is it due
  yet" check at all here (unlike the earlier local-queue draft) — every
  call immediately reserves and returns the next slot; the reboot-safety
  this project needs comes entirely from YouTube's own past-`publishAt`
  behavior instead.
- `schedule_upload(youtube_client, anthropic_client, work_dir: Path, settings, now: datetime | None = None, next_slot_path: Path = NEXT_SLOT_FILE) -> str`:
  the single upload code path used by every trigger (auto-enqueue sites AND
  the manual button below — there is no separate "immediate" vs "queued"
  upload function anymore). Resolves `now` to
  `datetime.now().astimezone()` when not given (always timezone-AWARE local
  time — required so the UTC conversion inside `upload_video` is correct
  regardless of the machine's configured timezone; tests pass a fixed aware
  `now` directly). Re-derives everything needed from the song's own
  `lyrics_timed.json` (`load_song(work_dir / "lyrics_timed.json")` for
  title/lyrics, `work_dir / f"{slugify(song.title)}.mp4"` for the video
  file — the same path convention `run_pipeline` itself already uses), then:
  ```python
  title, description, tags = generate_video_metadata(anthropic_client, song.title, full_lyrics)
  if settings.youtube_privacy == "public":
      slot = compute_next_publish_slot(now, load_next_slot(next_slot_path), settings.youtube_min_days_between_uploads, settings.youtube_preferred_upload_hour)
      video_id = upload_video(youtube_client, video_path, title, description, tags,
                               privacy="private", publish_at=slot,
                               category_id=settings.youtube_category_id, made_for_kids=settings.youtube_made_for_kids)
      save_next_slot(slot, next_slot_path)
  else:
      video_id = upload_video(youtube_client, video_path, title, description, tags,
                               privacy=settings.youtube_privacy, publish_at=None,
                               category_id=settings.youtube_category_id, made_for_kids=settings.youtube_made_for_kids)
  save_youtube_state(work_dir, YoutubeState(video_id=video_id, uploaded_at=now.isoformat(), title=title))
  return video_id
  ```
  Unlisted/Private targets have no "publish later" concept on YouTube, so
  they skip the slot machinery entirely and upload immediately with that
  literal status — only a Public-bound video ever reserves a slot or gets a
  `publishAt`. Any exception here propagates to the caller (unlike the
  earlier queue design, there's no local retry-by-requeuing to fall back on
  since nothing is queued anymore) — callers (see `gui.py` integration
  below) catch it, log a warning, and never let an upload failure make an
  otherwise-successful video generation look like it failed.

### `lyricvideo/gui.py` integration
**Settings panel** (`settings_panel.py`) gains a "YouTube" section:
- File picker "Client secrets file" (bound to `youtube_client_secrets_path`,
  same `filedialog.askopenfilename` pattern as the font picker).
- "Connect to YouTube" button: calls `youtube_auth.connect()` in a background
  thread (network + browser wait, must not freeze the GUI thread), then shows
  "Connected as: <channel>" via `get_channel_title()`, or an error dialog on
  failure. Button also doubles as "Reconnect" if already connected.
- Checkbox "Auto-upload finished videos to YouTube" (`youtube_auto_upload`).
- Dropdown "Privacy" with values `["public", "unlisted", "private"]`
  (`youtube_privacy`), defaulting to `"public"`.
- Dropdown "Category" with values mapped to YouTube's own category ids —
  `{"Howto & Style": "26", "Education": "27", "Music": "10"}` — bound to
  `youtube_category_id`, defaulting to `"26"` (Howto & Style).
- Checkbox "Made for kids" (`youtube_made_for_kids`), defaulting to
  unchecked/`False` — a required COPPA declaration on every upload, not
  just a preference.
- Slider/entry "Minimum days between uploads" (`youtube_min_days_between_uploads`,
  default 2) and "Preferred upload hour" (`youtube_preferred_upload_hour`,
  default 15, shown as e.g. "3 PM" via the same `fmt` callback pattern
  `_slider()` already uses elsewhere in this panel).

**Upload trigger, single-song (`_run_worker`)**: after `run_pipeline`
returns `out_path` and before `self._queue.put(("done", str(out_path)))`, if
`self.settings.youtube_auto_upload` is True AND
`youtube_state.load_youtube_state(work_dir)` is `None` (never uploaded
before) AND `youtube_auth.load_credentials()` is not `None` (actually
connected): build the `youtube_client`/`anthropic_client` and call
`youtube_schedule.schedule_upload(youtube_client, anthropic_client, work_dir, settings)`
right there on the worker thread (already off the GUI thread) — this
uploads (and, for Public, schedules) immediately; there is no queue to
defer to anymore. Any exception is caught, logged as a warning into the
existing log console via the `("log", ...)` queue message kind, and never
re-raised — an upload problem must never make an otherwise-successful video
generation look like it failed. Single-song, batch, and Redo all funnel
through this exact same logic with no separate path that could bypass it.

**Upload trigger, batch (`_run_batch_worker`)**: identical logic, applied
per item inside the existing per-item try/except block, after a successful
`run_pipeline` call and before appending to `results["succeeded"]`. A batch
of 5 new songs schedules all 5 back-to-back; `compute_next_publish_slot`
still spaces their actual publish times one every
`youtube_min_days_between_uploads` days — this is precisely the owner's
"not a bunch [live] uploaded together" requirement, now enforced by
YouTube's own publish scheduling rather than a local queue.

**Redo (`_on_redo`, which shares `_run_worker` with Generate — both already
funnel through the same single-song trigger above)**: if
`youtube_state.load_youtube_state(work_dir)` is not `None` (song already
uploaded before), the upload step is skipped for this run even if
`youtube_auto_upload` is checked — this is the owner's explicit choice to
avoid duplicate videos piling up from repeat Redos. This lives in one
shared module-level helper function, `_maybe_upload_to_youtube(work_dir: Path, settings: Settings) -> None`,
called from both `_run_worker` and `_run_batch_worker`, encapsulating "only
if enabled, connected, AND not already uploaded."

**Manual "Upload to YouTube" button**: appears on the single-song
completion state (next to where "Done" status shows) and, for a batch run,
in the completion summary per succeeded item. Always enabled once a video
exists and YouTube is connected, regardless of the auto-upload setting or
prior-upload state — clicking it always performs a fresh
`schedule_upload()` call immediately (the owner's deliberate override, per
the approved design; it's the exact same function the automatic triggers
use, just invoked without the "already uploaded" skip-check).

**New "YouTube" panel** (own section or tab in the right-hand column,
alongside Settings): 
- "Check Now" button — triggers a background worker that, for every
  song directory under `work/` with a `youtube_state.json`, calls
  `list_new_comments()` against that video id, tracking already-seen
  comment ids in `~/.playalongvideoproduction/youtube_seen_comments.json`
  (a flat `{comment_id: true}` map — simplest structure that answers
  "have I already drafted a reply for this one").
- A periodic version of the same check runs via `self.root.after(...)`
  re-scheduling itself every 20 minutes, started once at GUI launch (only
  while the app is open, per the approved design) and skipped entirely if
  YouTube isn't connected. (Uploads themselves no longer need this or any
  other timer — `schedule_upload()` runs immediately at the moment a video
  finishes, per the redesign above; this periodic tick exists solely for
  comment-checking.)
- For each newly-found comment: one Claude call
  (`youtube_metadata.draft_comment_reply(anthropic_client, comment_text, song_title) -> tuple[str, bool]`
  returning `(draft_reply, looks_like_error_report)`) queues a row in the
  panel: comment author/text, the drafted reply (editable text box), an
  "⚠ possible error report" badge when flagged, and Approve/Dismiss buttons.
  Approve calls `post_reply()` with whatever text is currently in the box
  (so an edit-then-approve is just "edit the box, then click Approve" — no
  separate edit mode needed).
- Pending drafts persist to `~/.playalongvideoproduction/youtube_pending_replies.json`
  so they survive an app restart before being acted on.

## Data Flow Summary

```
Generate/Redo/Batch item finishes
  -> run_pipeline() returns out_path
  -> _maybe_upload_to_youtube(work_dir, settings)
       - skip if not youtube_auto_upload, not connected, or already uploaded
       - else: build youtube_client + anthropic_client
               youtube_schedule.schedule_upload(youtube_client, anthropic_client, work_dir, settings)
                 - generate_video_metadata() -> (title, description, tags)
                 - if privacy == "public":
                     slot = compute_next_publish_slot(now, load_next_slot(), min_days_between, preferred_hour)
                     upload_video(..., privacy="private", publish_at=slot)  -- YouTube auto-publishes at `slot`
                     save_next_slot(slot)
                   else:
                     upload_video(..., privacy=settings.youtube_privacy, publish_at=None)  -- immediate, as-is
                 - save_youtube_state(work_dir, ...)
  -> ("done"/"batch_done", ...) queue message as today, log shows the upload result
  -- any exception here is caught + logged as a warning, never re-raised

"Upload to YouTube" button (any time): calls schedule_upload() directly,
skipping only the "already uploaded" check (owner's deliberate override).

Every 20 min (or "Check Now"), while GUI open:
  for each work_dir with youtube_state.json:
    list_new_comments(video_id, seen_ids)
    for each new comment:
      draft_comment_reply() -> (draft, is_error_report)
      queue pending reply row in the YouTube panel
  -> owner reviews panel, Approve posts via post_reply(), Dismiss discards
```

## Testing

- `lyricvideo/youtube.py`, `lyricvideo/youtube_metadata.py`,
  `lyricvideo/youtube_state.py`: real unit tests with a fake YouTube API
  client / fake Anthropic client (same pattern as `test_imagery.py`), no real
  network calls. Covers: `upload_video`'s request body shape (title,
  description, tags, categoryId, selfDeclaredMadeForKids, and — only when
  given — a UTC-converted `publishAt` string alongside `privacyStatus:
  "private"`), comment-id de-duplication logic, state file round-trip,
  three-field (title/description/tags) metadata prompt extraction.
- `lyricvideo/youtube_auth.py`: thin wrapper around Google's own OAuth
  library — tested only for `load_credentials()`'s "no token file yet"/
  "corrupt token file" -> `None` paths (pure, no real OAuth flow invoked in
  tests). The interactive `connect()` flow itself is manually/visually
  verified, matching this project's existing GUI-testing-constraint
  precedent (it opens a real browser).
- `_maybe_upload_to_youtube`'s skip logic (not enabled / not connected /
  already uploaded) is unit-tested directly by injecting fakes for the
  connection check and `youtube_state`, following the same monkeypatch
  style already used throughout `test_pipeline.py` and `test_gui.py`.
- `lyricvideo/youtube_schedule.py` gets the heaviest test coverage of the
  whole feature, since `compute_next_publish_slot` is genuinely
  load-bearing logic (get it wrong and either everything clusters on one
  day, or slots drift indefinitely into the future):
  - Returns `now` unchanged when `reserved_slot is None` (first-ever
    scheduled video publishes right away).
  - Returns `reserved_slot + min_days_between` days, snapped to
    `preferred_hour`, when a slot is already reserved — regardless of what
    `now` is (scheduling several videos back-to-back must space them from
    each other, not from `now`).
  - A chain of several calls, each fed the previous call's own return
    value as `reserved_slot`, produces slots exactly `min_days_between`
    days apart — the concrete "batch of 5 songs" scenario.
  - `schedule_upload`: for `youtube_privacy == "public"`, calls
    `upload_video` with `privacy="private"` and a non-`None` `publish_at`,
    and calls `save_next_slot`; for `"unlisted"`/`"private"`, calls
    `upload_video` with that literal privacy and `publish_at=None`, and
    does NOT touch the next-slot file. Both paths call `save_youtube_state`
    with the returned video id. (All via fake `youtube_client`/
    `anthropic_client` injected the same way `test_imagery.py` fakes
    `anthropic_client`, plus a real temp `lyrics_timed.json` built with
    `save_song` so `load_song` inside `schedule_upload` has something real
    to read.)
- The GUI additions (Settings section, "Connect to YouTube" button, the
  YouTube comment-review panel) stay manually/visually verified — this
  project's established precedent for GUI code.
