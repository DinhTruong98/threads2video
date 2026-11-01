# PyInstaller spec: one folder with two executables sharing the same libraries:
#   threads2video-gui(.exe)  windowed app
#   threads2video(.exe)      console app (CLI)
# Build with:  python packaging/build.py   (see that file for options)
# ruff: noqa

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent

datas = (
    collect_data_files("threads2video")  # card template
    + collect_data_files("piper")  # espeak-ng-data
    + collect_data_files("playwright", include_py_files=False)  # Node.js driver (no PyInstaller hook exists)
    + collect_data_files("edge_tts")
    + collect_data_files("certifi")
)
binaries = collect_dynamic_libs("piper")  # espeakbridge native module
hiddenimports = (
    collect_submodules("threads2video")
    + ["piper.espeakbridge", "piper.phonemize_espeak", "edge_tts", "httpx", "numpy"]
)
excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "flask", "piper.train", "piper.http_server",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtQuick3D",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner", "PySide6.QtBluetooth", "PySide6.QtSql",
]


def analysis(script):
    return Analysis(
        [str(ROOT / "packaging" / script)],
        pathex=[str(ROOT)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=False,
    )


gui = analysis("gui_main.py")
cli = analysis("cli_main.py")

icon = ROOT / "packaging" / ("icon.ico" if sys.platform == "win32" else "icon.icns")
icon = str(icon) if icon.exists() else None

gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, [],
    exclude_binaries=True, name="threads2video-gui", console=False, icon=icon,
    upx=False, codesign_identity=None, entitlements_file=None,
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, [],
    exclude_binaries=True, name="threads2video", console=True, icon=icon,
    upx=False, codesign_identity=None, entitlements_file=None,
)
COLLECT(
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    name="threads2video", upx=False,
)
