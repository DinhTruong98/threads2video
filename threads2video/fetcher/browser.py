"""Playwright side of the fetcher: load the post page and collect raw JSON.

Every payload is written to `<cache>/threads/<code>/raw/` before parsing, so a
Threads schema change can be debugged (and the parser re-run) offline.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Response, sync_playwright

from .. import browser_env
from ..config import FetchConfig
from ..models import ThreadData
from ..retry import retry
from .parser import ParseError, loads_payload, parse_payloads
from .urls import parse_post_url

log = logging.getLogger(__name__)

VIEWPORT = {"width": 1280, "height": 1800}
# Consecutive scrolls without a new reply before we stop.
_STALL_LIMIT = 3


class FetchError(RuntimeError):
    pass


def thread_cache_dir(cache_dir: Path, code: str) -> Path:
    return Path(cache_dir) / "threads" / code


def _load_cookies(path: Path) -> list[dict]:
    """Read cookies exported as Playwright JSON, a browser-extension JSON list, or Netscape cookies.txt."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        raw = data.get("cookies", []) if isinstance(data, dict) else data
    else:
        raw = []
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) == 7 and not line.startswith("#"):
                raw.append({"domain": parts[0], "path": parts[2], "secure": parts[3] == "TRUE",
                            "expires": int(parts[4] or 0), "name": parts[5], "value": parts[6]})
    cookies = []
    same_site = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
    for c in raw:
        if not c.get("name") or "threads" not in (c.get("domain") or ""):
            continue
        cookie = {"name": c["name"], "value": c.get("value", ""), "domain": c["domain"], "path": c.get("path") or "/",
                  "secure": bool(c.get("secure", True)), "httpOnly": bool(c.get("httpOnly", False))}
        expires = c.get("expires", c.get("expirationDate"))
        if expires and float(expires) > 0:
            cookie["expires"] = float(expires)
        if (ss := same_site.get(str(c.get("sameSite", "")).lower())) is not None:
            cookie["sameSite"] = ss
        cookies.append(cookie)
    return cookies


@contextmanager
def _context(cfg: FetchConfig, *, headless: bool | None = None) -> Iterator[BrowserContext]:
    headless = cfg.headless if headless is None else headless
    browser_env.configure()
    with sync_playwright() as pw:
        if cfg.profile_dir:
            Path(cfg.profile_dir).mkdir(parents=True, exist_ok=True)
            ctx = browser_env.launch_persistent(
                pw, str(cfg.profile_dir), headless=headless, viewport=VIEWPORT, locale="en-US"
            )
            browser = None
        else:
            browser = browser_env.launch(pw, headless=headless)
            ctx = browser.new_context(viewport=VIEWPORT, locale="en-US")
        try:
            if cfg.cookies_file:
                cookies = _load_cookies(Path(cfg.cookies_file))
                log.info("Loaded %d Threads cookies from %s", len(cookies), cfg.cookies_file)
                ctx.add_cookies(cookies)
            ctx.set_default_timeout(cfg.timeout_s * 1000)
            yield ctx
        finally:
            ctx.close()
            if browser:
                browser.close()


def _is_logged_in(ctx: BrowserContext) -> bool:
    return any(c["name"] == "sessionid" and c["value"] for c in ctx.cookies("https://www.threads.com"))


def _collect_scripts(page: Page) -> list[Any]:
    texts = page.eval_on_selector_all(
        'script[type="application/json"]',
        "els => els.map(e => e.textContent).filter(t => t.includes('text_post_app_info'))",
    )
    return [p for t in texts for p in loads_payload(t)]


def _fetch_once(url: str, cfg: FetchConfig, raw_dir: Path) -> ThreadData:
    ref = parse_post_url(url)
    network: list[Any] = []

    def on_response(resp: Response) -> None:
        if "/graphql" not in resp.url or resp.status != 200:
            return
        try:
            body = resp.text()
        except PlaywrightError:
            return
        if "text_post_app_info" in body:
            network.extend(loads_payload(body))

    with _context(cfg) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        log.info("Opening %s", ref.url)
        resp = page.goto(ref.url, wait_until="domcontentloaded")
        if resp is not None and resp.status >= 400:
            raise FetchError(f"Threads returned HTTP {resp.status} for {ref.url}")
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightError:
            pass  # Threads keeps long-lived connections; idle is best-effort.
        logged_in = _is_logged_in(ctx)
        log.info("Session: %s", "logged in" if logged_in else "logged out")

        def parse() -> ThreadData:
            return parse_payloads(_collect_scripts(page) + network, code=ref.code, source_url=ref.url, logged_in=logged_in)

        thread = parse()
        stall = 0
        for i in range(cfg.max_scrolls):
            if not thread.has_more_replies or stall >= _STALL_LIMIT:
                break
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(cfg.scroll_pause_s * 1000)
            before = len(thread.replies)
            thread = parse()
            stall = stall + 1 if len(thread.replies) == before else 0
            log.debug("scroll %d: %d replies", i + 1, len(thread.replies))
        payloads = _collect_scripts(page) + network

    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("*.json"):
        old.unlink()
    for i, p in enumerate(payloads):
        (raw_dir / f"{i:04d}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    return parse_payloads(payloads, code=ref.code, source_url=ref.url, logged_in=logged_in)


def fetch_thread(url: str, cfg: FetchConfig, *, cache_dir: Path) -> ThreadData:
    """Load a Threads post with Playwright and return the post plus its replies."""
    ref = parse_post_url(url)
    out_dir = thread_cache_dir(cache_dir, ref.code)
    thread = retry(
        lambda: _fetch_once(url, cfg, out_dir / "raw"),
        attempts=cfg.retries + 1,
        what=f"Fetching {ref.code}",
        retry_on=(PlaywrightError, FetchError, ParseError),
    )
    (out_dir / "thread.json").write_text(thread.model_dump_json(indent=2), encoding="utf-8")
    msg = "Fetched @%s/%s: %d replies (%d by OP), %d self-thread posts"
    log.info(msg, thread.post.author.username, ref.code, len(thread.replies), len(thread.op_replies), len(thread.self_thread))
    if thread.has_more_replies:
        hint = "" if thread.logged_in else " Log in to Threads (GUI button or `threads2video --login`) to load more."
        log.warning("Only %d of %d direct replies were loaded.%s",
                    sum(r.depth == 1 for r in thread.replies), thread.post.reply_count, hint)
    return thread


def load_cached_thread(url: str, cache_dir: Path) -> ThreadData:
    """Re-parse raw payloads saved by a previous fetch (no network)."""
    ref = parse_post_url(url)
    raw_dir = thread_cache_dir(cache_dir, ref.code) / "raw"
    files = sorted(raw_dir.glob("*.json"))
    if not files:
        raise FetchError(f"No cached payloads in {raw_dir}")
    payloads = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    return parse_payloads(payloads, code=ref.code, source_url=ref.url)


def login_interactive(cfg: FetchConfig) -> None:
    """Open a visible browser on the persistent profile so the user can log in once."""
    if not cfg.profile_dir:
        raise FetchError("Set fetch.profile_dir in the config before logging in.")
    with _context(cfg, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.threads.com/login")
        log.info("Log in in the browser window, then close it to save the session.")
        page.wait_for_event("close", timeout=0)
        log.info("Session saved to %s (logged in: %s)", cfg.profile_dir, _is_logged_in(ctx))
