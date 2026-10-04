# PlayAlongVideoProduction

Generates synced lyric+chord "play along" videos from nothing but an audio file:
karaoke-style scrolling lyrics (forced-aligned to the real vocal stem) and a
NOW/NEXT/timeline chord bar (chords detected directly from the audio, never a
tab/chord sheet) composited over AI-generated Ken-Burns backgrounds that change
per lyric line -- and per chord during instrumental gaps. `deep_review/` retries
the flagged backlog (below). Specs/plans live in `docs/superpowers/`. History: `docs/CLAUDE_HISTORY.md`.

## Running it

- **Linux and Windows.** Launchers `run_playalongvideoproduction.sh`/`.bat` call the venv's own `python`, never
  `source .venv/bin/activate`; `venv.py`'s `venv_python()` resolves `.venv/bin/python` vs
  `.venv/Scripts/python.exe` -- read every `.venv/bin/python` below that way. `.gitattributes`: `*.sh`/`.githooks/*`
  LF, `*.bat` CRLF.
- **GUI** (`gui.py`, CustomTkinter; desktop icon or the launcher): pick an audio file -- title/artist/lyrics are
  identified and fetched, chords detected; the title field is an optional override (its live YouTube-title preview
  is not fed into it) -- then Generate. Picking another file always clears the identified artist and identifies the
  new one (an identify-filled title is replaced, a typed one kept; a late result for another file is ignored). "New
  Song" resets the form; the work dir falls back to the file name until identify finishes. Left column: form/Generate/Redo/log/progress; right: YouTube status/Connect, "⚙ Settings",
  comment panels. Main window 1600x1000.
  - Startup is lazy: `import lyricvideo.gui` (~0.5 s) loads none of torch, torchaudio, moviepy, anthropic,
    googleapiclient, tensorflow, crema -- gui's `_LazyAnthropic` and `build()`
    wrapper, pipeline's lazy `anthropic` proxy, align's in-function torch import, assemble's `_load_moviepy`.
  - Quit: X (`WM_DELETE_WINDOW` -> `_on_close_window`; tests invoke the registered Tcl callback) and
    Relaunch Now ask first (`_confirm_quit_if_busy`) while a Generate/Redo/Batch, any YouTube upload or an Apply
    Update runs (closing kills it; no resume). `_shut_down` withdraws the window at once, then destroys it; `_closing`
    stops new scans.
  - Work folders (`batch.work_dir_for`, `holds_other_recording(folder, artist, audio_path=None)`): a folder holding
    this very file's copy (same name+size) is this song; else one whose `song_info.json` artist is known and differs
    (`artist_key`; a word-subset is the same act) holds another recording, and the file gets `<title-artist>`
    (`<title-file stem>` if the artist is unknown), then -2, -3... Generate then asks Yes = new folder / No = replace
    (backed up) / Cancel; Yes also backs up a folder that holds this recording, and generating over the same song
    backs up its video and timing first. Generate/Redo refuse up front, naming the path, when the audio is gone.
  - **Batch: Process a Folder** (`batch.py`): every audio file in a folder, in turn. One prompt: Yes = skip done songs
    / No = regenerate all (each backed up like Redo) / Cancel (Esc, X). An erroring file is logged and skipped. In one
    Batch a folder already given to the SAME known artist's file is shared (two rips are never made or uploaded
    twice); any other artist gets its own folder as above. Existing stems -> `BatchItem.resume_stage="fetch_lyrics"`.
    `release_memory()` (gc + Linux malloc_trim) after every song, pass or fail (RSS creep). The folder
    path is never `.strip()`'d (`resolve_existing_folder()`, 9-10); the last folder lives in its own JSON
    (`load_last_batch_folder`/`save_last_batch_folder`) -- not a Settings field: `SettingsPanel.collect()` replaces
    `Settings` wholesale and would reset any field with no widget.
  - `render.py`/`detect_chords.py`/`assemble_video()` take plain keyword args for every Settings value (colors as RGB
    tuples) defaulting to the original values, never importing `settings.py`; `run_pipeline()` alone takes a
    `Settings` and unpacks it (`Settings.render_kwargs()`; an unknown resolution -> 1080p with a warning).
- **CLI** (staged/resumable):
  ```bash
  .venv/bin/python -m lyricvideo.pipeline --audio <path> --work-dir <dir> [--title "<override>"] [--font <ttf>] \
      [--stage identify|separate|fetch_lyrics|align|detect_chords|images|render]
  ```
  `--stage` resumes from artifacts already in `--work-dir`; `run_pipeline()`'s `end_stage` stops early (9-22).
- Needs `ANTHROPIC_API_KEY` and `REPLICATE_API_TOKEN` in `.env` (template `.env.example`). No trading credentials --
  unrelated to AITrading beside it.

## Pipeline stages (`lyricvideo/pipeline.py`, `STAGES`)

