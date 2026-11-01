from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import runtime
from ..appdirs import data_dir, gui_config_path, gui_defaults
from ..log import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="threads2video-gui")
    parser.add_argument("-c", "--config", type=Path, default=None, help=f"settings file (default: {gui_config_path()})")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    runtime.prepare()
    setup_logging(args.verbose, data_dir() / "logs" / "threads2video.log")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("threads2video")
    window = MainWindow(args.config or gui_config_path(), gui_defaults())
    window.show()
    return app.exec()
