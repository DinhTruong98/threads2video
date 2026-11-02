"""Per-user folders for the GUI/packaged app (the CLI keeps paths relative to the working directory)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import AppConfig

APP = "threads2video"


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


def data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP
    if sys.platform == "darwin":
        return config_dir()
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP


def videos_dir() -> Path:
    return Path.home() / ("Movies" if sys.platform == "darwin" else "Videos") / APP


def gui_config_path() -> Path:
    return config_dir() / "config.yaml"


def gui_defaults() -> AppConfig:
    """Defaults with absolute per-user paths, so the app works wherever it is started from."""
    d = data_dir()
    cfg = AppConfig(cache_dir=d / "cache", work_dir=d / "work", output_dir=videos_dir())
    cfg.tts.piper_models_dir = d / "models" / "piper"
    cfg.tts.vieneu_models_dir = d / "models" / "vieneu"
    cfg.fetch.profile_dir = d / "browser-profile"
    return cfg
