"""Build the distributable folder + zip with PyInstaller.

    python packaging/build.py [--ffmpeg-dir DIR] [--bundle-browser] [--no-zip]

Output: dist/threads2video/ containing threads2video-gui(.exe) and threads2video(.exe),
and dist/threads2video-<version>-<platform>.zip.

FFmpeg: copied next to the executables from --ffmpeg-dir, or (Windows only) from
PATH. Use a static build (e.g. https://www.gyan.dev/ffmpeg/builds/ "essentials");
Homebrew/Linux distro ffmpeg binaries depend on system libraries and are not
copied, the app then uses the ffmpeg on PATH.

Chromium: by default the app downloads it on first run (~170 MB). With
--bundle-browser it is installed into dist/threads2video/ms-playwright and
works offline (adds ~300 MB unpacked).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_DIR = DIST / "threads2video"
EXE = ".exe" if sys.platform == "win32" else ""


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def copy_ffmpeg(ffmpeg_dir: str | None) -> None:
    sources: dict[str, Path] = {}
    for name in ("ffmpeg", "ffprobe"):
        if ffmpeg_dir:
            src = Path(ffmpeg_dir) / f"{name}{EXE}"
        elif sys.platform == "win32" and shutil.which(name):
            src = Path(shutil.which(name))
        else:
            src = None
        if src is None or not src.is_file():
            print(f"! {name}{EXE} not bundled; the app will look for it on PATH", flush=True)
            return
        sources[name] = src
    for name, src in sources.items():
        shutil.copy2(src, APP_DIR / src.name)
        print(f"  bundled {src}", flush=True)


def bundle_browser() -> None:
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(APP_DIR / "ms-playwright")}
    run([sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"], env=env)


def version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0"


def make_zip() -> Path:
    plat = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    out = DIST / f"threads2video-{version()}-{plat}-{platform.machine().lower()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(APP_DIR.rglob("*")):
            if f.is_file() or f.is_symlink():
                zf.write(f, f.relative_to(DIST))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ffmpeg-dir", help="folder containing static ffmpeg and ffprobe to bundle")
    ap.add_argument("--bundle-browser", action="store_true", help="bundle Chromium for offline use")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
         "--workpath", str(ROOT / "build"), str(ROOT / "packaging" / "threads2video.spec")])
    copy_ffmpeg(args.ffmpeg_dir)
    if args.bundle_browser:
        bundle_browser()
    shutil.copy2(ROOT / "LICENSE", APP_DIR / "LICENSE.txt")
    shutil.copy2(ROOT / "config.example.yaml", APP_DIR / "config.example.yaml")
    size = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file()) / 1e6
    print(f"Built {APP_DIR} ({size:.0f} MB)", flush=True)
    if not args.no_zip:
        print(f"Zipped {make_zip()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
