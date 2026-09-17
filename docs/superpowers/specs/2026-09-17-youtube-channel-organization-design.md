# YouTube Channel Organization (Playlists + Engagement Comment) — Design

**Date:** 2026-09-17
**Status:** Approved by owner, ready for implementation planning

## Purpose

Owner wants the channel organized into playlists (so viewers can binge a
whole artist or a whole genre), and wants a standing engagement comment
drafted on every video, with as much of this automated by the program as
YouTube's own API allows. Two of the three ideas the owner asked about
(pinning a comment, and end-screen/card links between videos) turned out to
have **no YouTube Data API v3 support at all** — confirmed against Google's
own current API reference, not assumed from memory. Those pieces are
explicitly out of scope for this design; see below.

## Scope

**In scope:**
1. Three kinds of playlists, created lazily (only when a song actually needs
   one, never pre-created):
   - One **"All"** playlist containing every uploaded video.
   - One playlist **per artist**, exact artist string as recorded in that
     song's `song_info.json` (no normalization/merging across
     differently-credited names for what might be the same act — see
     "Artist name handling" below).
   - One playlist **per genre**, genre chosen by Claude from a shared,
     growing list (see "Genre classification" below). A multi-artist credit
     (e.g. "Bryan Adams, Sting, Rod Stewart") adds the video to every listed
     artist's playlist.
2. One Claude-drafted **engagement comment** per video (asking viewers which
   instrument they're playing along with, or what they'd like to see next),
   queued for the owner's review/approval before it ever posts — same
   Approve/Dismiss pattern already used for comment replies. **Not**
   automated: actually pinning it in Studio, since the API has no pin
   endpoint at all.
3. Fully automatic on every future upload — wired into `schedule_upload()`,
   the single existing upload code path, no new owner action required beyond
   the existing comment-approval click.
4. A one-time, re-runnable backfill script that applies all of the above to
   the 67 already-uploaded videos.

**Out of scope (API cannot do this, confirmed against Google's current
YouTube Data API v3 reference docs):**
- Actually pinning a comment. No field, method, or endpoint exists for it
  anywhere in the API. Posting the comment is automated; pinning stays a
  manual Studio click, always.
- End screens and info cards (the clickable video-to-video links in the last
  seconds of a video, or during playback). The API exposes no resource for
  these at all — not read, not write. Nothing here can move that forward;
  it's 100% manual in Studio and isn't attempted in this design.
- Retroactively editing genre onto `song_info.json` for a song that's never
  been uploaded to YouTube at all (nothing to add it to a playlist for yet;
  classification happens at upload/backfill time, not at render time).

## Artist name handling

Different songs by what's arguably the same act sometimes have different
`artist` strings already on disk (e.g. `"Bob Seger"` vs `"Bob Seger and the
Silver Bullet Band"` vs `"Bob Seger & The Silver Bullet Band"`; `"Neil
Young"` vs `"Neil Young With Crazy Horse"`). This design uses the artist
string **exactly as recorded**, with zero fuzzy matching or normalization —
deliberately, since guessing "these are probably the same act" risks
wrongly merging two different acts, which is worse than a few
near-duplicate playlists. Owner's own call: merge manually in YouTube
Studio later if wanted (playlists support merging by moving items and
deleting the extra one). Nothing in this design prevents that later
cleanup.

## Genre classification

Genre is **not** a fixed enum. It's a shared, growing list, seeded with:

Classic Rock, Southern Rock, Hard Rock / Glam, Progressive Rock, Alternative
/ Grunge, Singer-Songwriter / Folk Rock, Pop Rock / Soft Rock, 60s Pop Rock
/ British Invasion, Punk, Country, Pop, Hip-Hop / Rap, R&B / Soul, Metal,
Blues, Folk / Americana, Jazz, Electronic / Dance, Latin, Reggae, Christian
/ Gospel

