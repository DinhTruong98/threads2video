from datetime import datetime, timezone

import pytest

from threads2video.config import SelectionConfig
from threads2video.models import Author, Post, ThreadData
from threads2video.selection import rejection_reason, select_segments

OP = Author(id="op", username="op")


def post(pid, text, likes=0, *, user="u", op=False, parent="main", depth=1):
    author = OP if op else Author(id=f"id_{user}", username=user)
    return Post(id=pid, author=author, text=text, like_count=likes, is_op=op, parent_id=parent, depth=depth)


def thread(replies, self_thread=()):
    main = Post(id="main", author=OP, text="Main post", is_op=True, depth=0)
    return ThreadData(source_url="u", fetched_at=datetime.now(timezone.utc), post=main,
                      replies=list(replies), self_thread=list(self_thread))


def ids(segs):
    return [s.id for s in segs]


@pytest.mark.parametrize(
    "text, reason",
    [
        ("Bài viết hay quá", None),
        ("   ", "empty"),
        ("xem tại https://example.com/x", "contains a link"),
        ("ghé shop.vn nhé", "contains a link"),
        ("😂😂😂", "emoji/symbols only"),
        ("!!! ❤️", "emoji/symbols only"),
        ("ok", "too short"),
        ("a" * 401, "too long"),
        ("真的吗", None),
    ],
)
def test_rejection_reason(text, reason):
    assert rejection_reason(text, SelectionConfig()) == reason


def test_filters_can_be_disabled():
    cfg = SelectionConfig(skip_links=False, skip_emoji_only=False, min_chars=1)
    assert rejection_reason("https://x.com", cfg) is None
    assert rejection_reason("😂", cfg) is None


def test_top_n_by_likes_with_stable_ties():
    t = thread([post("a", "comment a", 5), post("b", "comment b", 50), post("c", "comment c", 5), post("d", "comment d", 1)])
    segs = select_segments(t, SelectionConfig(top_comments=3, op_replies=0))
    assert ids(segs) == ["main", "b", "a", "c"]
    assert segs[0].kind == "post" and segs[0].is_op
    assert all(s.kind == "reply" for s in segs[1:])


def test_filtered_and_duplicate_replies_skipped():
    t = thread([post("a", "😂😂", 100), post("b", "Same text", 9), post("c", "same  TEXT", 8), post("d", "link www.x.com", 50)])
    assert ids(select_segments(t, SelectionConfig(top_comments=5))) == ["main", "b"]


def test_op_reply_follows_its_parent():
    t = thread([
        post("a", "comment a", 10),
        post("b", "comment b", 20),
        post("opb", "thanks b", 0, op=True, parent="b", depth=2),
    ])
    segs = select_segments(t, SelectionConfig(top_comments=2, op_replies=1))
    assert ids(segs) == ["main", "b", "opb", "a"]
    assert segs[2].kind == "op_reply"


def test_op_reply_pulls_in_parent_for_context():
    t = thread([
        post("a", "comment a", 10),
        post("low", "low comment", 0),
        post("op1", "answer to low", 3, op=True, parent="low", depth=2),
    ])
    with_parent = select_segments(t, SelectionConfig(top_comments=1, op_replies=1))
    assert ids(with_parent) == ["main", "a", "low", "op1"]
    alone = select_segments(t, SelectionConfig(top_comments=1, op_replies=1, op_reply_with_parent=False))
    assert ids(alone) == ["main", "a", "op1"]


def test_op_replies_limited_and_ranked():
    t = thread([
        post("a", "comment a", 10),
        post("o1", "op one", 1, op=True, parent="a", depth=2),
        post("o2", "op two", 7, op=True, parent="a", depth=2),
        post("o3", "op three", 3, op=True, parent="a", depth=2),
    ])
    assert ids(select_segments(t, SelectionConfig(top_comments=1, op_replies=2))) == ["main", "a", "o2", "o3"]
    assert ids(select_segments(t, SelectionConfig(top_comments=1, op_replies=0))) == ["main", "a"]


def test_op_reply_to_op_reply_not_duplicated():
    t = thread([
        post("a", "comment a", 0),
        post("o1", "op first", 5, op=True, parent="x_missing", depth=2),
        post("o2", "op second", 4, op=True, parent="o1", depth=3),
    ])
    segs = select_segments(t, SelectionConfig(top_comments=0, op_replies=2))
    assert ids(segs) == ["main", "o1", "o2"]


def test_self_thread_optional():
    st = [Post(id="s1", author=OP, text="Part 2", is_op=True)]
    t = thread([post("a", "comment a", 1)], self_thread=st)
    assert ids(select_segments(t, SelectionConfig())) == ["main", "a"]
    segs = select_segments(t, SelectionConfig(include_self_thread=True))
    assert ids(segs) == ["main", "s1", "a"] and segs[1].kind == "self_thread"


def test_segment_keeps_original_text():
    t = thread([post("a", "comment a", 1)])
    s = select_segments(t, SelectionConfig())[1]
    assert s.text == s.text_original == "comment a"
