from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_HOSTS = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
_POST_RE = re.compile(r"^/@(?P<user>[\w.]+)/post/(?P<code>[\w-]+)/?")
_SHORT_RE = re.compile(r"^/t/(?P<code>[\w-]+)/?")


@dataclass(frozen=True)
class PostRef:
    code: str
    username: str | None

    @property
    def url(self) -> str:
        if self.username:
            return f"https://www.threads.com/@{self.username}/post/{self.code}"
        return f"https://www.threads.com/t/{self.code}"


def parse_post_url(url: str) -> PostRef:
    """Accept threads.com/threads.net post URLs (`/@user/post/CODE` or `/t/CODE`)."""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    u = urlparse(raw)
    if u.hostname not in _HOSTS:
        raise ValueError(f"Not a Threads URL: {url!r}")
    if m := _POST_RE.match(u.path):
        return PostRef(code=m["code"], username=m["user"])
    if m := _SHORT_RE.match(u.path):
        return PostRef(code=m["code"], username=None)
    raise ValueError(f"Not a Threads post URL (expected /@user/post/CODE): {url!r}")
