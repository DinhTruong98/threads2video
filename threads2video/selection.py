"""Pick which replies make it into the video, and in what order.

Order: the post, optional self-thread posts, then the top N non-OP replies by
likes. Each selected OP reply follows the comment it answers; when that comment
is not among the top N it is pulled in as context (`op_reply_with_parent`).
"""

from __future__ import annotations

import re

from .config import SelectionConfig
from .models import Post, Segment, ThreadData

URL_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|vn|io|ly|me|co|app|gg|tv|xyz|link|shop)(?:/\S*)?\b",
    re.IGNORECASE,
)
_ALNUM_RE = re.compile(r"[^\W_]")


def rejection_reason(text: str, cfg: SelectionConfig) -> str | None:
    """Why a reply is filtered out, or None if it is usable."""
    t = text.strip()
    if not t:
        return "empty"
    if cfg.skip_links and URL_RE.search(t):
        return "contains a link"
    if cfg.skip_emoji_only and not _ALNUM_RE.search(t):
        return "emoji/symbols only"
    if len(t) < cfg.min_chars:
        return "too short"
    if len(t) > cfg.max_chars:
        return "too long"
    return None


def _segment(post: Post, kind: str) -> Segment:
    return Segment(
        id=post.id,
        kind=kind,
        author=post.author,
        is_op=post.is_op,
        like_count=post.like_count,
        reply_count=post.reply_count,
        media=post.media,
        created_at=post.created_at,
        text_original=post.text,
        text=post.text,
    )


def _by_likes(posts: list[Post]) -> list[Post]:
    # sorted() is stable, so equal likes keep page order.
    return sorted(posts, key=lambda p: -p.like_count)


def select_segments(thread: ThreadData, cfg: SelectionConfig) -> list[Segment]:
    segments = [_segment(thread.post, "post")]
    if cfg.include_self_thread:
        segments += [_segment(p, "self_thread") for p in thread.self_thread if p.text.strip()]

    seen_texts: set[str] = set()
    usable: list[Post] = []
    for r in thread.replies:
        key = " ".join(r.text.lower().split())
        if rejection_reason(r.text, cfg) is None and key not in seen_texts:
            seen_texts.add(key)
            usable.append(r)
    by_id = {r.id: r for r in usable}

    top = _by_likes([r for r in usable if not r.is_op])[: cfg.top_comments]
    op = _by_likes([r for r in usable if r.is_op])[: cfg.op_replies]

    ordered: list[Post] = list(top)
    for reply in op:
        if any(p.id == reply.id for p in ordered):  # already pulled in as another OP reply's parent
            continue
        if reply.parent_id in {p.id for p in ordered}:
            # Directly after its parent, after any earlier OP replies to the same parent.
            i = next(i for i, p in enumerate(ordered) if p.id == reply.parent_id) + 1
            while i < len(ordered) and ordered[i].is_op and ordered[i].parent_id == reply.parent_id:
                i += 1
            ordered.insert(i, reply)
        elif cfg.op_reply_with_parent and (parent := by_id.get(reply.parent_id or "")) is not None:
            ordered += [parent, reply]
        else:
            ordered.append(reply)

    segments += [_segment(p, "op_reply" if p.is_op else "reply") for p in ordered]
    return segments
