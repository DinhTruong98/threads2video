"""Process setup shared by the CLI and GUI entry points."""

from __future__ import annotations

import os
import sys

# PyInstaller points these at the bundle so the app finds its own libraries;
# child processes (ffmpeg, Chromium, Playwright's Node.js) must not inherit
# that, or e.g. a system ffmpeg loads Qt's bundled libavutil and crashes.
_LIBRARY_VARS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH")


def restore_child_env() -> None:
    if not getattr(sys, "frozen", False):
        return
    for var in _LIBRARY_VARS:
        orig = os.environ.get(f"{var}_ORIG")
        if orig is not None:
            os.environ[var] = orig
        else:
            os.environ.pop(var, None)


def prepare() -> None:
    from . import browser_env

    restore_child_env()
    browser_env.configure()
