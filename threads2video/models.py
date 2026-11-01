"""Typed data passed between pipeline stages."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Author(BaseModel):
    id: str
    username: str
    display_name: str = ""
    avatar_url: str | None = None
    is_verified: bool = False


class MediaItem(BaseModel):
    kind: Literal["image", "video"]
    url: str
    width: int | None = None
    height: int | None = None


class Post(BaseModel):
    id: str
    code: str | None = None
    author: Author
    text: str = ""
    created_at: datetime | None = None
    like_count: int = 0
    reply_count: int = 0
    repost_count: int = 0
    quote_count: int = 0
    media: list[MediaItem] = Field(default_factory=list)
    # Reply-tree position: 0 = the fetched post, 1 = direct reply, 2+ = nested.
    depth: int = 0
    parent_id: str | None = None
    # Written by the author of the fetched post.
    is_op: bool = False

    @property
    def url(self) -> str | None:
        if not self.code:
            return None
        return f"https://www.threads.com/@{self.author.username}/post/{self.code}"


class ThreadData(BaseModel):
    source_url: str
    fetched_at: datetime
    post: Post
    # The OP's own continuation posts directly under the fetched post.
    self_thread: list[Post] = Field(default_factory=list)
    # All replies seen, direct and nested, in page order.
    replies: list[Post] = Field(default_factory=list)
    # Threads reported more replies than were loaded (often a logged-out cap).
    has_more_replies: bool = False
    logged_in: bool = False

    @property
    def op_replies(self) -> list[Post]:
        return [r for r in self.replies if r.is_op]


class Segment(BaseModel):
    """One narrated card: the post, a reply, or an OP reply, in video order."""

    id: str
    kind: Literal["post", "self_thread", "reply", "op_reply"]
    author: Author
    is_op: bool = False
    like_count: int = 0
    reply_count: int = 0
    media: list[MediaItem] = Field(default_factory=list)
    created_at: datetime | None = None
    text_original: str
    # What is shown on the card and read aloud (translated and/or edited).
    text: str
    # Filled by the TTS stage.
    voice: str | None = None
    audio: str | None = None
    duration: float | None = None
    # Filled by the render stage: transparent PNG of the card.
    card: str | None = None
