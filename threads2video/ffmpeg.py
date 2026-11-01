"""Locate and run FFmpeg/FFprobe (config path → bundled next to the exe → PATH)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# Windowed (GUI) builds on Windows would flash a console window for every ffmpeg call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegError(RuntimeError):
    pass


class FFmpegCancelled(FFmpegError):
    pass


@dataclass(frozen=True)
class FFmpeg:
    ffmpeg: str
    ffprobe: str

    def run(self, *args: str) -> None:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW)
        if proc.returncode != 0:
            raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[-1500:]}")

    def run_progress(self, args: list[str], *, total: float,
                     progress: Callable[[float], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> None:
        """Run ffmpeg reporting progress (0..1) from `-progress`; `cancel()` returning True stops it."""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostats", "-progress", "pipe:1", *args]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        stderr_lines: list[str] = []
        reader = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
        reader.start()
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel and cancel():
                proc.terminate()
                proc.wait()
                raise FFmpegCancelled("Cancelled")
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and progress and total > 0 and value.isdigit():
                progress(min(1.0, int(value) / 1e6 / total))
        proc.wait()
        reader.join(timeout=5)
        if proc.returncode != 0:
            raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {''.join(stderr_lines).strip()[-2000:]}")
        if progress:
            progress(1.0)

    def sample_rate(self, path: Path) -> int:
        proc = subprocess.run(
            [self.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, creationflags=NO_WINDOW,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise FFmpegError(f"ffprobe could not read {path}: {proc.stderr.strip()}")
        return int(proc.stdout.strip())

    def duration(self, path: Path) -> float:
        proc = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, creationflags=NO_WINDOW,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise FFmpegError(f"ffprobe could not read {path}: {proc.stderr.strip()}")
        return float(proc.stdout.strip())


def _candidates(configured: str | None) -> list[Path]:
    dirs: list[Path] = []
    if configured:
        p = Path(configured)
        dirs.append(p if p.is_dir() else p.parent)
    if getattr(sys, "frozen", False):  # PyInstaller
        dirs += [Path(getattr(sys, "_MEIPASS", "")), Path(sys.executable).parent]
    return dirs


def find_ffmpeg(configured: str | None = None) -> FFmpeg:
    exe = ".exe" if sys.platform == "win32" else ""
    for d in _candidates(configured):
        ff, fp = d / f"ffmpeg{exe}", d / f"ffprobe{exe}"
        if ff.is_file() and fp.is_file():
            return FFmpeg(str(ff), str(fp))
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ff and fp:
        return FFmpeg(ff, fp)
    raise FFmpegError(
        "FFmpeg not found. Install it (Windows: `winget install Gyan.FFmpeg`, macOS: `brew install ffmpeg`) "
        "or set ffmpeg_path in the config to the folder containing ffmpeg and ffprobe."
    )
