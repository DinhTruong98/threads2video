"""Where Playwright's Chromium lives, and installing it on first run.

Everything uses Playwright's full Chromium build (`channel="chromium"`, which runs
headless in Chrome's new headless mode), so a single browser serves fetching,
card rendering and the visible login window, and the separate
headless-shell download (~200 MB) is never needed.

In the packaged app, a `ms-playwright` folder next to the executable (bundled
browser) wins; otherwise the browser is installed into the user data folder.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

CHANNEL = "chromium"


def configure() -> None:
    """Point Playwright at the right browser folder; call before launching a browser."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or not getattr(sys, "frozen", False):
        return
    bundled = Path(sys.executable).parent / "ms-playwright"
    if bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    else:
        from .appdirs import data_dir

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(data_dir() / "ms-playwright")


def launch(pw: Any, **kwargs: Any) -> Any:
    return pw.chromium.launch(channel=CHANNEL, **kwargs)


def launch_persistent(pw: Any, profile_dir: str, **kwargs: Any) -> Any:
    return pw.chromium.launch_persistent_context(profile_dir, channel=CHANNEL, **kwargs)


def browser_installed() -> bool:
    from playwright.sync_api import sync_playwright

    configure()
    with sync_playwright() as pw:
        return Path(pw.chromium.executable_path).exists()


def install_browser(on_line: Callable[[str], None] | None = None) -> None:
    """Download Chromium with Playwright's own installer (about 170 MB download)."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    configure()
    node, cli = compute_driver_executable()
    cmd = [node, cli, "install", "chromium", "--no-shell"]
    log.info("Installing Chromium for Playwright…")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, env=get_driver_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=flags)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line and on_line:
            on_line(line)
    if proc.wait() != 0:
        raise RuntimeError("Installing Chromium failed; check your internet connection and try again")
    log.info("Chromium installed")


def ensure_browser(on_line: Callable[[str], None] | None = None) -> None:
    if not browser_installed():
        install_browser(on_line)
