"""Background work for the GUI: everything slow runs off the UI thread.

Playwright's sync API, FFmpeg and network calls all block, so each job runs in
a QThread; results, progress and log lines come back through Qt signals
(queued onto the UI thread).
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal

from ..config import AppConfig
from ..ffmpeg import FFmpegCancelled
from ..models import Segment
from ..pipeline import JobCancelled, JobResult, overall_progress, run_job

log = logging.getLogger(__name__)

class LogBridge(QObject, logging.Handler):
    """logging.Handler that forwards records to the UI thread as a signal."""

    message = Signal(str, int)

    def __init__(self) -> None:
        QObject.__init__(self)
        logging.Handler.__init__(self, level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        try:
            self.message.emit(self.format(record), record.levelno)
        except RuntimeError:  # widget already gone during shutdown
            pass


class Task(QObject):
    """Run fn(*args) on a worker thread; emits done(result) or failed(message)."""

    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn: Callable[..., Any], *args: Any, with_progress: bool = False) -> None:
        super().__init__()
        self.fn, self.args, self.with_progress = fn, args, with_progress
        self.thread = QThread()
        # The Task stays on the UI thread and only _run executes on the worker thread, so the
        # signals it emits there are queued to the UI thread for plain-function callbacks.
        # (Moved to the worker, Qt would call them on the worker: a dialog opened there aborts
        # the app on macOS.)
        self.thread.started.connect(self._run, Qt.DirectConnection)

    def _run(self) -> None:
        try:
            kwargs = {"progress": lambda a, b: self.progress.emit(int(a), int(b))} if self.with_progress else {}
            self.done.emit(self.fn(*self.args, **kwargs))
        except Exception as e:
            log.debug("Task failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))
        finally:
            self.thread.quit()

    def start(self) -> None:
        self.thread.start()


class JobWorker(QObject):
    """Runs run_job() for a list of URLs, one after another."""

    progress = Signal(str, float, str)  # stage, overall 0..1, message
    job_started = Signal(int, str)  # index, url
    job_done = Signal(int, object)  # index, JobResult
    job_failed = Signal(int, str)  # index, message
    review_requested = Signal(list)  # list[Segment]; answer with provide_review()
    finished = Signal()

    def __init__(self, urls: list[str], cfg: AppConfig, *, reuse_fetch: bool) -> None:
        super().__init__()
        self.urls, self.cfg, self.reuse_fetch = urls, cfg, reuse_fetch
        self._cancel = threading.Event()
        self._review_done = threading.Event()
        self._review_result: list[Segment] | None = None
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.thread.started.connect(self._run)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        self._review_done.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def provide_review(self, segments: list[Segment] | None) -> None:
        """Called from the UI thread with the edited segments (None = skip this post)."""
        self._review_result = segments
        self._review_done.set()

    def _review(self, segments: list[Segment]) -> list[Segment] | None:
        self._review_done.clear()
        self.review_requested.emit(segments)
        self._review_done.wait()
        return None if self.cancelled else self._review_result

    def _run(self) -> None:
        try:
            for i, url in enumerate(self.urls):
                if self.cancelled:
                    break
                self.job_started.emit(i, url)
                try:
                    result: JobResult = run_job(
                        url, self.cfg, from_cache="auto" if self.reuse_fetch else False, review=self._review,
                        progress=lambda st, f, msg: self.progress.emit(st, overall_progress(st, f), msg),
                        cancel=lambda: self.cancelled,
                    )
                    if result.stopped_for_review:
                        self.job_failed.emit(i, "Skipped at review")
                    else:
                        self.job_done.emit(i, result)
                except (JobCancelled, FFmpegCancelled):
                    self.job_failed.emit(i, "Cancelled")
                except Exception as e:
                    log.error("%s failed: %s", url, e)
                    log.debug("%s", traceback.format_exc())
                    self.job_failed.emit(i, str(e))
        finally:
            self.finished.emit()
            self.thread.quit()