1. **identify** (`identify.py`) -- title/artist/duration from tags, the filename, then (artist unknown) lrclib-artist
   consensus + MusicBrainz-by-duration; written atomically to `song_info.json`, with `artists` (MusicBrainz's credited
   names) on a fresh identify. An MP3 with no Xing/VBRI header is measured by decoding (`decoded_duration`). Tag and
   MusicBrainz titles use `text_clean.strip_title_noise` (a leading number is kept: "19-2000"); filename titles use
   `clean_title`, which strips only a leading-zero or 1-2 digit "N. "/"N) " track number ("747 - Song" keeps it).
   `--title` overrides display/filename only (never artist or lyrics search). A resume past identify re-runs it when song_info.json is missing,
   unreadable or has no title.
2. **separate** (`separate.py`) -- Demucs two-stem split on `compute_device()`: `cuda` only when torch sees a GPU AND
   its build has kernels for that compute capability (`cuda_build_supports_device()`, issue #5), else `cpu`, logged;
   `LYRICVIDEO_DEVICE=cpu|cuda` overrides; an auto-picked GPU failure retries once on CPU. Output
   `work_dir/htdemucs/<audio_stem>/{vocals,no_vocals}.wav` is what `--stage` resumes from; missing stems re-run
   Demucs; its output relays through `sys.stdout` (`_run_demucs`). Stems >3% shorter than song_info's duration are
   truncated -- except that for an MP3 with no Xing/VBRI/LAME header (`_length_may_be_a_header_guess`) a
   `decoded_duration` agreeing with the stems within max(3 s, 3%) accepts them and corrects song_info
   (`_stems_are_complete`). A run starting at align or earlier refreshes work_dir's audio copy (atomically) when the
   source was replaced (other size, or other bytes with a new mtime), deleting old stems and transcript.json; a resume
   past align keeps its audio. Redo reads the copy, so Generate from a replaced file.
3. **fetch_lyrics** (`fetch_lyrics.py`, `lyric_audio_match.py`, `transcribe.py`; HISTORY 9-18/19) --
   `fetch_lyric_lines_verified()` tries each source (sidecar `.lrc`/`.txt` beside the audio copy; lrclib
   edition-consensus vote, `vocal_onset.py` tie-breaks; each `syncedlyrics` provider) until one passes, into
   `lyric_lines.json`/`Song`. Pass = matching what faster-whisper HEARS in the vocal stem (medium, VAD off): >=70%
   in-order word coverage, no run of >3 unmatched lines or >12 sung words the lyrics lack (backing vocals ignored).
   The tokenizer (`lyric_audio_match._tokens`, shared by timing_gate, anchors, lyric_reconcile, deep_review) folds
   curly apostrophes and accents. Section markers are not lyrics (`_clean_timed_rows`: "[Verse 1]" rows, "(Chorus)",
   "Chorus:", "(x2)"/"Repeat chorus"; a leading "[..]" tag stripped). Unsung lead/tail credit lines are trimmed per
   candidate before the check (`trim=`, from `pipeline._build_unsung_trim`: line times, Whisper words, vocal
   loudness) only when none of their words were heard. `artist_matches` compares whole words. None passing keeps the
   least-bad match flagged (no auto-upload). Claude then JUDGES unmatched stretches (`lyric_arbiter.py`: recognizer
   failures -> accepted), confirming only if coverage with those ranges excused (`coverage_excusing`) is still >=70%;
   its fix (`lyric_reconcile.py`) is only SAVED as `lyrics_suggested.txt`. Whisper unavailable -> a Claude text check
   (thinking off; an unclear/blank reply or API error is a non-empty concern). LRC timestamps discarded.
   `python -m lyricvideo.verify_lyrics -h` re-checks finished songs: owner-typed lyrics (source "owner") are never
   checked or held (`owner-lyrics`); `--flag` releases old holds a report-only run verified or marked owner-lyrics; a
   failing song whose only concern is the timing gate's gets the lyric reason put first. `replace_report` lists uploads.
4. **align** -- forced word alignment (`align.py`, 75 s pieces, flat ~4 GB) against the vocal stem (length from its WAV
   header, `_vocal_stem_seconds`); `combine.py` merges onto the lines (a last word past the stem end is tolerated;
   no lyrics -> clear RuntimeError). Whisper word times (`anchors.py`; words in silence dropped)
   checked against lrclib's line times (`combine_anchors`) bound each line to its own window
   (`align_words_anchored`, pad capped at half the neighbour gap so repeated back-to-back lines never overlap,
   9-29; whole-song CTC pass drifted 20-50 s; blend() weighs duration). `precision.py`/`sync.py` suggest;
   `timing_gate.py` alone decides (`Settings.timing_pass_percent`, default 90: % of lines within 0.5 s of Whisper's
   singing): best of whole-song/anchored/blended (`pick_by_sync`, ranked over judgeable lines; ties favor fewer
   silent lines), else the song is HELD before chords/images/render (Flagged: Render Anyway; Remove = hide only).
   Lists judge older songs READ-ONLY at the current bar (`timing_verdict`); only `python -m
   lyricvideo.timing_gate --hold` writes holds; `pipeline.review_concern()` gives the current reason. EASY variants are
   judged against their song's transcript (`models.original_song_dir`). Whisper mishearing/skipping a real,
   correctly-placed line also scores "out of sync" -- Whisper Text (Flagged) is editable per
   row; Save Corrections (`owner_whisper.py`, `whisper_owner.json`, keyed by row + line text) synthesizes heard
   words across its span, read by `corrected_heard_words()`/`_align_lyrics` (`add_corrections()`; a Redo
   recomputes it) -- never the placement itself. The anchor transcription is hinted (`lyric_hotwords()`,
   cache-keyed) with the accepted lyrics -- owner's text, or a fetched candidate once verified (the verifying
   check stays unhinted, 9-27). `revalidate_hotwords.py` checks for regression. `owner_verified.py` (Mark
   Verified, or an
   Upload Anyway the daily cap skips) overrides every check until the lyric words or their times change
   (`timing_fingerprint`: a Redo usually lapses it, a key fix keeps it). A cleared song's real % is in cleared_log's
   note. When new timing replaces a song that has a video, `<slug>.mp4` becomes `<slug>.previous.mp4` and
   `held_before_video.json` (`REDO_STOPPED_REASON`) holds it until a render: a Redo that dies mid-run shows in
   Flagged with Render Anyway, never in Pending/Upload. MMS_FA knows only a-z and `'`:
   `_normalize_word_for_alignment` spells numbers out ("31" -> "thirtyone", "10,000" -> "tenthousand"), folds
   accents, reads `&` as "and", else gives the `*` token. Display text never changes.
