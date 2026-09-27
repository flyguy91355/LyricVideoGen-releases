from lyricvideo.youtube_comment_state import (
    PendingComment,
    PendingReply,
    add_pending_comment,
    add_pending_reply,
    load_pending_comments,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_comment,
    remove_pending_reply,
    save_pending_comments,
    save_pending_replies,
)


def test_load_seen_comment_ids_empty_when_no_file(tmp_path):
    assert load_seen_comment_ids(tmp_path / "no_such_file.json") == set()


def test_mark_comments_seen_then_load_round_trips(tmp_path):
    path = tmp_path / "seen.json"

    mark_comments_seen(["c1", "c2"], path)
    mark_comments_seen(["c2", "c3"], path)

    assert load_seen_comment_ids(path) == {"c1", "c2", "c3"}


def _make_reply(comment_id="c1") -> PendingReply:
    return PendingReply(
        comment_id=comment_id, video_id="v1", author="Alice", comment_text="hi",
        draft_reply="thanks!", is_error_report=False,
    )


def test_load_pending_replies_empty_when_no_file(tmp_path):
    assert load_pending_replies(tmp_path / "no_such_file.json") == []


def test_add_then_load_pending_replies_round_trips(tmp_path):
    path = tmp_path / "pending.json"

    add_pending_reply(_make_reply("c1"), path)
    add_pending_reply(_make_reply("c2"), path)

    replies = load_pending_replies(path)
    assert [r.comment_id for r in replies] == ["c1", "c2"]


def test_remove_pending_reply_removes_only_the_matching_one(tmp_path):
    path = tmp_path / "pending.json"
    save_pending_replies([_make_reply("c1"), _make_reply("c2")], path)

    remove_pending_reply("c1", path)

    assert [r.comment_id for r in load_pending_replies(path)] == ["c2"]


def _make_pending_comment(video_id="v1") -> PendingComment:
    return PendingComment(video_id=video_id, song_title="My Song", draft_text="Which instrument are you playing?")


def test_load_pending_comments_empty_when_no_file(tmp_path):
    assert load_pending_comments(tmp_path / "no_such_file.json") == []


def test_add_then_load_pending_comments_round_trips(tmp_path):
    path = tmp_path / "pending_comments.json"

    add_pending_comment(_make_pending_comment("v1"), path)
    add_pending_comment(_make_pending_comment("v2"), path)

    comments = load_pending_comments(path)
    assert [c.video_id for c in comments] == ["v1", "v2"]


def test_remove_pending_comment_removes_only_the_matching_one(tmp_path):
    path = tmp_path / "pending_comments.json"
    save_pending_comments([_make_pending_comment("v1"), _make_pending_comment("v2")], path)

    remove_pending_comment("v1", path)

    assert [c.video_id for c in load_pending_comments(path)] == ["v2"]


# --- a re-scan never queues a second copy, and concurrent changes are never lost (issue #7 review, F083/F143) -----------

def test_add_pending_reply_twice_for_one_comment_keeps_one(tmp_path):
    path = tmp_path / "pending.json"

    assert add_pending_reply(_make_reply("c1"), path) is True
    assert add_pending_reply(_make_reply("c1"), path) is False

    assert [r.comment_id for r in load_pending_replies(path)] == ["c1"]


def test_add_pending_comment_twice_for_one_video_keeps_one(tmp_path):
    path = tmp_path / "pending_comments.json"

    add_pending_comment(_make_pending_comment("v1"), path)
    add_pending_comment(_make_pending_comment("v1"), path)

    assert [c.video_id for c in load_pending_comments(path)] == ["v1"]


def test_mark_comment_seen_one_at_a_time(tmp_path):
    from lyricvideo.youtube_comment_state import mark_comment_seen

    path = tmp_path / "seen.json"
    mark_comment_seen("c1", path)
    mark_comment_seen("c2", path)

    assert load_seen_comment_ids(path) == {"c1", "c2"}


def test_concurrent_adds_and_removes_never_lose_an_added_reply(tmp_path):
    """The review's race: an Approve (remove) overlapping the comment scan (add) wrote back a list without the new
    reply, and that viewer's comment -- already marked seen -- never came back."""
    import threading

    path = tmp_path / "pending.json"
    save_pending_replies([_make_reply(f"old{i}") for i in range(30)], path)

    def adder(start):
        for i in range(start, start + 30):
            add_pending_reply(_make_reply(f"new{i}"), path)

    def remover():
        for i in range(30):
            remove_pending_reply(f"old{i}", path)

    threads = [threading.Thread(target=adder, args=(0,)), threading.Thread(target=adder, args=(30,)),
               threading.Thread(target=remover)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r.comment_id for r in load_pending_replies(path)) == sorted(f"new{i}" for i in range(60))


def test_a_corrupt_seen_file_is_moved_aside_not_overwritten(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text('["c1", "c2"', encoding="utf-8")

    assert load_seen_comment_ids(path) == set()
    mark_comments_seen(["c3"], path)

    assert load_seen_comment_ids(path) == {"c3"}
    backups = list(tmp_path.glob("seen.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == '["c1", "c2"'


def test_draft_failures_are_counted_per_comment_and_can_be_cleared(tmp_path):
    from lyricvideo.youtube_comment_state import MAX_DRAFT_FAILURES, clear_draft_failures, record_draft_failure

    path = tmp_path / "failures.json"

    assert [record_draft_failure("c1", path) for _ in range(MAX_DRAFT_FAILURES)] == list(range(1, MAX_DRAFT_FAILURES + 1))
    assert record_draft_failure("c2", path) == 1
    clear_draft_failures("c1", path)
    assert record_draft_failure("c1", path) == 1


def test_one_malformed_pending_entry_does_not_cost_the_rest(tmp_path):
    import json

    path = tmp_path / "pending.json"
    good = {"comment_id": "c1", "video_id": "v1", "author": "A", "comment_text": "hi", "draft_reply": "thanks",
            "is_error_report": False}
    path.write_text(json.dumps([good, {"unexpected": True}]), encoding="utf-8")

    assert [r.comment_id for r in load_pending_replies(path)] == ["c1"]
