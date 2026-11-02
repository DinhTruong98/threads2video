"""Main window: four settings sections (in the requested order), generate/cancel, progress and log."""

from __future__ import annotations

import logging
import random
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget, QHeaderView)

from ..config import AppConfig, load_config, save_config
from ..fetcher.urls import parse_post_url
from ..models import Segment
from ..pipeline import JobResult
from ..translate import SUGGESTED_MODELS
from .dialogs import PiperDownloadDialog, ReviewDialog
from .workers import JobWorker, LogBridge, Task

log = logging.getLogger(__name__)

LANGUAGES = [("vi", "Tiếng Việt"), ("en", "English"), ("ja", "日本語"), ("ko", "한국어"), ("zh", "中文"),
             ("th", "ไทย"), ("id", "Bahasa Indonesia"), ("es", "Español")]
PROVIDERS = [("edge", "Edge TTS (online, free)"), ("vieneu", "VieNeu-TTS (offline, Vietnamese)"),
             ("piper", "Piper (offline)"), ("capcut", "CapCut (unofficial, unstable)")]
IDENTITY = [("show", "Show names"), ("blur", "Blur names & avatars"), ("black", "Black out names & avatars")]
PREVIEW_TEXT = {"vi": "Xin chào! Đây là giọng đọc thử cho video Threads của bạn.",
                "en": "Hi! This is a preview of the voice for your Threads video."}


def _path_row(edit: QLineEdit, *, file_filter: str, allow_folder: bool = True) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)
    file_btn = QPushButton("File…")
    file_btn.clicked.connect(lambda: (p := QFileDialog.getOpenFileName(w, "Choose file", edit.text(), file_filter)[0])
                             and edit.setText(p))
    lay.addWidget(file_btn)
    if allow_folder:
        dir_btn = QPushButton("Folder…")
        dir_btn.setToolTip("Pick a random file from this folder for each video")
        dir_btn.clicked.connect(lambda: (p := QFileDialog.getExistingDirectory(w, "Choose folder", edit.text()))
                                and edit.setText(p))
        lay.addWidget(dir_btn)
    return w