persisted in `~/.playalongvideoproduction/youtube_genres.json`. Every
classification call passes Claude the **current full list** and instructs
it to reuse an existing entry whenever one reasonably fits, and only
propose a new one when the song genuinely doesn't fit anything already
there. A newly-proposed genre is appended to the persisted list so the next
song of that same genre reuses it rather than minting a near-duplicate.
This trades a small amount of naming drift (unavoidable with any
LLM-driven open vocabulary) for never permanently missing a genre the
channel grows into. Since playlists are created lazily, an ever-growing
genre list costs nothing until a song actually lands in a new bucket.

Classification happens once per song (Claude call with title + artist +
full lyrics, same cost profile as the existing description/tags call) and
the result is cached in `song_info.json`'s new `genre` key — computed once,
hand-correctable later, never reclassified on a backfill re-run once set.
`song_info.json` is a plain hand-built dict (not a dataclass round-trip;
`run_identify()` in `pipeline.py` writes it directly and every reader uses
`json.loads(...).get(...)`), so adding `genre` is a targeted
read-existing-dict / set key / write-back inside `organize_video()`, not a
schema migration — a song_info.json from before this feature simply has no
`genre` key until the first time it's organized, exactly like any other
`.get("field", default)` read elsewhere in this codebase.

## Components

**`lyricvideo/youtube.py`** (existing raw-API-wrapper module) gains:
- `create_playlist(client, title, description) -> playlist_id`
- `find_playlist_by_id(client, playlist_id) -> bool` — existence check, same
  self-heal role as the existing `video_exists`.
- `is_video_in_playlist(client, playlist_id, video_id) -> bool` — via
  `playlistItems().list(playlistId=..., videoId=...)`, confirmed supported
  by the current API reference.
- `add_video_to_playlist(client, playlist_id, video_id) -> None` — no-ops
  (via the check above) if already a member, so backfill re-runs are safe.
- `post_top_level_comment(client, video_id, text) -> comment_id` — via
  `commentThreads().insert`. Distinct from the existing `post_reply`, which
  uses `comments().insert` and can only reply to an existing comment; a
  fresh standalone comment needs the thread-insert call instead.

**`lyricvideo/youtube_playlist_state.py`** (new, pure persistence, same
shape as `youtube_state.py`/`youtube_comment_state.py`):
- Loads/saves `~/.playalongvideoproduction/youtube_playlists.json`, mapping
  a key (`"all"`, `"artist:<exact name>"`, `"genre:<exact name>"`) to a
  playlist ID.
- Loads/saves `~/.playalongvideoproduction/youtube_genres.json`, the
  growing flat genre list.

**`lyricvideo/youtube_metadata.py`** (existing) gains:
- `classify_genre(anthropic_client, song_title, artist, full_lyrics,
  known_genres) -> str`
- `draft_engagement_comment(anthropic_client, song_title) -> str`

**`lyricvideo/youtube_comment_state.py`** (existing) gains a second queue
alongside `PendingReply`:
- `PendingComment` (video_id, song_title, draft_text) +
  `load_pending_comments`/`add_pending_comment`/`remove_pending_comment`,
  same file-per-queue pattern as the existing pending-replies file.

**`lyricvideo/youtube_playlists.py`** (new, orchestration — the one place
both the upload hook and the backfill script call into):
- `get_or_create_playlist(client, state, key, title, description) ->
  playlist_id` — checks the local cache first, verifies the cached ID is
  still real via `find_playlist_by_id` (self-heals by recreating if the
  owner deleted it in Studio), creates fresh on a genuine cache miss.
