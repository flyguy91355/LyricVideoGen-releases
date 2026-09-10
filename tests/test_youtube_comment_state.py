from lyricvideo.youtube_comment_state import (
    PendingReply,
    add_pending_reply,
    load_pending_replies,
    load_seen_comment_ids,
    mark_comments_seen,
    remove_pending_reply,
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
