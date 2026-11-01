"""Burned-in subtitle captions rendered as transparent PNGs (no libass needed).

Many FFmpeg builds ship without libass, so instead of the `subtitles` filter the
composer overlays these images the same way it overlays cards.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import browser_env

CAPTION_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html, body { margin: 0; background: transparent; }
#c { display: inline-block; max-width: %(max_w)dpx; padding: 8px 14px; text-align: center;
     font: 800 %(fs)dpx/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, "Noto Sans", sans-serif,
           "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji";
     color: #fff; -webkit-text-stroke: %(stroke)spx #000; paint-order: stroke fill;
     text-shadow: 0 3px 8px rgba(0,0,0,.55); overflow-wrap: anywhere; }
</style></head><body><div id="c"></div></body></html>"""

# Text is sized against the frame's short side (CSS_SHORT_SIDE CSS px), so captions
# look the same in 9:16 and 16:9; the device scale factor maps CSS px to video px.
CSS_SHORT_SIDE = 540


def render_captions(texts: list[str], out_dir: Path, *, frame_size: tuple[int, int]) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = frame_size
    scale = min(w, h) / CSS_SHORT_SIDE
    css_width = round(w / scale)
    html = CAPTION_HTML % {"max_w": int(min(css_width * 0.9, CSS_SHORT_SIDE * 1.1)), "fs": 30, "stroke": 6}
    paths = []
    browser_env.configure()
    with sync_playwright() as pw:
        browser = browser_env.launch(pw)
        try:
            page = browser.new_page(viewport={"width": css_width, "height": 400}, device_scale_factor=scale)
            page.set_content(html)
            for i, text in enumerate(texts):
                page.evaluate(f"document.getElementById('c').textContent = {json.dumps(text)}")
                path = out_dir / f"cap_{i:03d}.png"
                page.locator("#c").screenshot(path=str(path), omit_background=True)
                paths.append(path)
        finally:
            browser.close()
    return paths