- `organize_video(youtube_client, anthropic_client, work_dir, settings) ->
  None`:
  1. Reads `song_info.json` + `lyrics_timed.json` for this work dir.
  2. Classifies genre if `song_info.json.genre` is still blank; persists it
     back.
  3. Ensures the All playlist, each listed artist's playlist, and the genre
     playlist all exist (via `get_or_create_playlist`), and adds the video
     to each (skipping ones it's already in).
  4. Drafts the engagement comment and queues it as a `PendingComment`,
     unless one is already pending or the video already has a
     `youtube_state.json` marker that a comment was already posted for it
     (new `engagement_comment_posted: bool = False` field on `YoutubeState`,
     set True only when the owner approves it. Defaulted, not required —
     `YoutubeState(**data)` reconstructs fine from any of the 67 existing
     `youtube_state.json` files that predate this field and simply lack the
     key; without a default that call would raise, `load_youtube_state`
     would return `None`, and every already-uploaded song would look
     never-uploaded to the rest of the app. Same reasoning as this
     codebase's existing `load_song` tolerance for legacy fields).

**`lyricvideo/youtube_schedule.py`**: `schedule_upload()` calls
`organize_video()` right after `save_youtube_state()`, wrapped so a
playlist/comment failure is logged and never blocks or fails the upload
itself — the video is the important part; organization is best-effort
on top of it, consistent with how every other post-upload side effect in
this app already fails soft.

**`scripts/backfill_channel_organization.py`** (new, manual/re-runnable,
same convention as `scripts/backfill_support_overlay_description.py`):
iterates every `work/*/youtube_state.json`, skips (logs) any whose video no
longer exists on YouTube (self-heal, e.g. the deleted Ironic upload) or
whose audio/render artifacts are missing, calls `organize_video()` for each
survivor. One song's failure is logged and skipped, never aborts the rest —
same per-item isolation already used by Batch and by comment-checking.

**`lyricvideo/gui.py`**: the existing "YouTube Comments" panel gains a
second collapsible list of pending engagement comments (own Approve/Dismiss
per row, same widget pattern as pending replies). Approve calls
`post_top_level_comment`, marks `engagement_comment_posted = True` in that
song's `YoutubeState`, and shows a one-line reminder that pinning is still a
manual Studio step.

## Data flow summary

```
schedule_upload() / backfill script
        |
        v
organize_video(work_dir)
        |-- classify_genre() ---------> song_info.json["genre"] (cached)
        |-- get_or_create_playlist() -> youtube_playlists.json (cached, self-healing)
        |-- add_video_to_playlist()  -> All / Artist(s) / Genre playlists
        |-- draft_engagement_comment() -> PendingComment queue
                                              |
                                              v
                                   owner clicks Approve in GUI
                                              |
                                              v
                                   post_top_level_comment()
                                   YoutubeState.engagement_comment_posted = True
```

## Error handling

- Every per-song step in the backfill script is isolated in its own
  try/except; one bad song is logged and skipped, never aborts the batch.
- `organize_video()` itself is wrapped by its caller in `schedule_upload()`
  so a playlist/genre/comment-drafting failure never fails the upload that
  triggered it.
- A stale cached playlist ID (owner deleted it directly in Studio)
  self-heals by recreating the playlist, same philosophy as the existing
  `video_exists` self-heal for deleted videos.
- A video already removed from YouTube (like the just-deleted Ironic
  upload) is skipped cleanly by the backfill script rather than erroring.

## Testing

Matches this codebase's existing pattern: every new API-touching function
takes an already-built client as its first argument, so tests inject a fake
client/response and never make a real network call. New coverage needed:
- `create_playlist` / `is_video_in_playlist` / `add_video_to_playlist`
  (including the no-op-when-already-a-member case) / `post_top_level_comment`
  against a fake client.
- `get_or_create_playlist`: cache hit, stale-cache self-heal, genuine
  cache-miss creation — three separate cases.
- `classify_genre` / `draft_engagement_comment`: response parsing, same
  style as the existing `generate_video_metadata`/`draft_comment_reply`
  tests.
- `organize_video` end-to-end against fakes, covering the "already
  organized, re-run is a no-op" idempotency case explicitly, since the
  backfill script's safety depends on it.
- Backfill script: one failing song doesn't stop the rest (isolated
  try/except).
