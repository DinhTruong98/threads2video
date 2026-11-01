"""Render each segment as a Threads-style card: HTML template → Playwright → transparent PNG.

The template is laid out at a fixed CSS width (BASE_WIDTH) and screenshotted
with a device scale factor that makes the PNG exactly as wide as the card
should be in the video, so text stays sharp at any output size.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .. import browser_env
from ..config import CardConfig, VideoConfig
from ..models import Segment

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).parent / "templates" / "card.html"
BASE_WIDTH = 420  # CSS px of the card inside the template
FONT_SIZE = 17.0
MIN_FONT_SIZE = 12.0
# #wrap padding around the card (room for the shadow), in CSS px.
WRAP_PAD_X = 14
OP_LABEL = {"vi": "Tác giả", "en": "Author"}

Progress = Callable[[int, int, str], None]


@dataclass(frozen=True)
class CardLayout:
    card_px: int  # card width in output pixels (without the shadow margin)
    scale: float  # device scale factor
    max_height_css: float

    @property
    def png_width(self) -> int:
        return round((BASE_WIDTH + 2 * WRAP_PAD_X) * self.scale)


# With burned-in subtitles the bottom of the frame is reserved for captions.
SUBTITLE_CARD_MAX_HEIGHT = 0.68


def card_layout(video: VideoConfig, card: CardConfig) -> CardLayout:
    w, h = video.size
    card_px = round(w * (card.width_portrait if h > w else card.width_landscape))
    scale = card_px / BASE_WIDTH
    max_height = min(card.max_height, SUBTITLE_CARD_MAX_HEIGHT) if video.subtitles else card.max_height
    return CardLayout(card_px=card_px, scale=scale, max_height_css=h * max_height / scale)


def compact_count(n: int) -> str:
    if n <= 0:
        return ""
    if n < 1000:
        return str(n)
    value, suffix = (n / 1_000_000, "M") if n >= 1_000_000 else (n / 1000, "K")
    text = f"{value:.1f}".rstrip("0").rstrip(".") if value < 100 else f"{value:.0f}"
    return text + suffix


def time_label(created: datetime | None, now: datetime, lang: str = "vi") -> str:
    """Threads-style relative time: 5m, 3h, 2d, then a date."""
    if created is None:
        return ""
    secs = max(0, (now - created).total_seconds())
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d"
    return created.strftime("%m/%d/%y" if lang.startswith("en") else "%d/%m/%y")


class AssetCache:
    """Downloads avatars/images once per job and hands them to the page as data URIs.

    Threads CDN URLs are signed and expire, so they are fetched right before rendering.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _path(self, url: str) -> Path:
        # The signature query changes between fetches; the path identifies the image.
        return self.folder / hashlib.sha1(urlparse(url).path.encode()).hexdigest()

    def fetch(self, url: str | None) -> str | None:
        if not url or not url.startswith("http"):
            return None
        path = self._path(url)
        if not path.exists():
            import httpx

            try:
                resp = httpx.get(url, follow_redirects=True, timeout=20)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("Could not download %s…: %s", url[:60], e)
                return None
            self.folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
            path.with_suffix(".type").write_text(resp.headers.get("content-type", "image/jpeg").split(";")[0])
        mime_file = path.with_suffix(".type")
        mime = mime_file.read_text() if mime_file.exists() else (mimetypes.guess_type(url)[0] or "image/jpeg")
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

    def prefetch(self, urls: list[str | None]) -> dict[str, str | None]:
        unique = sorted({u for u in urls if u})
        with ThreadPoolExecutor(max_workers=6) as pool:
            return dict(zip(unique, pool.map(self.fetch, unique)))


def _first_image(seg: Segment) -> str | None:
    return next((m.url for m in seg.media if m.kind == "image"), None)


def card_data(seg: Segment, assets: dict[str, str | None], layout: CardLayout, card: CardConfig,
              lang: str, now: datetime) -> dict:
    hide = card.identity != "show"
    return {
        "theme": card.theme,
        "identity": card.identity,
        "cardWidth": BASE_WIDTH,
        "maxHeight": layout.max_height_css,
        "fontSize": FONT_SIZE * card.font_scale,
        "minFontSize": MIN_FONT_SIZE * card.font_scale,
        "username": seg.author.username,
        "verified": seg.author.is_verified and not hide,
        "avatar": assets.get(seg.author.avatar_url or "") if not hide else None,
        "pill": OP_LABEL.get(lang.split("-")[0], OP_LABEL["en"]) if seg.kind == "op_reply" else "",
        "time": time_label(seg.created_at, now, lang),
        "text": seg.text,
        "media": assets.get(_first_image(seg) or "") if card.show_media else None,
        "likes": compact_count(seg.like_count),
        "replies": compact_count(seg.reply_count),
        "showActions": card.show_counts,
    }


def render_cards(segments: list[Segment], out_dir: Path, *, video: VideoConfig, card: CardConfig,
                 lang: str, progress: Progress | None = None) -> list[Segment]:
    """Write one PNG per segment to out_dir and return the segments with `card` set."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    layout = card_layout(video, card)
    assets = AssetCache(out_dir.parent / "assets")
    urls = [s.author.avatar_url for s in segments] if card.identity == "show" else []
    urls += [_first_image(s) for s in segments] if card.show_media else []
    data_uris = assets.prefetch(urls)
    now = datetime.now(timezone.utc)

    result = []
    browser_env.configure()
    with sync_playwright() as pw:
        browser = browser_env.launch(pw)
        try:
            page = browser.new_page(
                viewport={"width": BASE_WIDTH + 2 * WRAP_PAD_X, "height": int(layout.max_height_css) + 200},
                device_scale_factor=layout.scale,
            )
            page.goto(TEMPLATE.as_uri())
            for i, seg in enumerate(segments):
                if progress:
                    progress(i, len(segments), f"Card {i + 1}/{len(segments)}")
                info = page.evaluate("d => renderCard(d)", card_data(seg, data_uris, layout, card, lang, now))
                if info["clampedLines"]:
                    log.warning("Card %d text too long for the frame; cut to %d lines", i, info["clampedLines"])
                elif info["mediaDropped"]:
                    log.info("Card %d: image dropped to fit the text", i)
                path = out_dir / f"{i:02d}_{seg.id}.png"
                page.locator("#wrap").screenshot(path=str(path), omit_background=True)
                result.append(seg.model_copy(update={"card": str(path)}))
        finally:
            browser.close()
    if progress:
        progress(len(segments), len(segments), "Cards done")
    log.info("Rendered %d cards (%d px wide) → %s", len(result), layout.png_width, out_dir)
    return result