5. **detect_chords** (`detect_chords.py`, `chord_theory.py`) -- `crema` (CNN/CRNN, ISC) on `no_vocals.wav`, collapsed
   to 5 qualities by `_simplify_chord_label()` (every pumpp `3567s` quality mapped explicitly; `minmaj7` -> minor
   triad); returns sharp labels and key "". Needs old TF/Keras/sklearn -- **`.venv` is Python 3.11**; see
   `requirements.txt` pins. A resume past this stage whose song has no chords runs it (and the key settle) first;
   `gui._stage_to_resume`: no chords -> detect_chords, chords + video -> render, chords + key decision -> images.
   **Key check** (`key_decision.py`; owner: video and description MUST show the real key):
   `settle_song_key()` sets `chord_track.key` when the chord-based estimate (`key_estimate.py`) AGREES with a Claude
   second opinion among its candidates (`key_opinion.py`); on disagreement `key_research.py` (Haiku 4.5, web search <=2,
   ~4 cents) settles it only on high confidence + 2 cited sources (`source` "researched"; a Render Anyway resume of
   an unresearched hold searches too); else the song is HELD before images and Flagged with **Set Key** (`key_owner.json`, wins, survives Redo). A Set Key the saved chords already carry confirms at once
   (`confirm_owner_key`; the video needs no remaking); otherwise Render Anyway's resume runs `apply_saved_owner_key`
   (respells, records the decision regardless). During a job Set Key shows a message; a
   non-key says 'Not a key'. On an uploaded song (listed only while its EASY version waits on the key,
   `easy_version_waits_on_key`, reason 'key (EASY waits)') `_settle_key_of_uploaded_song` respells and confirms without
   re-rendering, then offers Rebuild EASY version. `key_decision.json` records it. `parse_key` accepts every real
   spelling (Cb/Fb/E#/B#, ♯/♭, sharp/flat, any case, m/min/minor/maj/major), refuses double accidentals and a
   capital 'M', never raises. `chord_theory.spell_in_key` spells by function: the key's own chords in its letters and
   flats/sharps, a borrowed chord by the step it alters (D major's bVI is Bb); Cb/E# etc. become B/F, so only
   `CHORD_SHAPES` names result (`respell_chord_track`, `transpose_chord_track`). `Settings.prefer_flats` ('Use flats
   in flat keys') off -> chords and the Key label use sharps ('A# major'); `key_decision.json`/`key_owner.json` always
   store the flat spelling; applied in settle_song_key, apply_saved_owner_key, settle_keys.py.
   `schedule_upload` refuses an unsettled key (`KeyNotConfirmed`); unsettled songs stay out of the pending lists
   (`key_needs_attention`). `scripts/settle_keys.py` (dry run;
   `--apply`) asks only songs with no decision or one in review (else 'already-settled', no Claude call); a corrected
   song's video becomes `*.previous.mp4`, `easychords/` moves to `easychords_prior_<time>/` unless on YouTube; `--apply`
   then remakes it from images (`--no-render` or a failed remake holds for Render Anyway).
   `scripts/add_key_note.py` adds `📌 Song key: X` notes to live descriptions.
6. **images** (`imagery.py`) -- one Claude gist call (`summarize_song_gist`), then a Replicate image per unique lyric
   line and per instrumental caption (`layout.instrumental_image_captions()`, the same walk `build_image_timeline()`
   renders; a sliver adopts its neighbour's caption; `[Instrumental]` only for a chordless song), cached by content
   hash. Instrumental pictures are filed under the chord NAME; `layout.enharmonic_instrumental_captions`/
   `fill_missing_instrumental_images` copy an existing one to a respelled or capo name (run_pipeline's
   `_carry_respelled_chord_pictures`, before images and before render: no spend). Pictures already in `images/` or
   `images_backup_*/` (decodable, not placeholders) are counted first; none missing -> no Claude client and no
   `REPLICATE_API_TOKEN` needed (else a missing token is a clear RuntimeError). A cached placeholder or undecodable
   file is a cache miss; cache writes are temp file + `os.replace`. Replicate polling rides out network errors,
   cancels on timeout, honours Retry-After; a retry reuses Claude's prompt unless the prediction failed.
   `get_or_generate_image` tries 3x, then reuses the song's last real image (`last_real_image`; only a first image
   falls back to plain colour); `substitute_fallback_images` swaps remaining placeholders (`is_fallback_image`: one
   solid colour) for the nearest real one; all placeholders -> RuntimeError, nothing renders.
   **Shared image library** (spec 9-25, decoupled HISTORY 9-30/10-1: saving unconditional; `use_image_library` controls
   only reuse; `image_library_min_score` provisional 0.34, see `scripts/preview_library_matches.py`'s contact
   sheets in `reports/`): before buying, look in
   `~/PlayAlongVideoProductionImages/` (`image_library.py`: SQLite + deduped PNGs; env `PLAYALONG_IMAGE_LIBRARY`)
   by local CLIP (`clip_embedder.py`; only `scripts/import_image_library.py` [`--dry-run`/`--limit N`] may
   download the ~605 MB weights). `LibrarySession` copies a hit into `images/`, files every purchase with its
   prompt regardless of the setting, disables itself on error. Reuse (`skip_lookup`) is off when the setting is
   off or Redo's "Generate new images"; a moved-aside picture is never offered again
   (`image_library_rejected.json`). `python -m lyricvideo.image_library stats`.
7. **render** (`assemble.py`/`layout.py`/`render.py`) -- lyrics (karaoke word sweep, Ken Burns), NOW/NEXT/timeline chord
   bar, Key/BPM badge, chord legend over the audio into `work_dir/<slug>.mp4`. Font: `--font`, else
   `Settings.font_path`, via `pipeline.resolve_font` (a missing file -> `default_font()` with a warning; candidates
   DejaVu/Liberation/Arial Bold/Segoe UI Bold). All text via `render.load_font()` (per-thread cache: never share
   FreeType faces across threads).
   - **Atomic output**: renders to `<name>.mp4.rendering` (temp audio `.rendering-audio.m4a` beside it, never the
     CWD), reads the picture length back (`_check_rendered_video`, ffmpeg stream copy), then `os.replace`s. A failed
     or killed render keeps the previous video and never leaves a cut-short mp4; `.rendering*` leftovers
     are never listed and are replaced next render. `rendered_stream_seconds(path)` -> (picture s, audio s), backed
     by `_frame_count_from_report()`: prefers ffmpeg's own `frame=` count, falling back to `time=` x the report's own
     `fps` when a build prints no `frame=` for a stream-copy-to-null pass -- else that
     build refuses every video, cut short or not (issue #8). libx265 gets `-pix_fmt yuv420p -tag:v
     hvc1`. `AudioFileClip` is closed in a `finally`.
   - Backgrounds: `_BackgroundCache` (LRU of 4 keyed by the real file, pre-scaled). A missing key shows the nearest
     existing picture in timeline order (previous first), else any of the song's; an undecodable one warns once and
     does the same, else the fallback colour. Swaps crossfade over `Settings.image_transition_seconds` (0.25 s, <=40%
     of either segment), the outgoing image frozen at its own Ken Burns progress.
   - Overlays (legend, CAPO badge, Key/BPM, support overlay, countdown) are lru_cached patches composited over their
     own box (`render.overlay_patch`/`composite_patch`), cleared when a render ends.
   - Legend: one diagram per unique chord (`pipeline.ordered_unique_chords()`), upper-left, current one highlighted.
     `chord_shapes.py` covers 12 roots x 5 qualities (sharp and flat names) from `tombatossals/chords-db` (MIT), except
     hand-picked Em7 022030. `chord_diagram._legend_layout()` sizes from the chord count: <=2 rows, <=0.35h including
     its top margin, never past `Settings.chord_legend_size`; panel opacity `chord_diagram_panel_alpha` (235); "Nfr"
     sits left of the low-E string. `Settings.show_chord_legend` toggles it.
   - Count-in: `Settings.countdown_beats` (4; 0 off) beats of `60/bpm` (120 if undetected), AT LEAST `countdown_seconds` (4 s:
     `countdown_beat_count`, 4-16 beats, 129 BPM -> 9) over the song's FIRST FRAME
     (scene at 0 on a real image, `_first_available_image_key()`; chord bar/chart at the first non-N chord) with
     `render.draw_countdown()`. `make_frame(T)` runs on the countdown-extended timeline (`song_t = T - countdown`).
   - Like/Subscribe (`render.draw_like_subscribe`, 10-4): Like pill, red Subscribe, bell; a cursor clicks it -> SUBSCRIBED,
     bell rings (`like_subscribe_state`, 4 s) + benefit line; in the count-in (donate
     spot) and the last `like_subscribe_lead_seconds` (10, under the donate label). Settings `show_like_subscribe`/etc.
   - Lyrics wrap at commas, else by word (`_split_line_into_rows`), never shrinking; spacing uses real block heights
     (`_rows_and_block_height`). `_in_a_line()` uses `_plausible_sung_intervals()`: past a line's plausible end "current" advances to the next line as an unsung preview (9-15),
     blank after the last; previews show only `Settings.lyric_preview_lead_seconds` (3.0) ahead. `scroll_progress`
     uses `_plausible_line_end()`; word highlight keys on each word's start.
   - `layout.build_image_timeline()`: one segment per sung line; in gaps one per instrumental chord, short ones merged
     forward to `Settings.image_min_hold_seconds` (2.0), boundaries always real chord onsets; a too-short gap holds
     the prior image; Ken Burns paces to the block's span.
   - Lane labels shrink to 18 pt (`_lane_label_font`) else are omitted (`_lane_label_visible`). No title/artist text
     in the frame (owner 9-09).
   - `render.draw_support_overlay()`: `Settings.support_overlay_text` (blank = off) in the last
     `support_overlay_lead_seconds` (20) only, upper-right, top at max(110, Key/BPM bottom + 14). The separate
     `support_description_text` is a description TEMPLATE (above / `{description}` / below; `assemble_description`)
     -- never clickable. Order: description, key note, tip/thank-you (9-29).
     `scripts/update_support_description.py` (`--dry-run`) re-renders into this order (bodies under `READY_CHARS`, 200).

## EASY CHORD versions

`build_capo_variant(work_dir, audio_path_override=None, settings=None, font_path=None)` renders `<slug>/easychords`
with the owner's Settings (`Settings.load()` if none). Every call remakes it from the song as it is now: lyrics and
timing; chords moved to capo shapes (`capo_and_shape_key()`), spelled in the SHAPE key (Dm shapes use Bb) and proved
by `capo_track_matches` (pitch classes); owner verification carried over (`carried_from`); a fresh mirror of
`images/` with each instrumental picture copied to its capo name (`fill_missing_instrumental_images` +
`instrumental_caption_sources`, `replace_from_sources=True`). Changed content -> old mp4 set aside (`.previous.mp4`),
folder held (`HELD_MARKER`) until the render finishes. `draw_capo_badge()` shows CAPO N under the legend, the Key badge
the original key (`key_label`); `easy_chord_capo.json` backs the title/description. `easy_chord_build_problem()` says
why nothing would build (key unsettled or already easy, no song_info title, audio gone). run_pipeline refuses a start
at detect_chords or earlier in an `easychords` folder, takes capo from the marker, and never applies an owner key or
hold, uses the library, or builds a variant of it. `is_easy_key`: open C/D/E/G/A/Am/Dm/Em (F/B excluded).
Staleness is judged in one place, `pipeline.easy_variant_problem` (read-only, cached): `easy_chord_upload_problem`'s
key/capo checks, chords shifted by the capo, identical lyric timing; no marker = stale. After every song render
`_update_easy_version_after_render` builds the variant when `Settings.generate_easy_chord_versions` (or Redo's Easy
Chords box) is on and the key is hard; otherwise a stale one's mp4 is set aside and held (Flagged: `EASY_STALE_PREFIX`
'EASY CHORD version out of date: ... Use Rebuild EASY version.'), or only set aside if the key is now easy. A stale
variant stays out of Pending even when verified, shows in Flagged while not uploaded, and `schedule_upload` refuses it
(`EasyChordVersionStale`) before the Claude call. "Generate EASY CHORD Versions" (`easy_chord_backfill_listing`/
`list_easy_chord_backfill_candidates`) lists passing songs the build WILL make with no current EASY video ('has no
video'/'out of date'), notes hard-key songs waiting for a key, renders with a Settings snapshot, reports a None build
as 'not built -- <reason>', and calls `release_memory()` per song. deep_review's EASY rebuild passes its settings.

## Redo an Existing Song

Re-runs a finished song through current code from `"fetch_lyrics"` (lyrics/chords fresh), reusing its stems and
`song_info.json` (`list_redoable_songs()`/`load_redo_inputs()`, preferring the local audio copy). `load_song()`
tolerates pre-merge files (reads only current `Word` fields; no `"chord_track"` -> empty). "Generate new images"
(default off) runs `prepare_images_for_fresh_regeneration()`: `images/` moves to `images_prior_<time>/`, deliberately
NOT `images_backup_*` (auto-reused). `backup_song_outputs()` first copies the video + `lyrics_timed.json` into
`redo_backup_<time>/`; `redo_log.py` records the redo (redone songs on YouTube still need replacing).

## GUI lists, panels and Settings

- **Song lists** (Redo / Upload / Pending / EASY CHORD / Flagged): each is ONE `ttk.Treeview` (`gui._SongListView`,
  dark via `_song_list_font`; Windows uses 'clam') in a section starting CLOSED
  (`_make_collapsible_section`, `SONG_LIST_HEIGHT`; never nest a `CTkScrollableFrame`). One Watch / ✕ Remove pair per
  list acts on the highlighted row (double-click Watches); Remove hides it (`_drop_list_row`, no rescan), files
  untouched. Filled on first expand by one non-daemon scanner thread (`_refresh_song_list` -> `_start_list_load`;
  `_poll_list_results` applies on Tk; a generation counter drops stale results); `invalidate()` on an open section
  rescans in the background; `_refresh_retry_upload_options` invalidates all six lists;
  Batch's `"batch_item_done"` refreshes them live. Pending and EASY CHORD are checklists (☑ column: click or Space;
  all start ticked; ticks survive refreshes; `_TickFlag`s in `_pending_upload_vars`/`_easy_chord_backfill_vars`).
- **Scans** (`pipeline.list_*`) parse each `lyrics_timed.json` once per file version (keyed by folder + (inode,
  mtime_ns, size) of it and `owner_verified.json`); the sync verdict is cached in `timing_gate.check_saved_song`
  (bar applied after). Lock-guarded, safe off the Tk thread, never write. An unreadable song is skipped with one
  warning and counts as held (fail closed).
- **Flagged for Lyrics Review**: a list with a short reason column plus ONE detail pane (`_show_flagged_details`;
  reason = `pipeline.review_concern`, else `held_before_video.json`'s own) whose buttons enable per song: Watch (else
  Play MP3), Whisper Text, Edit Lyrics (`lyrics_owner.txt`, reused by Redo), Redo, Render Anyway, Mark Verified, Upload
  Anyway, Remove, Set Key (plain text, no emoji). A `<song>/easychords` row gets only Watch/Remove and Rebuild EASY
  version (`_on_rebuild_easy_flagged`); the other handlers refuse it (`_refuse_easy_variant`).
- **YouTube Comments** / **Pending Engagement Comments**: collapsible and lazy, each one list + one editor
  (`_DraftQueueView`); each reply names its video too (10-3, `PendingReply.video_title`); edits are kept per draft
  id across refreshes, Approve posts the edited text, the header shows "N waiting".
- **Settings popup** (`_open_settings_window`): built once, then hidden/shown (`_show_settings_dialog`/
  `_hide_settings_dialog`; `_grab_settings_dialog` retries grab_set every 50 ms up to 20x -- X11 refuses a grab until
  the window is mapped); transient/grab_set/lift/focus_force/brief-topmost like the Update dialog; its confirms are
  parented to it (`_ask_over`, `SettingsPanel._confirm`). Closing with unsaved changes asks like Discard and reverts
  `self.settings` to the on-disk baseline. Its preview (`settings_preview.py`: synthetic frame via `render_kwargs()`
  and `resolve_font`) is debounced 150 ms. A moved timing pass mark refreshes the lists once, on close
  (`_apply_pass_mark_change`). Building a `SettingsPanel` is wrapped in `_suppress_settings_save` (`load_from()` fires
  `on_change` early; 9-11).
- **SettingsPanel** (`settings_panel.py`; `~/.playalongvideoproduction/settings.json`): writes disk ONLY via Save
  Settings. A field differing from `self._baseline` gets a ● and a bold+orange label
  (`_refresh_dirty_indicators`/`_dirty_fields`; only flipped labels are redrawn); Save shows an itemized `old → new`
  confirm, and a failed write (OSError) shows an error and stays unsaved; Discard reloads the baseline. gui's
  `self.settings` updates live, so this session's runs use the latest values. Each field shows its default
  (`_default_text`); each slider has a box (`_parse_clamped_float`: "%"/"s" suffix, ".5", "0,5"; unedited text is
  ignored; opacity boxes are percent) driven off a trace on its Tk variable, not `CTkSlider`'s `command`;
  `on_value_change` fires only on a real change. Labels must stay short (one long label broke the panel; `_add()`).
  Font row: file name + 'Auto' (clears it). The Category dropdown shows labels ("Howto & Style"/"Education"/"Music");
  `youtube_category_id` stores the id. "Reset to Defaults" repopulates in one on_change. `Settings.save()` is atomic
  and raises OSError; `load()`/`from_dict()` default a non-object file and any wrong-typed value (with a warning).

## Notable pinned dependency

`requirements.txt` pins `moviepy>=1.0.3,<2.0` and `decorator<5.0,>=4.0.2` (moviepy 1.0.3's decorators silently
break under decorator 5: fps resolves to `None`) -- never bump either without re-verifying rendered output.
`.venv`'s Python 3.11 must be a real system install (`python3.11-tk`, deadsnakes), never `uv`'s standalone build:
its Tk lacks Xft and breaks the GUI font.

## Update Available Feature

Spec `docs/superpowers/specs/2026-09-08-update-available-design.md`. `VERSION` is the last release applied to this
checkout (never hand-edited or bumped per commit). `lyricvideo/update/`: `version.py`, `release_client.py` (httpx)
against the public, unlisted `flyguy91355/LyricVideoGen-releases` (pre-rename name, deliberately kept), and `apply.py`
-- allows `lyricvideo/`, `deep_review/`, `scripts/`, `tests/`, `docs/`, `requirements.txt`, `CLAUDE.md`, a bare
top-level `*.py`/`*.sh`/`*.bat`; denies (always wins) `.env`, `songs/`, `work/`, `.venv/`. `cut_release.sh` ships that
same set (`tests/test_update_manifest.py` keeps them in step) plus `RELEASE_MANIFEST`; `copy_updatable_files` then
removes allow-listed regular files the previous applied release shipped and this one does not (install-root
`.release_manifest`, gitignored). `gui.py` checks once on launch (background thread); a banner opens a modal dialog
(centered, `transient`+`grab_set`+`lift`+`focus_force`+brief `-topmost`) with the notes, Apply Update (its confirm is
parented to `self._update_dialog_window`) then Relaunch Now. Apply Update is refused while a video is made or an
upload runs, and runs one at a time (`_update_apply_in_progress`); Close/X only HIDE the dialog during an apply (the
result or the banner brings it back); `_poll_update_queue` handles each message alone and always reschedules.
`self.top_frame` (the banner's `before=` anchor) must be `.pack()`-managed. Cut a release with
`scripts/cut_release.sh <version-tag> <notes-file>`: it exports committed `HEAD` (`git show`, never the working tree),
re-applies `chmod +x` to `100755` paths, and with `apply.py` guards against a stale release reverting newer commits
(HISTORY 9-13). The owner runs this git checkout directly, so commits reach them at once; releases keep the banner and
changelog meaningful.

## YouTube upload + channel management

Design `docs/superpowers/specs/2026-09-10-youtube-upload-design.md` (why `publishAt`, not a local queue).
`youtube_state.py`: each song's `youtube_state.json` (`video_id`, `uploaded_at`, `title`, `engagement_comment_posted`)
beside its `lyrics_timed.json`; `uploaded_song_dirs(work_root)` / `mark_engagement_comment_posted(work_root, id)`
include nested `<song>/easychords`. One that exists but cannot be read counts as uploaded everywhere (fail closed).
`youtube.py` wraps the Data API v3 (`upload_video`, `list_new_comments`, `post_reply`, playlists,
`post_top_level_comment` = `commentThreads().insert`), each taking an injected client (faked in tests; no test touches
the network); `Comment.author_channel_id`. `upload_video` retries a dropped connection or 5xx up to 8x on the SAME
resumable request (`bytes */N` recovers what YouTube stored); 4xx (quota, uploadLimitExceeded, invalidTitle) raise at
once; `is_quota_exceeded_error` also knows HTTP 403 `quotaExceeded`.
`youtube_metadata.py`: every Claude call has thinking off and is retried; `generate_video_metadata` (description/tags)
and `draft_comment_reply`/`draft_engagement_comment` RAISE rather than return blank (never a bare video, 9-26);
`classify_genre` returns '' rather than a fragment; replies use labeled lines robust to reordering. The TITLE is never
Claude's: `build_play_along_title()` `"{title} - {artist} - (Play Along Lyrics & Chords)"` (artist omitted when
unknown) / `build_easy_chord_title`, fitted to 100 chars with no '<'/'>' (the suffix stays; artist dropped first, then
an ellipsis).
`youtube_schedule.schedule_upload()` is the single upload path (auto and manual). It refuses a missing, unreadable,
audio-less or cut-short video with `IncompleteVideo` (`.cut_short`, `.video_path`; cut short = picture >1 s shorter
than audio per `assemble.rendered_stream_seconds`) -- like the key, stale-EASY and bad-title checks, before the Claude
call and any YouTube call; the GUI logs it as 'the video must be made again' and never renames the file. It uploads one
at a time (`_UPLOAD_LOCK`, `upload_in_progress()`); `only_if_not_uploaded=True` raises `AlreadyUploaded` when a
`youtube_state.json` EXISTS (even empty/cut off); a state write failing after the upload raises naming the video_id.
Public target -> uploaded Private with a future `publishAt`: `compute_next_publish_slot()` gap-fills the channel's live
schedule (`reserved_publish_datetimes()`, one claim per video), localizing each slot for its own date (DST-safe;
`tz=` for tests), at `Settings.youtube_upload_times`, capped by `youtube_max_uploads_per_day`; quota/
`uploadLimitExceeded` cools down `youtube_quota_retry_hours`. Unlisted/Private upload at once. Category default "27".
Thumbnails (`thumbnail*.py`, 10-4): `Settings.generate_thumbnails` -> `thumbnail.jpg` after render (Sonnet
prompt of the song's central image + 3 flux pictures, best kept as `thumbnail_bg.png`, full title (<=100 px; one word up to 150) left of the chord diagrams; EASY reuses its song's
picture + green "EASY CHORDS · CAPO n" badge), set after upload (`set_thumbnail`, 50 units, soft-fail); `scripts/backfill_thumbnails.py`.
`scripts/find_truncated_videos.py` (dry run; `--only <song>` or `<song>/easychords`, backslash ok; `--jobs`) lists cut-short mp4s in every song and easychords folder,
marking ones on YouTube (delete/replace there); `--set-aside` renames them `*.truncated.mp4` (never deletes; Redo, with
Easy Chords ticked for an EASY one, remakes them); key_rollout and `_set_aside_videos` skip that name.
`youtube_auth.py`: `connect()` (browser consent with a `client_secret_*.json`) saves `youtube_token.json` (0600 in a
0700 folder, refresh serialized); `load_credentials()` is `None` for not connected/expired-without-refresh (never
raises). Status label + Connect refresh on a
background thread (never call YouTube from the Tk thread; also every 20-minute tick). Worker threads format an error's
text BEFORE a deferred `root.after` lambda (`except ... as e` unbinds `e`; 9-14).
**GUI uploads**: "Upload to YouTube" = a `list_rendered_songs()` list + Upload (the only path that re-sends a song,
after "create a duplicate?"), and the "Pending YouTube Uploads" checklist (`list_pending_uploads()`: never-uploaded,
cleared) + Upload Selected; both ignore `youtube_auto_upload`. One upload run at a time (`_UPLOAD_RUN_LOCK`): Upload /
Upload Selected / Upload Anyway during a run or the tick's auto-retry say 'Upload already running' and are disabled
(a Batch/tick finish never re-enables them mid-run); the tick just skips. Every pending or auto upload goes
through `_upload_within_todays_cap` (`_UPLOAD_SLOT_LOCK`: re-check state, cap,
`schedule_upload(only_if_not_uploaded=True)`, `record_upload`); `AlreadyUploaded` is a skip (in the results).
`_maybe_upload_to_youtube(work_dir, settings)` gates auto-upload (enabled, connected, never uploaded -- verified
live by `video_exists()`, failing CLOSED, so a deleted video re-uploads; flagged songs skipped) from `_run_worker`
and each Batch item; failures only warn.
A failed auto-retry waits 6 h (per session). Ticks never overlap (`_TICK_LOCK`).
**Comments** (`youtube_comment_state.py`, flat JSON; locked, atomic, idempotent adds): `_scan_youtube_comments` (Check
Now or the 20-minute tick, never overlapping: `_COMMENT_CHECK_LOCK`) walks `uploaded_song_dirs` (EASY included); one
`videos.list(part=status,statistics)` per 50 videos; a public video's comments are read only when its commentCount
changed since its last complete check (`youtube_comment_counts.json`) plus a full read every 24 h; 0 or comments off
skipped; the channel's own comments skipped; quota break and cooldown kept. Each comment is marked seen
(`mark_comment_seen`) right after its draft is queued; a failed draft retries next check (`record_draft_failure`),
then after `MAX_DRAFT_FAILURES` (3) is queued blank. Nothing posts without Approve; a draft being or already posted is
claimed (`_claim_posting`; its buttons off via `_DraftQueueView.set_busy`), released on failure; not connected says so
and refreshes the label; Approve checks `is_video_public()`. An approved engagement comment's thread is marked seen;
Dismissing one marks `engagement_comment_posted`.
**Channel organization** (spec 9-17): after every `schedule_upload()` gui calls `youtube_playlists.organize_video()`
(fails soft; idempotent): All, EASY/3/4 and artist playlists first, then genre (`classify_genre`). EASY/3/4 membership
is reconciled both ways from the real key (owner key > confirmed decision > saved key; an easychords version uses its
shape key). Artists: song_info `artists` (`_artists_for`) else `_split_artists` ('A, B & C', comma-named acts kept
whole). A cache miss re-finds the channel's playlist by exact title before creating one; `add_video_to_playlist_with_
retry()` rides a fresh 404. State files (comment, playlist, youtube_state, token) use locked read-modify-write and
atomic writes (`youtube_state.atomic_write_text`); an unreadable one is moved to `*.corrupt-<time>`. Key notes: only
paragraphs starting '📌 Song key:'/'📌 Correction' count; `apply_key_note` keeps exactly one, first.
`scripts/backfill_channel_organization.py` (loads `.env`; stops on quota; `--dry-run`, `--only`, `--key-notes`
[default `scripts/key_notes_2026-09-26.json`]/`--no-key-notes`, `--fix-playlist-descriptions` backing up to
`reports/`) applies this to older uploads.

## deep_review

`python -m deep_review` retries top-level songs flagged for lyrics or timing (`runner.list_songs_to_review`), cost-
tracked (3 searches/attempt, 5-cent/song cap, skips 85%+ untouched; $0.01 per web search counted; 9-22). It never
redoes an EASY variant (a fixed original re-renders its existing one), skips key-only songs (`waiting_for_key`, never a
pass) and songs on verify_lyrics' unchecked hold (UNKNOWN). A lyric-text concern is never cleared by a timing pass
(`diagnosis.is_lyric_text_concern`); all checks use `Settings.timing_pass_percent`. `--dry-run` writes nothing. A
failed review restores `lyrics_owner.txt` byte for byte (also copied into the first `redo_backup_*`). Needs-human
markers are flat files (`<song>__easychords.txt`).

## Persistence invariants

`models.save_song`/`atomic_write_text` (unique temp, fsync, `os.replace`); cleared_log and redo_log serialize their
read-modify-write under a lock, and a corrupt `cleared_songs.json`/`redone_songs.json` is renamed `.corrupt-<time>`,
never silently replaced.

## Tests

```bash
cd <repo root> && .venv/bin/python -m pytest tests/ -v      # Windows: .venv/Scripts/python.exe
```

`tests/conftest.py` points HOME/USERPROFILE at a throwaway home before any import and gives each test its own
(per-user path constants redirected; XDG_CACHE_HOME and GIT_CONFIG_GLOBAL stay real): the suite
never touches the real `~/.playalongvideoproduction/`. Its font fixture lists Windows fonts too. Real-window tests
create their root with `_new_ctk_root(ctk)`.
Tests write only to pytest's `tmp_path`, never a fixed path (a leftover file caused a stale-cache bug).
