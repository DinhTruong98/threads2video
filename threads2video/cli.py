"""Command line entrypoint: `python -m threads2video URL [URL ...] --config config.yaml`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import runtime
from .config import load_config
from .fetcher import login_interactive
from .log import setup_logging
from .pipeline import STAGES, run_job

log = logging.getLogger("threads2video")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="threads2video", description=__doc__)
    p.add_argument("urls", nargs="*", help="Threads post URL(s)")
    p.add_argument("-f", "--urls-file", type=Path, help="text file with one URL per line (# comments allowed)")
    p.add_argument("-c", "--config", type=Path,
                   help="YAML/JSON config (default: config.yaml; with --mcp: the desktop app's saved settings)")
    p.add_argument("--until", choices=STAGES, default=STAGES[-1], help="stop after this stage")
    p.add_argument("--from-cache", action="store_true", help="re-parse cached raw JSON instead of fetching")
    p.add_argument("--reuse-segments", action="store_true",
                   help="use work/<code>/segments.json as edited instead of re-selecting")
    p.add_argument("--login", action="store_true", help="open a browser to log in to Threads (needs fetch.profile_dir)")
    p.add_argument("--download-piper-voice", metavar="KEY", nargs="?", const="",
                   help="download a Piper voice (no KEY: list voices for the configured language)")
    p.add_argument("--download-vieneu", action="store_true",
                   help="download the VieNeu-TTS Vietnamese model (~570 MB) into tts.vieneu_models_dir")
    p.add_argument("--mcp", action="store_true", help="run as an MCP server on stdio, for AI agents")
    p.add_argument("--mcp-http", metavar="PORT", type=int, nargs="?", const=18765,
                   help="run as an MCP server on http://127.0.0.1:PORT/mcp (default port 18765)")
    p.add_argument("--install-browser", action="store_true", help="download the Chromium build Playwright needs")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _piper_voices(cfg, key: str) -> int:
    from .tts.piper import download_voice, downloadable_voices, fetch_catalog, remote_missing_phonemes

    catalog = fetch_catalog()
    if not key:
        for v in downloadable_voices(cfg.language, catalog):
            size = sum(m["size_bytes"] for p, m in v["files"].items() if p.endswith(".onnx")) / 1e6
            missing = remote_missing_phonemes(v)
            note = f"  INCOMPATIBLE with this piper-tts (missing {', '.join(missing)})" if missing else ""
            print(f"{v['key']:32} quality={v['quality']:7} speakers={v['num_speakers']:<3} {size:.0f} MB{note}")
        return 0
    if key in catalog and (missing := remote_missing_phonemes(catalog[key])):
        log.warning("%s lacks phonemes %s and will be skipped at synthesis time", key, missing)
    path = download_voice(key, cfg.tts.piper_models_dir, catalog=catalog)
    print(f"Saved {path}")
    return 0


def _print_progress(stage: str, fraction: float, message: str) -> None:
    if stage == "compose":
        end = "\n" if fraction >= 1 else ""
        print(f"\r  {message}", end=end, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime.prepare()
    if args.mcp or args.mcp_http:
        from . import mcp_server

        cfg = mcp_server.load_settings(args.config)
        setup_logging(args.verbose, cfg.cache_dir / "logs" / "mcp.log")  # console logs go to stderr
        mcp_server.main(http_port=args.mcp_http, config_path=args.config)
        return 0
    cfg = load_config(args.config or Path("config.yaml"))
    setup_logging(args.verbose, cfg.cache_dir / "logs" / "threads2video.log")

    if args.install_browser:
        from .browser_env import install_browser

        install_browser(print)
        return 0
    if args.login:
        return 0 if login_interactive(cfg.fetch) else 1
    if args.download_piper_voice is not None:
        return _piper_voices(cfg, args.download_piper_voice)
    if args.download_vieneu:
        from .tts.vieneu import download_model

        download_model(cfg.tts.vieneu_models_dir,
                       lambda done, total: print(f"\r  {done / 1e6:.0f} / {total / 1e6:.0f} MB", end="", flush=True))
        print(f"\nSaved to {cfg.tts.vieneu_models_dir}")
        return 0
    urls = list(args.urls)
    if args.urls_file:
        lines = args.urls_file.read_text(encoding="utf-8").splitlines()
        urls += [ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()]
    urls = list(dict.fromkeys(urls))
    if not urls:
        build_parser().print_usage()
        return 2

    failed = 0
    for n, url in enumerate(urls, 1):
        if len(urls) > 1:
            print(f"[{n}/{len(urls)}] {url}")
        try:
            res = run_job(url, cfg, until=args.until, from_cache=args.from_cache, reuse_segments=args.reuse_segments,
                          progress=_print_progress if sys.stderr.isatty() else None)
            if res.thread:
                t = res.thread
                print(f"@{t.post.author.username}: {t.post.text[:80]!r}")
                print(f"  {t.post.like_count} likes, {t.post.reply_count} replies; "
                      f"loaded {len(t.replies)} replies ({len(t.op_replies)} by OP)")
            if res.stopped_for_review:
                print(f"  Review/edit {res.work_dir / 'segments.json'}, then rerun with --reuse-segments")
            for i, s in enumerate(res.segments or []):
                timing = f"{s.duration:6.2f}s {s.voice}" if s.duration is not None else ""
                print(f"  [{i:02d}] {s.kind:9} @{s.author.username:<20} {s.like_count:>6}♥ {timing}  {s.text[:50]!r}")
            if res.segments and res.segments[-1].duration is not None:
                print(f"  total narration: {sum(s.duration for s in res.segments):.2f}s")
            if res.video:
                print(f"  video: {res.video}")
        except Exception as e:  # keep going through a batch
            if "Executable doesn't exist" in str(e):
                e = RuntimeError("Chromium is not installed; run: threads2video --install-browser")
            failed += 1
            log.error("%s: %s", url, e, exc_info=args.verbose)
    if len(urls) > 1:
        print(f"Batch finished: {len(urls) - failed} of {len(urls)} videos made")
    return 1 if failed else 0
