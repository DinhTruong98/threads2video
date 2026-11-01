"""Turn a cached fetch (cache/threads/<code>/raw) into an anonymised test fixture.

Usernames, names, ids, avatars, media URLs, shortcodes and texts are replaced
with deterministic fakes so real people's posts never land in the repo, while
the JSON structure (what the parser depends on) is kept intact.

    python tools/make_fixture.py cache/threads/<code> tests/fixtures/thread_sample
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DROP_KEYS = {"accessibility_caption", "logging_info_token", "fetch_token", "organic_tracking_token",
             "transcription_data", "end_cursor", "interop_messaging_user_fbid", "fbid"}
WORDS = ("hôm nay mình thấy bài này hay quá nên muốn chia sẻ với mọi người cùng đọc nhé "
         "thật sự không ngờ lại có nhiều người đồng ý như vậy cảm ơn bạn đã viết").split()


class Anonymizer:
    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.codes: dict[str, str] = {}
        self.texts: dict[str, str] = {}

    def user(self, name: str) -> str:
        return self.users.setdefault(name, f"user_{len(self.users) + 1}")

    def id(self, value: str) -> str:
        return self.ids.setdefault(str(value), str(9_000_000_000 + len(self.ids)))

    def code(self, value: str) -> str:
        return self.codes.setdefault(value, f"SAMPLE{len(self.codes) + 1:03d}")

    def text(self, value: str) -> str:
        if value in self.texts:
            return self.texts[value]
        if not re.search(r"\w", value):  # emoji/punctuation only: keep, the selection filters need it
            return value
        n = max(1, len(value.split()))
        i = len(self.texts)
        out = " ".join(WORDS[(i + k) % len(WORDS)] for k in range(n)).capitalize() + "."
        return self.texts.setdefault(value, out)

    def walk(self, o):
        if isinstance(o, list):
            return [self.walk(x) for x in o]
        if not isinstance(o, dict):
            return o
        out = {}
        is_user = "username" in o
        for k, v in o.items():
            if k in DROP_KEYS:
                continue
            if is_user and k == "username" and isinstance(v, str):
                out[k] = self.user(v)
            elif is_user and k in ("pk", "id", "pk_id") and v:
                out[k] = self.id(v)
            elif k == "full_name" and isinstance(v, str):
                out[k] = f"Sample {self.user(o.get('username', v))}"
            elif k in ("display_name", "tag_cluster_name") and isinstance(v, str):
                out[k] = "Sample topic"
            elif k in ("profile_pic_url", "url", "canonical_url") and isinstance(v, str) and v.startswith("http"):
                out[k] = "https://example.invalid/media.jpg"
            elif k == "code" and isinstance(v, str):
                out[k] = self.code(v)
            elif k in ("text", "plaintext") and isinstance(v, str):
                out[k] = self.text(v)
            else:
                out[k] = self.walk(v)
        return out


def main(src: str, dst: str) -> None:
    src_dir, dst_dir = Path(src), Path(dst)
    raw = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((src_dir / "raw").glob("*.json"))]
    anon = Anonymizer()
    # Post owners first so the OP is user_1 and the fetched post is SAMPLE001.
    thread = json.loads((src_dir / "thread.json").read_text(encoding="utf-8"))
    anon.user(thread["post"]["author"]["username"])
    anon.code(thread["post"]["code"])
    payloads = anon.walk(raw)
    text = json.dumps(payloads, ensure_ascii=False)
    # Mentions and other free-text occurrences of real usernames.
    for real, fake in sorted(anon.users.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<![\w.]){re.escape(real)}(?![\w])", fake, text)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for old in dst_dir.glob("*.json"):
        old.unlink()
    for i, p in enumerate(json.loads(text)):
        (dst_dir / f"{i:04d}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    meta = {"url": f"https://www.threads.com/@user_1/post/SAMPLE001", "users": len(anon.users), "payloads": len(payloads)}
    (dst_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(meta)


if __name__ == "__main__":
    main(*sys.argv[1:3])
