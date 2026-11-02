"""Runs the stages for one URL. Used by both the CLI and the GUI.

Each stage writes into `<work_dir>/<code>/`, so the stages can be inspected,
edited (segments.json) and resumed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .compose import compose
from .config import AppConfig
from .enrich import enrich_segments
from .fetcher import fetch_thread, load_cached_thread, parse_post_url
from .fetcher.browser import thread_cache_dir
from .ffmpeg import find_ffmpeg
from .models import Segment, ThreadData
from .render import render_cards
from .selection import select_segments
from .translate import GeminiTranslator
from .tts import TTSManager

log = logging.getLogger(__name__)

STAGES = ["fetch", "select", "translate", "ai", "tts", "render", "compose"]
Progress = Callable[[str, float, str], None]  # (stage, fraction 0..1, message)


class JobCancelled(RuntimeError):
    pass


@dataclass
class JobResult:
    url: str
    work_dir: Path
    thread: ThreadData | None = None
    video: Path | None = None
    segments: list[Segment] | None = None
    stopped_for_review: bool = False


def job_dir(cfg: AppConfig, url: str) -> Path:
    return Path(cfg.work_dir) / parse_post_url(url).code


def save_segments(segments: list[Segment], path: Path) -> None:
    path.write_text(json.dumps([s.model_dump(mode="json") for s in segments], indent=2, ensure_ascii=False),
                    encoding="utf-8")


def load_segments(path: Path) -> list[Segment]:
    return [Segment.model_validate(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def output_path(cfg: AppConfig, segments: list[Segment], code: str) -> Path:
    user = segments[0].author.username if segments else "threads"
    return Path(cfg.output_dir) / f"{user}_{code}_{cfg.video.aspect.replace(':', 'x')}.mp4"


def translate_segments(segments: list[Segment], cfg: AppConfig) -> list[Segment]:
    """Replace each segment's text with its translation; text_original keeps the source."""
    tr = GeminiTranslator(cfg.translate.api_key, cfg.translate.model, service=cfg.translate.service,
                          cache_path=Path(cfg.cache_dir) / "translations.json")
    texts = tr.translate([s.text_original for s in segments], cfg.language)
    return [s.model_copy(update={"text": t}) for s, t in zip(segments, texts)]


def run_job(url: str, cfg: AppConfig, *, until: str = STAGES[-1], from_cache: bool | str = False,
            reuse_segments: bool = False, review: Callable[[list[Segment]], list[Segment] | None] | None = None,
            progress: Progress | None = None, cancel: Callable[[], bool] | None = None) -> JobResult:
    """Run stages up to `until`.

    from_cache: True = never fetch; "auto" = reuse a previous fetch of this post if there is one.
    review: called with the selected segments before TTS when selection.review_before_tts
    is on; returns the edited list, or None to stop here (the CLI stops so the user can
    edit segments.json and rerun with reuse_segments).
    cancel: polled between steps; returning True raises JobCancelled.
    """

    def report(stage: str, fraction: float, message: str) -> None:
        if cancel and cancel():
            raise JobCancelled("Cancelled")
        if progress:
            progress(stage, fraction, message)

    out = job_dir(cfg, url)
    out.mkdir(parents=True, exist_ok=True)
    result = JobResult(url=url, work_dir=out)
    seg_path = out / "segments.json"

    if reuse_segments and seg_path.exists():
        result.segments = load_segments(seg_path)
        log.info("Using edited segments from %s", seg_path)
    else:
        cached = any((thread_cache_dir(cfg.cache_dir, parse_post_url(url).code) / "raw").glob("*.json"))
        if from_cache is True or (from_cache == "auto" and cached):
            report("fetch", 0, "Loading cached post")
            result.thread = load_cached_thread(url, cfg.cache_dir)
        else:
            report("fetch", 0, "Fetching post")
            result.thread = fetch_thread(url, cfg.fetch, cache_dir=cfg.cache_dir)
        if until == "fetch":
            return result
        result.segments = select_segments(result.thread, cfg.selection)
        save_segments(result.segments, seg_path)
        log.info("Selected %d segments → %s", len(result.segments), seg_path)
        if until == "select":
            return result
        if cfg.translate.enabled:
            report("translate", 0, "Translating")
            result.segments = translate_segments(result.segments, cfg)
            save_segments(result.segments, seg_path)
        if until == "translate":
            return result
        if cfg.ai.speech_text or cfg.ai.reactions:
            report("ai", 0, "AI: reading the thread")
            result.segments = enrich_segments(result.segments, cfg, out)
            save_segments(result.segments, seg_path)
        if until == "ai":
            return result
        if cfg.selection.review_before_tts:
            edited = review(result.segments) if review else None
            if edited is None:
                result.stopped_for_review = True
                return result
            result.segments = edited
            save_segments(edited, seg_path)

    report("tts", 0, "Generating speech")
    tts = TTSManager(cfg.tts, cfg.language, find_ffmpeg(cfg.ffmpeg_path))
    result.segments = tts.synthesize_segments(
        result.segments, out / "audio", progress=lambda i, n, msg: report("tts", i / max(n, 1), msg)
    )
    save_segments(result.segments, seg_path)
    if until == "tts":
        return result

    report("render", 0, "Rendering cards")
    result.segments = render_cards(
        result.segments, out / "cards", video=cfg.video, card=cfg.card, lang=cfg.language,
        progress=lambda i, n, msg: report("render", i / max(n, 1), msg),
    )
    save_segments(result.segments, seg_path)
    if until == "render":
        return result

    report("compose", 0, "Encoding video")
    result.video = compose(
        result.segments, cfg, output_path(cfg, result.segments, out.name), out / "compose",
        find_ffmpeg(cfg.ffmpeg_path), progress=lambda f: report("compose", f, f"Encoding {f:.0%}"), cancel=cancel,
    )
    log.info("Video written to %s", result.video)
    return result
