"""Turn raw Threads JSON payloads into a ThreadData.

Pure and offline: the browser layer only collects payloads (embedded
`<script type="application/json">` blobs and /api/graphql responses); all the
schema knowledge lives here so it can be fixed and tested against the cache.

Posts are recognised by shape (a dict with `pk`, `user` and
`text_post_app_info`), not by GraphQL query name or doc_id, which change often.
Replies are read from `direct_replies` of the fetched post: each edge is a
reply chain (reply, then reply-to-that-reply, ...). Subtrees that hold posts
which are not part of this conversation are skipped.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator

from ..models import Author, MediaItem, Post, ThreadData

# Subtrees whose posts are not replies to the fetched post.
_FOREIGN_KEYS = {
    "relatedPosts",
    "quoted_post",
    "reposted_post",
    "quoted_attachment_post",
    "containing_thread",
}


class ParseError(RuntimeError):
    pass


def loads_payload(text: str) -> list[Any]:
    """Decode a response body; handles the `for (;;);` guard and streamed (NDJSON) bodies."""
    text = text.strip().removeprefix("for (;;);")
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


def _is_post(o: Any) -> bool:
    return isinstance(o, dict) and "pk" in o and "text_post_app_info" in o and isinstance(o.get("user"), dict)


def _walk(o: Any, skip_foreign: bool = True) -> Iterator[dict]:
    """Yield every dict in the tree in document order, not descending into foreign subtrees."""
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            children = [
                v for k, v in cur.items()
                if isinstance(v, (dict, list)) and not (skip_foreign and k in _FOREIGN_KEYS)
            ]
            stack.extend(reversed(children))
        elif isinstance(cur, list):
            stack.extend(x for x in reversed(cur) if isinstance(x, (dict, list)))


def _edges(conn: Any) -> list[dict]:
    if not isinstance(conn, dict):
        return []
    return [e.get("node") for e in conn.get("edges") or [] if isinstance(e, dict) and isinstance(e.get("node"), dict)]


def _media_pk(media: dict) -> str | None:
    if media.get("pk"):
        return str(media["pk"])
    mid = media.get("id")
    if isinstance(mid, str) and "_" in mid:
        return mid.split("_", 1)[0]
    return None


def _text(raw: dict) -> str:
    caption = raw.get("caption")
    if isinstance(caption, dict) and caption.get("text"):
        return caption["text"]
    frags = ((raw.get("text_post_app_info") or {}).get("text_fragments") or {}).get("fragments") or []
    return "".join(f.get("plaintext") or "" for f in frags if isinstance(f, dict))


def _best(candidates: Any) -> dict | None:
    cands = [c for c in candidates or [] if isinstance(c, dict) and c.get("url")]
    return max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0), default=None)


def _media(raw: dict) -> list[MediaItem]:
    items = raw.get("carousel_media") or [raw]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if vid := _best(it.get("video_versions")):
            out.append(MediaItem(kind="video", url=vid["url"], width=vid.get("width"), height=vid.get("height")))
        elif img := _best((it.get("image_versions2") or {}).get("candidates")):
            out.append(MediaItem(kind="image", url=img["url"], width=img.get("width"), height=img.get("height")))
    return out


def _author(user: dict) -> Author:
    return Author(
        id=str(user.get("pk") or user.get("id") or ""),
        username=user.get("username") or "",
        display_name=user.get("full_name") or "",
        avatar_url=user.get("profile_pic_url"),
        is_verified=bool(user.get("is_verified")),
    )


def _post(raw: dict, *, depth: int, parent_id: str | None, op_id: str | None) -> Post:
    info = raw.get("text_post_app_info") or {}
    author = _author(raw["user"])
    taken_at = raw.get("taken_at")
    return Post(
        id=str(raw["pk"]),
        code=raw.get("code"),
        author=author,
        text=_text(raw),
        created_at=datetime.fromtimestamp(taken_at, tz=timezone.utc) if isinstance(taken_at, (int, float)) else None,
        like_count=int(raw.get("like_count") or 0),
        reply_count=int(info.get("direct_reply_count") or 0),
        repost_count=int(info.get("repost_count") or 0),
        quote_count=int(info.get("quote_count") or 0),
        media=_media(raw),
        depth=depth,
        parent_id=parent_id,
        is_op=op_id is not None and author.id == op_id,
    )


def _richness(raw: dict) -> int:
    """Prefer the copy of a post that carries the most user/post detail."""
    return len(raw) + len(raw.get("user") or {}) + (10 if (raw.get("user") or {}).get("username") else 0)


def parse_payloads(payloads: list[Any], *, code: str, source_url: str, logged_in: bool = False) -> ThreadData:
    # Pass 1: find the fetched post (richest copy, matched by shortcode).
    main_raw: dict | None = None
    for payload in payloads:
        for d in _walk(payload):
            if _is_post(d) and d.get("code") == code and (main_raw is None or _richness(d) > _richness(main_raw)):
                main_raw = d
    if main_raw is None:
        raise ParseError(
            f"Post {code} not found in {len(payloads)} payload(s). The post may be private or deleted, "
            "or Threads showed a login wall; try a logged-in profile."
        )
    main = _post(main_raw, depth=0, parent_id=None, op_id=None)
    main.is_op = True
    op_id = main.author.id

    # Pass 2: reply chains and self-thread hanging off the fetched post.
    replies: dict[str, tuple[Post, int]] = {}
    self_thread: dict[str, Post] = {}
    has_more = False

    def add_reply(raw: dict, depth: int, parent: str | None) -> None:
        post = _post(raw, depth=depth, parent_id=parent, op_id=op_id)
        prev = replies.get(post.id)
        if prev is None or _richness(raw) > prev[1]:
            replies[post.id] = (post, _richness(raw))

    for payload in payloads:
        for d in _walk(payload):
            info = d.get("text_post_app_info")
            if not isinstance(info, dict) or _media_pk(d) != main.id:
                continue
            direct = info.get("direct_replies")
            if isinstance(direct, dict):
                has_more = has_more or bool((direct.get("page_info") or {}).get("has_next_page"))
                for thread_view in _edges(direct):
                    parent = main.id
                    for depth, raw in enumerate(_edges(thread_view.get("posts")), start=1):
                        if not _is_post(raw):
                            break
                        add_reply(raw, depth, parent)
                        parent = str(raw["pk"])
            for raw in _edges((info.get("self_thread") or {}).get("posts")):
                if _is_post(raw) and str(raw["pk"]) != main.id:
                    self_thread.setdefault(str(raw["pk"]), _post(raw, depth=0, parent_id=main.id, op_id=op_id))

    ordered = [p for p, _ in replies.values() if p.id not in self_thread]
    if main.reply_count and len([r for r in ordered if r.depth == 1]) < main.reply_count:
        has_more = True

    return ThreadData(
        source_url=source_url,
        fetched_at=datetime.now(timezone.utc),
        post=main,
        self_thread=list(self_thread.values()),
        replies=ordered,
        has_more_replies=has_more,
        logged_in=logged_in,
    )