def _volume(value: int) -> tuple[QWidget, QSlider]:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    slider = QSlider(Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(value)
    label = QLabel(f"{value}%")
    label.setMinimumWidth(40)
    slider.valueChanged.connect(lambda v: label.setText(f"{v}%"))
    lay.addWidget(slider, 1)
    lay.addWidget(label)
    return w, slider


def _combo(items: list[tuple[str, str]], *, editable: bool = False) -> QComboBox:
    c = QComboBox()
    c.setEditable(editable)
    for value, label in items:
        c.addItem(label, value)
    return c


def _set_combo(c: QComboBox, value: str | None) -> None:
    i = c.findData(value)
    if i >= 0:
        c.setCurrentIndex(i)
    elif c.isEditable() and value:
        c.setEditText(value)


def _combo_value(c: QComboBox) -> str:
    if c.isEditable():
        i = c.findText(c.currentText())
        return c.itemData(i) if i >= 0 and c.itemData(i) else c.currentText().strip()
    return c.currentData()


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path, defaults: AppConfig) -> None:
        super().__init__()
        self.config_path = config_path
        self.cfg = load_config(config_path, defaults)
        self.worker: JobWorker | None = None
        self.tasks: list[Task] = []
        self.last_video: Path | None = None
        self.job_urls: list[str] = []
        self.job_videos: dict[int, Path] = {}
        self.job_failed: dict[int, str] = {}
        self.current_job = 0
        self.login_open = False
        # Created on first preview: Qt's audio backend is slow to start and can crash without a sound device.
        self.player: QMediaPlayer | None = None
        self.audio_out: QAudioOutput | None = None

        self.setWindowTitle("Threads → Short Video")
        self.resize(980, 860)
        self._build()
        self._load(self.cfg)

        self.log_bridge = LogBridge()
        self.log_bridge.message.connect(self._append_log)
        logging.getLogger().addHandler(self.log_bridge)
        self._refresh_voices()
        self._check_browser()

    # ---------------------------------------------------------------- layout

    def _build(self) -> None:
        # (1) Source
        self.url = QPlainTextEdit()
        self.url.setPlaceholderText("https://www.threads.com/@user/post/…\n"
                                    "Paste one or more post URLs, one per line, to make a video for each")
        self.url.setFixedHeight(62)
        self.url.setTabChangesFocus(True)
        self.reuse_fetch = QCheckBox("Reuse already-fetched post data (re-render without fetching again)")
        self.login_btn = QPushButton("Log in to Threads…")
        self.login_btn.setToolTip("Opens a browser window; log in once to load more replies (incl. OP replies)")
        self.login_btn.clicked.connect(self._login)
        self.background = QLineEdit()
        self.background.setPlaceholderText("Video file or folder (empty: animated gradient)")
        self.music = QLineEdit()
        self.music.setPlaceholderText("Audio file or folder")
        url_row = QWidget()
        ur = QHBoxLayout(url_row)
        ur.setContentsMargins(0, 0, 0, 0)
        ur.addWidget(self.url, 1)
        ur.addWidget(self.login_btn, 0, Qt.AlignTop)
        source = QFormLayout()
        source.addRow("Threads URL(s)", url_row)
        source.addRow("", self.reuse_fetch)
        source.addRow("Background video", _path_row(self.background, file_filter="Videos (*.mp4 *.mov *.mkv *.webm)"))
        source.addRow("Background music", _path_row(self.music, file_filter="Audio (*.mp3 *.wav *.m4a *.ogg *.flac)"))

        # (2) Reading
        self.top_comments = QSpinBox()
        self.top_comments.setRange(0, 50)
        music_vol_w, self.music_volume = _volume(20)
        self.provider = _combo(PROVIDERS)
        self.provider.currentIndexChanged.connect(self._refresh_voices)
        self.voice = QComboBox()
        self.op_voice = QComboBox()
        reading = QFormLayout()
        reading.addRow("Top comments (by likes)", self.top_comments)
        reading.addRow("Music volume", music_vol_w)
        reading.addRow("TTS engine", self.provider)
        reading.addRow("Default voice", self.voice)
        reading.addRow("OP voice", self.op_voice)

        # (3) Video & effects
        self.op_replies = QSpinBox()
        self.op_replies.setRange(0, 20)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 2.0)
        self.speed.setSingleStep(0.05)
        self.speed.setDecimals(2)
        self.random_voice = QCheckBox("Different random voice for each comment author")
        self.aspect = _combo([("9:16", "9:16 vertical (1080×1920)"), ("16:9", "16:9 horizontal (1920×1080)")])
        self.theme = _combo([("dark", "Dark"), ("light", "Light")])
        self.position = _combo([("center", "Center"), ("top", "Top"), ("bottom", "Bottom")])
        self.animation = _combo([("fade", "Fade in"), ("slide", "Slide in"), ("none", "None")])
        self.subtitles = QCheckBox("Burn in subtitles")
        video = QFormLayout()
        video.addRow("OP replies", self.op_replies)
        video.addRow("Reading speed", self.speed)
        video.addRow("", self.random_voice)
        video.addRow("Aspect ratio", self.aspect)
        video.addRow("Card theme", self.theme)
        video.addRow("Card position", self.position)
        video.addRow("Card animation", self.animation)
        video.addRow("", self.subtitles)

        # (4) Language & AI
        self.language = _combo(LANGUAGES, editable=True)
        self.language.currentIndexChanged.connect(self._refresh_voices)
        self.review = QCheckBox("Review / edit texts before reading")
        self.identity = _combo(IDENTITY)
        self.sfx_enabled = QCheckBox("Sound effect when each card appears")
        self.sfx = QLineEdit()
        self.sfx.setPlaceholderText("Sound file or folder (empty: built-in pop)")
        sfx_vol_w, self.sfx_volume = _volume(40)
        preview_sfx = QPushButton("Preview SFX")
        preview_sfx.clicked.connect(self._preview_sfx)
        sfx_row = QWidget()
        sr = QHBoxLayout(sfx_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(sfx_vol_w, 1)
        sr.addWidget(preview_sfx)
        self.preview_text = QLineEdit()
        self.preview_btn = QPushButton("Preview voice")
        self.preview_btn.clicked.connect(self._preview_voice)
        piper_btn = QPushButton("Download Piper voice…")
        piper_btn.clicked.connect(self._download_piper)
        self.vieneu_btn = QPushButton("Download VieNeu model…")
        self.vieneu_btn.setToolTip("Offline Vietnamese voices (~570 MB); used when Edge TTS fails")
        self.vieneu_btn.clicked.connect(self._download_vieneu)
        prev_row = QWidget()
        pr = QHBoxLayout(prev_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(self.preview_text, 1)
        pr.addWidget(self.preview_btn)
        pr.addWidget(piper_btn)
        pr.addWidget(self.vieneu_btn)
        self.music_enabled = QCheckBox("Background music")
        self.translate = QCheckBox("Translate with Gemini, then read")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("Gemini API key (stored only in your local config file)")
        self.model = _combo([(m, m) for m in SUGGESTED_MODELS], editable=True)
        check_btn = QPushButton("Check key / list models")
        check_btn.clicked.connect(self._check_gemini)
        model_row = QWidget()
        mr = QHBoxLayout(model_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.addWidget(self.model, 1)
        mr.addWidget(check_btn)
        lang = QFormLayout()
        lang.addRow("Reading language", self.language)
        lang.addRow("", self.review)
        lang.addRow("Names", self.identity)
        lang.addRow("", self.sfx_enabled)
        lang.addRow("SFX file/folder", _path_row(self.sfx, file_filter="Audio (*.wav *.mp3 *.ogg *.m4a)"))
        lang.addRow("SFX volume", sfx_row)
        lang.addRow("Voice preview", prev_row)
        lang.addRow("", self.music_enabled)
        lang.addRow("", self.translate)
        lang.addRow("Gemini API key", self.api_key)
        lang.addRow("Gemini model", model_row)

        settings = QWidget()
        col = QVBoxLayout(settings)
        for title, form in [("1 · Source", source), ("2 · Reading", reading),
                            ("3 · Video && effects", video), ("4 · Language && AI", lang)]:
            box = QGroupBox(title)
            box.setLayout(form)
            col.addWidget(box)
        col.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(settings)

        # Run controls, progress, log.
        self.generate_btn = QPushButton("Generate video")
        self.generate_btn.setDefault(True)
        self.generate_btn.setMinimumHeight(36)
        self.generate_btn.clicked.connect(self._generate)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        self.open_btn = QPushButton("Open output folder")
        self.open_btn.clicked.connect(self._open_output)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.status = QLabel("Ready")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QFont("Menlo, Consolas, monospace", 10))
        buttons = QHBoxLayout()
        buttons.addWidget(self.generate_btn, 2)
        buttons.addWidget(self.cancel_btn, 1)
        buttons.addWidget(self.open_btn, 1)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.addLayout(buttons)
        self.queue = QTableWidget(0, 3)
        self.queue.setHorizontalHeaderLabels(["Post", "Status", "Video"])
        self.queue.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.queue.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.queue.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.queue.verticalHeader().setVisible(False)
        self.queue.setEditTriggers(QTableWidget.NoEditTriggers)
        self.queue.setSelectionBehavior(QTableWidget.SelectRows)
        self.queue.setToolTip("Double-click a finished row to play its video")
        self.queue.cellDoubleClicked.connect(self._open_row_video)
        self.retry_btn = QPushButton("Retry failed")
        self.retry_btn.setEnabled(False)
        self.retry_btn.clicked.connect(self._retry_failed)
        buttons.addWidget(self.retry_btn, 1)
        bl.addWidget(self.progress)
        bl.addWidget(self.status)
        lower = QSplitter(Qt.Horizontal)
        lower.addWidget(self.queue)
        lower.addWidget(self.log_view)
        lower.setSizes([420, 560])
        bl.addWidget(lower, 1)

        split = QSplitter(Qt.Vertical)
        split.addWidget(scroll)
        split.addWidget(bottom)
        split.setSizes([560, 300])
        self.setCentralWidget(split)

    # ---------------------------------------------------------------- config <-> widgets

    def _load(self, c: AppConfig) -> None:
        self.background.setText(str(c.video.background or ""))
        self.music.setText(str(c.audio.music or ""))
        self.top_comments.setValue(c.selection.top_comments)
        self.music_volume.setValue(c.audio.music_volume)
        _set_combo(self.provider, c.tts.provider)
        self.op_replies.setValue(c.selection.op_replies)
        self.speed.setValue(c.tts.speed)
        self.random_voice.setChecked(c.tts.random_voice_per_author)
        _set_combo(self.aspect, c.video.aspect)
        _set_combo(self.theme, c.card.theme)
        _set_combo(self.position, c.video.card_position)
        _set_combo(self.animation, c.video.animation)
        self.subtitles.setChecked(c.video.subtitles)
        _set_combo(self.language, c.language)
        self.review.setChecked(c.selection.review_before_tts)
        _set_combo(self.identity, c.card.identity)
        self.sfx_enabled.setChecked(c.audio.sfx_enabled)
        self.sfx.setText(str(c.audio.sfx_dir or ""))
        self.sfx_volume.setValue(c.audio.sfx_volume)
        self.preview_text.setText(PREVIEW_TEXT.get(c.language, PREVIEW_TEXT["en"]))
        self.music_enabled.setChecked(c.audio.music_enabled)
        self.translate.setChecked(c.translate.enabled)
        self.api_key.setText(c.translate.api_key)
        _set_combo(self.model, c.translate.model)

    def _read(self) -> AppConfig:
        c = self.cfg.model_copy(deep=True)
        c.video.background = Path(self.background.text().strip()) if self.background.text().strip() else None
        c.audio.music = Path(self.music.text().strip()) if self.music.text().strip() else None
        c.selection.top_comments = self.top_comments.value()
        c.audio.music_volume = self.music_volume.value()
        c.tts.provider = self.provider.currentData()
        # Unusable fallbacks (e.g. Piper without a voice) are skipped at run time.
        c.tts.fallback = [p for p in ("vieneu", "edge", "piper") if p != c.tts.provider]
        if self.voice.currentData():
            c.tts.voice = self.voice.currentData()
        c.tts.op_voice = self.op_voice.currentData() or None
        c.selection.op_replies = self.op_replies.value()
        c.tts.speed = round(self.speed.value(), 2)
        c.tts.random_voice_per_author = self.random_voice.isChecked()
        c.video.aspect = self.aspect.currentData()
        c.card.theme = self.theme.currentData()
        c.video.card_position = self.position.currentData()
        c.video.animation = self.animation.currentData()
        c.video.subtitles = self.subtitles.isChecked()
        c.language = _combo_value(self.language) or "vi"
        c.selection.review_before_tts = self.review.isChecked()
        c.card.identity = self.identity.currentData()
        c.audio.sfx_enabled = self.sfx_enabled.isChecked()
        c.audio.sfx_dir = Path(self.sfx.text().strip()) if self.sfx.text().strip() else None
        c.audio.sfx_volume = self.sfx_volume.value()
        c.audio.music_enabled = self.music_enabled.isChecked()
        c.translate.enabled = self.translate.isChecked()
        c.translate.api_key = self.api_key.text().strip()
        c.translate.model = _combo_value(self.model)
        return c

    def save_settings(self) -> AppConfig:
        self.cfg = self._read()
        save_config(self.cfg, self.config_path)
        return self.cfg

    # ---------------------------------------------------------------- helpers

    def _run_task(self, task: Task, on_done, on_fail=None) -> None:
        self.tasks = [t for t in self.tasks if t.thread.isRunning()] + [task]
        task.done.connect(on_done)
        task.failed.connect(on_fail or (lambda msg: QMessageBox.warning(self, "Error", msg)))
        task.start()

    def _append_log(self, text: str, level: int) -> None:
        if level >= logging.ERROR:
            text = f"❌ {text}"
        elif level >= logging.WARNING:
            text = f"⚠ {text}"
        self.log_view.appendPlainText(text)

    def _play(self, path: Path, volume: float = 1.0) -> None:
        if self.player is None:
            self.player = QMediaPlayer(self)
            self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
        self.player.stop()
        self.audio_out.setVolume(volume)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def _check_browser(self) -> None:
        """First run: Playwright's Chromium (fetching + card rendering) may need downloading."""
        from .. import browser_env

        def missing(installed: bool) -> None:
            if installed:
                return
            answer = QMessageBox.question(
                self, "Download browser",
                "This app needs a Chromium browser (about 170 MB download) to read Threads and draw the cards.\n\nDownload it now?")
            if answer != QMessageBox.Yes:
                log.warning("Chromium is not installed; videos can't be made until it is")
                return
            self.generate_btn.setEnabled(False)
            self.status.setText("Downloading Chromium…")
            task = Task(lambda: browser_env.install_browser(lambda line: log.info("%s", line)))
            self._run_task(task, lambda _: (self.generate_btn.setEnabled(True), self.status.setText("Chromium ready")),
                           lambda msg: (self.generate_btn.setEnabled(True),
                                        QMessageBox.warning(self, "Browser download failed", msg)))

        self._run_task(Task(browser_env.browser_installed), missing, lambda msg: log.warning("Browser check: %s", msg))

    # ---------------------------------------------------------------- voices & previews

    def _refresh_voices(self) -> None:
        cfg = self._read()
        provider, lang = cfg.tts.provider, cfg.language

        def load() -> list[tuple[str, str]]:
            from ..tts.manager import default_providers

            p = default_providers(cfg.tts)[provider]
            reason = p.unavailable_reason(lang)
            if reason:
                raise RuntimeError(reason)
            # Base voices only; pitch variants are used automatically for random per-author voices.
            return [(str(v), v.label or str(v)) for v in p.voices(lang)
                    if not v.pitch or v.pitch.lstrip("+-") in ("0Hz", "0%")]

        def fill(voices: list[tuple[str, str]]) -> None:
            for combo, current, allow_none in [(self.voice, self.cfg.tts.voice, False),
                                               (self.op_voice, self.cfg.tts.op_voice, True)]:
                combo.clear()
                if allow_none:
                    combo.addItem("Same as default voice", "")
                for value, label in voices:
                    combo.addItem(label, value)
                _set_combo(combo, current or "")
            self.status.setText(f"{len(voices)} voice(s) for {provider} / {lang}")

        def failed(msg: str) -> None:
            self.voice.clear()
            self.op_voice.clear()
            self.status.setText(f"{provider}: {msg}")
            log.warning("%s: %s", provider, msg)

        self.status.setText(f"Loading {provider} voices…")
        self._run_task(Task(load), fill, failed)

    def _preview_voice(self) -> None:
        cfg = self._read()
        text = self.preview_text.text().strip() or PREVIEW_TEXT.get(cfg.language, PREVIEW_TEXT["en"])
        out = Path(tempfile.gettempdir()) / f"threads2video_preview_{random.randrange(10**6)}.wav"

        def synth() -> Path:
            from ..ffmpeg import find_ffmpeg
            from ..tts import TTSManager

            return TTSManager(cfg.tts, cfg.language, find_ffmpeg(cfg.ffmpeg_path)).preview(text, out)

        self.preview_btn.setEnabled(False)
        self.status.setText("Synthesizing preview…")

        def done(path: Path) -> None:
            self.preview_btn.setEnabled(True)
            self.status.setText("Playing voice preview")
            self._play(path)

        def failed(msg: str) -> None:
            self.preview_btn.setEnabled(True)
            QMessageBox.warning(self, "Voice preview failed", msg)

        self._run_task(Task(synth), done, failed)

    def _preview_sfx(self) -> None:
        cfg = self._read()

        def pick() -> Path:
            from ..compose.audio import AUDIO_EXTS, default_pop, pick_media
            from ..ffmpeg import find_ffmpeg

            return pick_media(cfg.audio.sfx_dir, AUDIO_EXTS, random.Random()) or default_pop(
                find_ffmpeg(cfg.ffmpeg_path), cfg.cache_dir)

        self._run_task(Task(pick), lambda path: self._play(path, cfg.audio.sfx_volume / 100))

    def _download_piper(self) -> None:
        cfg = self._read()
        PiperDownloadDialog(cfg.language, cfg.tts.piper_models_dir, self).exec()
        self._refresh_voices()

    def _download_vieneu(self) -> None:
        from ..tts.vieneu import download_model, is_downloaded

        models_dir = self._read().tts.vieneu_models_dir
        try:
            import vieneu  # noqa: F401
        except ImportError:
            QMessageBox.warning(self, "VieNeu-TTS", "The vieneu package is not installed (pip install vieneu).")
            return
        if is_downloaded(models_dir):
            QMessageBox.information(self, "VieNeu-TTS", f"The VieNeu model is already downloaded to\n{models_dir}")
            return
        answer = QMessageBox.question(
            self, "Download VieNeu-TTS",
            "Download the VieNeu-TTS Vietnamese voice model (about 570 MB, once)?\n\n"
            "It runs offline on this computer. It reads the video when Edge TTS fails, "
            "or choose it as the TTS engine.")
        if answer != QMessageBox.Yes:
            return

        def finish(ok: bool, msg: str = "") -> None:
            self.vieneu_btn.setEnabled(True)
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000 if ok else 0)
            if ok:
                self.status.setText("VieNeu-TTS model ready")
                self._refresh_voices()
            else:
                QMessageBox.warning(self, "Download failed", msg)

        self.vieneu_btn.setEnabled(False)
        self.status.setText("Downloading VieNeu-TTS model…")
        task = Task(lambda progress: download_model(models_dir, progress), with_progress=True)
        # Progress in MB so the bar's int range is never exceeded.
        task.progress.connect(lambda done, total: (self.progress.setRange(0, max(total >> 20, 1)),
                                                   self.progress.setValue(done >> 20)))
        self._run_task(task, lambda _: finish(True), lambda msg: finish(False, msg))

    def _check_gemini(self) -> None:
        cfg = self._read()

        def check() -> list[str]:
            from ..translate import GeminiTranslator

            return GeminiTranslator(cfg.translate.api_key, cfg.translate.model).list_models()

        def done(models: list[str]) -> None:
            current = _combo_value(self.model)
            self.model.clear()
            preferred = [m for m in models if "flash" in m] or models
            for m in preferred:
                self.model.addItem(m, m)
            _set_combo(self.model, current)
            ok = current in models
            QMessageBox.information(self, "Gemini", f"API key works. {len(models)} models available."
                                    + ("" if ok else f"\n\n⚠ '{current}' is not among them; pick another model."))

        self._run_task(Task(check), done, lambda msg: QMessageBox.warning(self, "Gemini check failed", msg))

    def _login(self) -> None:
        cfg = self.save_settings()
        if not cfg.fetch.profile_dir:
            QMessageBox.warning(self, "Login", "Set fetch.profile_dir in the config file first.")
            return

        def run() -> bool:
            from ..fetcher import login_interactive

            return login_interactive(cfg.fetch)

        def done(logged_in: bool) -> None:
            self._set_login_open(False)
            self.status.setText("Logged in to Threads" if logged_in else "Not logged in (window closed before login)")

        def failed(msg: str) -> None:
            self._set_login_open(False)
            QMessageBox.warning(self, "Login failed", msg)

        # The login window holds the browser profile, so a job can't fetch until it is closed.
        self._set_login_open(True)
        self.status.setText("Log in in the browser window, then close it")
        self._run_task(Task(run), done, failed)

    def _set_login_open(self, open_: bool) -> None:
        self.login_open = open_
        self.login_btn.setEnabled(not open_)
        self.generate_btn.setEnabled(not open_)
        self.retry_btn.setEnabled(not open_ and bool(self.job_failed))
        self.generate_btn.setToolTip("Close the Threads login window first" if open_ else "")

    # ---------------------------------------------------------------- jobs

    def urls(self) -> list[str]:
        """URLs from the box, one per line or whitespace-separated, without duplicates."""
        return list(dict.fromkeys(u.strip() for u in self.url.toPlainText().split() if u.strip()))

    def _generate(self, urls: list[str] | None = None) -> None:
        urls = urls or self.urls()
        bad = []
        for u in urls:
            try:
                parse_post_url(u)
            except ValueError:
                bad.append(u)
        if not urls or bad:
            QMessageBox.warning(self, "Threads URL", "Enter a Threads post URL like https://www.threads.com/@user/post/…"
                                + (f"\n\nNot a post URL: {bad[0]}" if bad else ""))
            return
        cfg = self.save_settings()
        if cfg.translate.enabled and not cfg.translate.api_key:
            QMessageBox.warning(self, "Gemini", "Translation is on but the Gemini API key is empty.")
            return
        self.job_urls = urls
        self.job_videos: dict[int, Path] = {}
        self.job_failed: dict[int, str] = {}
        self.current_job = 0
        self.queue.setRowCount(len(urls))
        for i, u in enumerate(urls):
            ref = parse_post_url(u)
            self.queue.setItem(i, 0, QTableWidgetItem(f"@{ref.username}/{ref.code}" if ref.username else ref.code))
            self.queue.item(i, 0).setToolTip(u)
            self.queue.setItem(i, 1, QTableWidgetItem("Queued"))
            self.queue.setItem(i, 2, QTableWidgetItem(""))
        self.worker = JobWorker(urls, cfg, reuse_fetch=self.reuse_fetch.isChecked())
        self.worker.progress.connect(self._on_progress)
        self.worker.job_started.connect(self._on_job_started)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker.review_requested.connect(self._on_review)
        self.worker.finished.connect(self._on_finished)
        self.generate_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.login_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        if len(urls) > 1:
            log.info("Batch of %d posts queued", len(urls))
        self.worker.start()

    def _retry_failed(self) -> None:
        failed = [self.job_urls[i] for i in sorted(self.job_failed)]
        if failed:
            self._generate(failed)

    def _cancel(self) -> None:
        if self.worker:
            self.status.setText("Cancelling…")
            self.worker.cancel()

    def _set_row(self, index: int, status: str, video: str | None = None) -> None:
        if 0 <= index < self.queue.rowCount():
            self.queue.item(index, 1).setText(status)
            if video is not None:
                self.queue.item(index, 2).setText(video)

    def _on_job_started(self, index: int, url: str) -> None:
        self.current_job = index
        self.queue.selectRow(index)
        prefix = f"[{index + 1}/{len(self.job_urls)}] " if len(self.job_urls) > 1 else ""
        log.info("▶ %s%s", prefix, url)

    def _on_progress(self, stage: str, overall: float, message: str) -> None:
        n = max(1, len(self.job_urls))
        self.progress.setValue(int((self.current_job + overall) / n * 1000))
        prefix = f"[{self.current_job + 1}/{n}] " if n > 1 else ""
        self.status.setText(prefix + message)
        self._set_row(self.current_job, message)

    def _on_review(self, segments: list[Segment]) -> None:
        dlg = ReviewDialog(segments, self)
        accepted = dlg.exec() == ReviewDialog.Accepted
        if self.worker:
            self.worker.provide_review(dlg.result_segments() if accepted else None)

    def _on_job_done(self, index: int, result: JobResult) -> None:
        self.last_video = result.video
        self.job_videos[index] = result.video
        self._set_row(index, "✅ Done", result.video.name if result.video else "")
        if result.video:
            self.queue.item(index, 2).setToolTip(str(result.video))
        log.info("✅ Video saved: %s", result.video)

    def _on_job_failed(self, index: int, message: str) -> None:
        self.job_failed[index] = message
        if message in ("Cancelled", "Skipped at review"):
            self._set_row(index, message)
            log.info("Job %d: %s", index + 1, message)
        else:
            self._set_row(index, "❌ Failed")
            self.queue.item(index, 1).setToolTip(message)
            log.error("Job %d: %s", index + 1, message)

    def _on_finished(self) -> None:
        cancelled = self.worker.cancelled
        for i in range(self.current_job + 1, len(self.job_urls)):
            if i not in self.job_videos and i not in self.job_failed:
                self.job_failed[i] = "Cancelled"
                self._set_row(i, "Cancelled")
        self.generate_btn.setEnabled(True)
        self.login_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.retry_btn.setEnabled(bool(self.job_failed))
        ok, n = len(self.job_videos), len(self.job_urls)
        if n > 1:
            summary = f"Batch finished: {ok} of {n} videos made" + (f", {n - ok} failed/skipped" if ok < n else "")
            log.info(summary)
        else:
            summary = f"Done: {self.last_video}" if ok else ("Stopped" if cancelled else "Failed; see the log")
        if ok == n:
            self.progress.setValue(1000)
        self.status.setText(summary)
        self.worker = None

    def _open_row_video(self, row: int, _col: int) -> None:
        video = getattr(self, "job_videos", {}).get(row)
        if video and Path(video).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(video).resolve())))

    def _open_output(self) -> None:
        folder = Path(self._read().output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self.worker:
            if QMessageBox.question(self, "Quit", "A video is being generated. Cancel it and quit?") != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.thread.wait(10_000)
        self.save_settings()
        logging.getLogger().removeHandler(self.log_bridge)
        super().closeEvent(event)
