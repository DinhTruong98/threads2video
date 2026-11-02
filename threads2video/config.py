"""Application settings: one pydantic model, persisted as YAML (or JSON by suffix).

Secrets (the Gemini API key) live only in the user's local config file, which is
git-ignored, or in the VERTEX_API_KEY / GEMINI_API_KEY environment variable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    @field_validator("*")
    @classmethod
    def _expand_home(cls, v):
        # "~/Movies/..." typed or pasted into a path field means the user's home folder.
        return v.expanduser() if isinstance(v, Path) else v


class FetchConfig(_Section):
    headless: bool = True
    # Persistent Chromium profile; log in once with `threads2video --login`.
    profile_dir: Path | None = None
    # Cookies exported from a browser (Playwright/JSON-extension format).
    cookies_file: Path | None = None
    max_scrolls: int = Field(12, ge=0, le=200)
    scroll_pause_s: float = Field(1.5, ge=0.2)
    timeout_s: float = Field(45.0, ge=5)
    retries: int = Field(2, ge=0, le=10)


class SelectionConfig(_Section):
    top_comments: int = Field(5, ge=0, le=100)
    op_replies: int = Field(2, ge=0, le=50)
    min_chars: int = Field(3, ge=0)
    max_chars: int = Field(400, ge=1)
    skip_links: bool = True
    skip_emoji_only: bool = True
    include_self_thread: bool = False
    # Put the comment an OP reply answers right before it, even if it is not a top comment.
    op_reply_with_parent: bool = True
    review_before_tts: bool = False


class TTSConfig(_Section):
    provider: Literal["edge", "vieneu", "piper", "capcut"] = "edge"
    # Tried in order when the provider fails; a video is always read by a single provider.
    fallback: list[Literal["edge", "vieneu", "piper", "capcut"]] = ["vieneu", "edge", "piper"]
    voice: str = "vi-VN-HoaiMyNeural"
    op_voice: str | None = "vi-VN-NamMinhNeural"
    random_voice_per_author: bool = True
    speed: float = Field(1.10, ge=0.5, le=2.0)
    piper_models_dir: Path = Path("models/piper")
    vieneu_models_dir: Path = Path("models/vieneu")


class TranslateConfig(_Section):
    enabled: bool = False
    # Where the model runs (also for the AI step): "vertex" = Vertex AI API key, "gemini" = Gemini API key.
    service: Literal["vertex", "gemini"] = "vertex"
    model: str = "gemini-3.5-flash-lite"
    api_key: str = Field("", repr=False)


class AIConfig(_Section):
    # Gemini (key and model from the translate section) reads the whole thread at once.
    # Rewrite abbreviations/teencode into full words for the voice ("ko" → "không", "T" → "tao").
    speech_text: bool = False
    # Pick a reaction sound (laugh, gasp, aww, ...) for comments where an audience would react.
    reactions: bool = False
    # Send post/comment images too, so jokes that live in a picture are understood.
    use_images: bool = True


class VideoConfig(_Section):
    aspect: Literal["9:16", "16:9"] = "9:16"
    fps: int = Field(30, ge=15, le=60)
    # A file, or a folder to pick a random file from.
    background: Path | None = None
    # Pick a random start point in the background video.
    random_start: bool = True
    card_position: Literal["center", "top", "bottom"] = "center"
    animation: Literal["none", "fade", "slide"] = "fade"
    animation_s: float = Field(0.25, ge=0, le=2)
    subtitles: bool = False
    # Timing: silence before the first card, between a card appearing and its
    # narration (room for the SFX), between narrations, and after the last one.
    lead_in_s: float = Field(0.2, ge=0)
    pre_roll_s: float = Field(0.15, ge=0)
    gap_s: float = Field(0.35, ge=0)
    tail_s: float = Field(0.8, ge=0)
    # Extra pause after a comment that gets a reaction sound, so the laugh lands before the next card.
    reaction_pause_s: float = Field(1.0, ge=0, le=5)
    crf: int = Field(20, ge=0, le=51)
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"] = "veryfast"

    @property
    def size(self) -> tuple[int, int]:
        return (1080, 1920) if self.aspect == "9:16" else (1920, 1080)


class AudioConfig(_Section):
    music_enabled: bool = True
    music: Path | None = None
    music_volume: int = Field(20, ge=0, le=100)
    sfx_enabled: bool = True
    # A file or folder; empty uses the built-in pop.
    sfx_dir: Path | None = None
    sfx_volume: int = Field(40, ge=0, le=100)
    # Reaction sounds picked by the AI step (ai.reactions). Built-in CC0 sounds are used for
    # moods with no files in reactions_dir/<mood>/ (laugh, rimshot, gasp, aww, applause, crickets, fail, wow).
    reactions_dir: Path | None = None
    reaction_volume: int = Field(60, ge=0, le=100)
    music_fade_out_s: float = Field(2.0, ge=0)


class CardConfig(_Section):
    theme: Literal["dark", "light"] = "dark"
    # Hide usernames, display names, avatars and @mentions.
    identity: Literal["show", "blur", "black"] = "show"
    # Card width as a fraction of the frame width (portrait) / frame width (landscape).
    width_portrait: float = Field(0.88, ge=0.3, le=1.0)
    width_landscape: float = Field(0.55, ge=0.2, le=1.0)
    # Tallest a card may be, as a fraction of the frame height; text shrinks to fit.
    max_height: float = Field(0.8, ge=0.2, le=1.0)
    font_scale: float = Field(1.0, ge=0.5, le=2.0)
    show_media: bool = True
    show_counts: bool = True


class AppConfig(_Section):
    # Reading language: TTS voice language and translation target.
    language: str = "vi"
    ffmpeg_path: str | None = None
    cache_dir: Path = Path("cache")
    work_dir: Path = Path("work")
    output_dir: Path = Path("output")

    fetch: FetchConfig = FetchConfig()
    selection: SelectionConfig = SelectionConfig()
    tts: TTSConfig = TTSConfig()
    translate: TranslateConfig = TranslateConfig()
    ai: AIConfig = AIConfig()
    video: VideoConfig = VideoConfig()
    audio: AudioConfig = AudioConfig()
    card: CardConfig = CardConfig()


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: Path | str | None, defaults: AppConfig | None = None) -> AppConfig:
    """Load config from YAML/JSON; a missing path yields defaults. Env vars override secrets.

    Settings missing from the file come from `defaults` (e.g. the GUI's per-user paths),
    so options added in a newer version get sensible values in an older config file.
    """
    data: dict = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            text = p.read_text(encoding="utf-8")
            data = (json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)) or {}
    if defaults is not None:
        data = _merge(defaults.model_dump(mode="json"), data)
    cfg = AppConfig.model_validate(data)
    if key := os.environ.get("VERTEX_API_KEY" if cfg.translate.service == "vertex" else "GEMINI_API_KEY"):
        cfg.translate.api_key = key
    return cfg


def save_config(cfg: AppConfig, path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    if p.suffix == ".json":
        text = json.dumps(data, indent=2, ensure_ascii=False)
    else:
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    p.write_text(text, encoding="utf-8")
