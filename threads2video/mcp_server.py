"""MCP server: lets an AI agent make videos with the app's saved settings.

    threads2video --mcp                  stdio (the agent starts the program itself)
    threads2video --mcp-http [PORT]      streamable HTTP on 127.0.0.1:PORT/mcp (default 18765)

Every job starts from the settings saved by the desktop app (re-read for each
job, so changes made in the app apply at once); a tool call can override any of
them for that job only. Jobs run one at a time on a worker thread, because they
share the logged-in browser profile, FFmpeg and the TTS models.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from .appdirs import gui_config_path, gui_defaults
from .config import AppConfig, _merge, load_config
from .fetcher import parse_post_url
from .pipeline import JobCancelled, overall_progress, run_job

log = logging.getLogger(__name__)

DEFAULT_HTTP_PORT = 18765
INSTRUCTIONS = """Makes narrated vertical (or horizontal) short videos from Threads posts: the post and its top
comments are read aloud by TTS, each shown as a Threads-style card over a background video with music.
Call create_video with one or more threads.com post URLs. Settings default to what the user saved in the
threads2video app (see get_settings); pass overrides only when the user asks for something different.
A video takes about 1-2 minutes. Use wait=false for many posts and poll get_job."""

# Friendly tool arguments → config paths.
OVERRIDES = {
    "aspect": "video.aspect", "top_comments": "selection.top_comments", "op_replies": "selection.op_replies",
    "tts_engine": "tts.provider", "voice": "tts.voice", "op_voice": "tts.op_voice", "speed": "tts.speed",
    "background": "video.background", "music": "audio.music", "subtitles": "video.subtitles",
    "language": "language", "translate": "translate.enabled", "ai_speech_text": "ai.speech_text",
    "ai_reactions": "ai.reactions",
}


def load_settings(config_path: Path | None = None) -> AppConfig:
    """The desktop app's saved settings, with per-user defaults for anything unset."""
    return load_config(config_path or gui_config_path(), gui_defaults())


def apply_overrides(cfg: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """Return cfg with dotted-path overrides ({"video.aspect": "16:9"}) applied and validated."""
    nested: dict = {}
    for path, value in overrides.items():
        *parents, leaf = path.split(".")
        node = nested
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    unknown = [k for k in overrides if k.split(".")[0] not in AppConfig.model_fields]
    if unknown:
        raise ValueError(f"Unknown setting(s): {unknown}; see get_settings")
    return AppConfig.model_validate(_merge(cfg.model_dump(mode="json"), nested))


def public_settings(cfg: AppConfig) -> dict[str, Any]:
    data = cfg.model_dump(mode="json")
    data["translate"]["api_key"] = "(set)" if cfg.translate.api_key else "(not set)"
    return data


@dataclass
class Job:
    id: str
    urls: list[str]
    cfg: AppConfig
    use_cached_fetch: bool = False
    status: str = "queued"  # queued, running, done, failed, cancelled
    current: int = 0
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    videos: list[dict] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.id, "status": self.status, "urls": self.urls,
            "progress": round(self.progress, 3), "current_post": self.current + 1 if self.status == "running" else None,
            "stage": self.stage or None, "message": self.message or None,
            "videos": self.videos, "errors": self.errors,
            "output_dir": str(self.cfg.output_dir),
        }


class JobQueue:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="threads2video-job")

    def submit(self, urls: list[str], cfg: AppConfig, *, use_cached_fetch: bool = False) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], urls=urls, cfg=cfg, use_cached_fetch=use_cached_fetch)
        self.jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        if job.cancel.is_set():
            job.status, job.finished = "cancelled", time.time()
            return
        job.status = "running"
        n = len(job.urls)
        for i, url in enumerate(job.urls):
            job.current = i

            def progress(stage: str, fraction: float, message: str, i: int = i) -> None:
                job.stage, job.message = stage, message
                job.progress = (i + overall_progress(stage, fraction)) / n

            try:
                log.info("MCP job %s: %s", job.id, url)
                res = run_job(url, job.cfg, from_cache="auto" if job.use_cached_fetch else False,
                              progress=progress, cancel=job.cancel.is_set)
                job.videos.append(_video_info(url, res, job.cfg))
            except JobCancelled:
                job.status = "cancelled"
                break
            except Exception as e:  # one bad post doesn't stop the others
                if job.cancel.is_set():
                    job.status = "cancelled"
                    break
                log.error("MCP job %s: %s failed: %s", job.id, url, e)
                job.errors[url] = str(e)
        if job.status != "cancelled":
            job.status = "done" if job.videos else "failed"
        job.progress = 1.0 if job.status == "done" else job.progress
        job.stage, job.finished = "", time.time()


def _video_info(url: str, res, cfg: AppConfig) -> dict[str, Any]:
    segs = res.segments or []
    info = {"url": url, "path": str(res.video), "segments": len(segs),
            "voices": sorted({s.voice for s in segs if s.voice}),
            "reactions": [s.reaction for s in segs if s.reaction]}
    try:
        from .ffmpeg import find_ffmpeg

        info["duration_s"] = round(find_ffmpeg(cfg.ffmpeg_path).duration(res.video), 1)
    except Exception:
        pass
    return info


