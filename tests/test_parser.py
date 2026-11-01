import json
from pathlib import Path

import pytest

from threads2video.fetcher.parser import ParseError, loads_payload, parse_payloads

FIXTURE = Path(__file__).parent / "fixtures" / "thread_sample"


def load_fixture() -> tuple[list, str]:
    meta = json.loads((FIXTURE / "meta.json").read_text())
    payloads = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(FIXTURE.glob("0*.json"))]
    return payloads, meta["url"]


def raw_post(pk, username, text, *, code=None, likes=0, user_pk=None, **info):
    return {
        "pk": str(pk),
        "code": code or f"C{pk}",
        "user": {"pk": str(user_pk or f"u_{username}"), "username": username, "full_name": username.title()},
        "caption": {"text": text},
        "like_count": likes,
        "taken_at": 1_790_000_000,
        "text_post_app_info": {"direct_reply_count": 0, **info},
    }


def chain(*posts):
    return {"node": {"posts": {"edges": [{"node": p} for p in posts]}}}


# --- real (anonymised) payloads -------------------------------------------------


def test_fixture_main_post():
    payloads, url = load_fixture()
    t = parse_payloads(payloads, code="SAMPLE001", source_url=url)
    assert t.post.author.username == "user_1"
    assert t.post.is_op and t.post.depth == 0
    assert t.post.text
    assert t.post.like_count > 0 and t.post.reply_count > 0


def test_fixture_replies_and_self_thread():
    payloads, url = load_fixture()
    t = parse_payloads(payloads, code="SAMPLE001", source_url=url)
    assert len(t.replies) >= 10
    assert all(r.author.username for r in t.replies)
    assert {r.depth for r in t.replies} >= {1, 2}
    nested = [r for r in t.replies if r.depth == 2]
    ids = {r.id for r in t.replies}
    assert all(r.parent_id in ids for r in nested)
    assert all(r.parent_id == t.post.id for r in t.replies if r.depth == 1)
    assert len(t.self_thread) >= 1
    assert all(p.author.id == t.post.author.id for p in t.self_thread)
    # The page only showed some of the replies.
    assert t.has_more_replies
    # Related posts / quoted posts elsewhere on the page must not leak in.
    assert t.post.id not in ids and not ids & {p.id for p in t.self_thread}


def test_fixture_media_on_reply():
    payloads, url = load_fixture()
    t = parse_payloads(payloads, code="SAMPLE001", source_url=url)
    assert any(r.media and r.media[0].kind == "image" for r in t.replies)


# --- synthetic edge cases ---------------------------------------------------------


def test_op_reply_chain_and_foreign_subtrees():
    main = raw_post(1, "op", "Main post", code="MAIN", user_pk="OP", likes=50)
    main["text_post_app_info"]["direct_reply_count"] = 2
    main["text_post_app_info"]["direct_replies"] = {
        "edges": [
            chain(raw_post(2, "alice", "First!", likes=5), raw_post(3, "op", "Thanks Alice", user_pk="OP")),
            chain(raw_post(4, "bob", "Nice", likes=9)),
        ],
        "page_info": {"has_next_page": False},
    }
    main["text_post_app_info"]["share_info"] = {"quoted_post": raw_post(99, "eve", "quoted")}
    page = {"data": {"media": main, "relatedPosts": {"threads": [{"thread_items": [{"post": raw_post(98, "x", "y")}]}]}}}

    t = parse_payloads([page], code="MAIN", source_url="u")
    assert [r.id for r in t.replies] == ["2", "3", "4"]
    by_id = {r.id: r for r in t.replies}
    assert by_id["3"].is_op and by_id["3"].depth == 2 and by_id["3"].parent_id == "2"
    assert not by_id["2"].is_op
    assert [r.id for r in t.op_replies] == ["3"]
    assert not t.has_more_replies


def test_refetch_payload_merges_by_media_id():
    main = raw_post(1, "op", "Main", code="MAIN", user_pk="OP")
    page = {"data": {"media": main}}
    refetch = {"data": {"media": {"id": "1_OP", "text_post_app_info": {
        "direct_replies": {"edges": [chain(raw_post(5, "carol", "later reply"))], "page_info": {"has_next_page": True}}}}}}
    other = {"data": {"media": {"id": "777_X", "text_post_app_info": {
        "direct_replies": {"edges": [chain(raw_post(6, "dave", "not ours"))]}}}}}
    t = parse_payloads([page, refetch, other], code="MAIN", source_url="u")
    assert [r.id for r in t.replies] == ["5"]
    assert t.has_more_replies


def test_text_falls_back_to_fragments():
    main = raw_post(1, "op", "", code="MAIN")
    main["caption"] = None
    main["text_post_app_info"]["text_fragments"] = {"fragments": [{"plaintext": "Hello "}, {"plaintext": "@bob"}]}
    t = parse_payloads([main], code="MAIN", source_url="u")
    assert t.post.text == "Hello @bob"


def test_missing_post_raises():
    with pytest.raises(ParseError, match="not found"):
        parse_payloads([{"data": {}}], code="NOPE", source_url="u")


def test_loads_payload_guard_and_ndjson():
    assert loads_payload('for (;;);{"a": 1}') == [{"a": 1}]
    assert loads_payload('{"a": 1}\n{"b": 2}\n') == [{"a": 1}, {"b": 2}]