def build_server(queue: JobQueue | None = None, config_path: Path | None = None) -> MCPServer:
    queue = queue or JobQueue()
    server = MCPServer("threads2video", instructions=INSTRUCTIONS)

    @server.tool(structured_output=True)
    async def create_video(ctx: Context, urls: list[str], wait: bool = True, aspect: str | None = None,
                           top_comments: int | None = None, op_replies: int | None = None,
                           tts_engine: str | None = None, voice: str | None = None, op_voice: str | None = None,
                           speed: float | None = None, background: str | None = None, music: str | None = None,
                           subtitles: bool | None = None, language: str | None = None, translate: bool | None = None,
                           ai_speech_text: bool | None = None, ai_reactions: bool | None = None,
                           settings: dict[str, Any] | None = None, use_cached_fetch: bool = False) -> dict[str, Any]:
        """Make one video per Threads post URL, using the app's saved settings.

        urls: threads.com (or threads.net) post URLs, e.g. https://www.threads.com/@user/post/ABC123
        wait: true = return when finished (progress is reported); false = return a job_id at once, poll get_job.
        Optional overrides for this job only (omit to keep the saved setting):
          aspect "9:16" | "16:9"; top_comments, op_replies: how many comments / OP replies to read;
          tts_engine "edge" | "vieneu" | "piper"; voice, op_voice: voice ids of that engine; speed 0.5-2.0;
          background, music: a file or a folder (random pick); subtitles; language (reading language, e.g. "vi");
          translate, ai_speech_text (write out abbreviations), ai_reactions (laugh/gasp sounds).
        settings: any other setting by dotted path, e.g. {"audio.music_volume": 15, "card.theme": "light"}
          (see get_settings for all of them).
        use_cached_fetch: reuse an earlier download of the post instead of fetching it again.
        Returns the job: status, videos (path, duration_s, segments, voices, reactions) and errors per URL.
        """
        if isinstance(urls, str):
            urls = [urls]
        urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))
        if not urls:
            raise ToolError("Give at least one Threads post URL")
        bad = []
        for u in urls:
            try:
                parse_post_url(u)
            except ValueError:
                bad.append(u)
        if bad:
            raise ToolError(f"Not a Threads post URL: {bad}")
        given = {"aspect": aspect, "top_comments": top_comments, "op_replies": op_replies, "tts_engine": tts_engine,
                 "voice": voice, "op_voice": op_voice, "speed": speed, "background": background, "music": music,
                 "subtitles": subtitles, "language": language, "translate": translate,
                 "ai_speech_text": ai_speech_text, "ai_reactions": ai_reactions}
        overrides = {OVERRIDES[k]: v for k, v in given.items() if v is not None}
        overrides.update(settings or {})
        # Nobody is there to click through the review dialog.
        overrides["selection.review_before_tts"] = False
        try:
            cfg = apply_overrides(load_settings(config_path), overrides)
        except (ValueError, ValidationError) as e:  # pydantic's ValidationError is a ValueError
            raise ToolError(f"Invalid setting: {e}") from None
        job = queue.submit(urls, cfg, use_cached_fetch=use_cached_fetch)
        if not wait:
            return job.summary()
        try:
            while job.status in ("queued", "running"):
                await ctx.report_progress(job.progress, 1.0, f"{job.stage or job.status}: {job.message}".strip(": "))
                await anyio.sleep(1)
        except anyio.get_cancelled_exc_class():
            job.cancel.set()
            raise
        return job.summary()

    @server.tool(structured_output=True)
    def get_job(job_id: str) -> dict[str, Any]:
        """Status of a job from create_video: queued/running/done/failed/cancelled, progress, videos, errors."""
        if job_id not in queue.jobs:
            raise ToolError(f"No job {job_id!r}")
        return queue.jobs[job_id].summary()

    @server.tool(structured_output=True)
    def list_jobs() -> list[dict[str, Any]]:
        """All jobs since the server started, newest first."""
        return [j.summary() for j in sorted(queue.jobs.values(), key=lambda j: j.created, reverse=True)]

    @server.tool(structured_output=True)
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Stop a queued or running job (the current post stops at its next step)."""
        if job_id not in queue.jobs:
            raise ToolError(f"No job {job_id!r}")
        job = queue.jobs[job_id]
        job.cancel.set()
        return job.summary()

    @server.tool(structured_output=True)
    def get_settings() -> dict[str, Any]:
        """The default settings jobs start from: the ones saved in the threads2video app (API key hidden)."""
        return public_settings(load_settings(config_path))

    @server.tool(structured_output=True)
    def list_videos(limit: int = 20) -> list[dict[str, Any]]:
        """Most recent videos in the output folder: path, size and modification time."""
        out = Path(load_settings(config_path).output_dir)
        files = sorted(out.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [{"path": str(f), "size_mb": round(f.stat().st_size / 1e6, 1),
                 "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))}
                for f in files[:limit] if not f.name.endswith(".part.mp4")]

    return server


def cli() -> None:
    """`threads2video-mcp [--http [PORT]]`: same as `threads2video --mcp` / `--mcp-http`."""
    import sys

    from .cli import main as cli_main

    args = sys.argv[1:]
    if args[:1] == ["--http"]:
        args = ["--mcp-http", *args[1:]]
    raise SystemExit(cli_main(args if "--mcp-http" in args else ["--mcp", *args]))


def main(http_port: int | None = None, config_path: Path | None = None) -> None:
    server = build_server(config_path=config_path)
    if http_port:
        log.info("MCP server on http://127.0.0.1:%d/mcp", http_port)
        server.run("streamable-http", host="127.0.0.1", port=http_port)
    else:
        server.run("stdio")
