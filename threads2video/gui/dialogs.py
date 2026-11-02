from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..enrich import REACTIONS
from ..models import Segment
from ..render.cards import compact_count
from .workers import Task

KIND_LABEL = {"post": "Post", "self_thread": "Post (cont.)", "reply": "Comment", "op_reply": "OP reply"}


class ReviewDialog(QDialog):
    """Edit the text shown on each card and what is read aloud, pick reaction sounds, drop segments."""

    def __init__(self, segments: list[Segment], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review texts before reading")
        self.resize(760, 720)
        self.segments = segments
        self.rows: list[tuple[QCheckBox, QPlainTextEdit, QPlainTextEdit | None, QComboBox]] = []

        body = QWidget()
        col = QVBoxLayout(body)
        for i, seg in enumerate(segments):
            frame = QFrame()
            frame.setFrameShape(QFrame.StyledPanel)
            lay = QVBoxLayout(frame)
            head = QHBoxLayout()
            include = QCheckBox(f"#{i + 1}  {KIND_LABEL.get(seg.kind, seg.kind)} · @{seg.author.username}"
                                f" · ♥ {compact_count(seg.like_count) or 0}")
            include.setChecked(True)
            include.setEnabled(seg.kind != "post")  # the post itself always stays
            head.addWidget(include)
            head.addStretch()
            reaction = QComboBox()
            reaction.addItem("No reaction sound", None)
            for mood in REACTIONS:
                reaction.addItem(f"Reaction: {mood}", mood)
            reaction.setCurrentIndex(max(0, reaction.findData(seg.reaction)))
            head.addWidget(reaction)
            lay.addLayout(head)
            if seg.text_original.strip() != seg.text.strip():
                orig = QLabel(seg.text_original)
                orig.setWordWrap(True)
                orig.setTextInteractionFlags(Qt.TextSelectableByMouse)
                orig.setStyleSheet("color: gray;")
                lay.addWidget(orig)
            edit = QPlainTextEdit(seg.text)
            edit.setFixedHeight(max(60, min(200, 22 * (seg.text.count("\n") + len(seg.text) // 70 + 2))))
            lay.addWidget(edit)
            speech = None
            if seg.speech:
                # The AI step wrote abbreviations out in full; that version is what gets read.
                lay.addWidget(QLabel("Read aloud as:"))
                speech = QPlainTextEdit(seg.speech)
                speech.setFixedHeight(edit.height())
                lay.addWidget(speech)
            col.addWidget(frame)
            self.rows.append((include, edit, speech, reaction))
        col.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Continue")
        buttons.button(QDialogButtonBox.Cancel).setText("Skip this post")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Edit what is shown on each card (and what is read aloud, when the AI rewrote it), "
                               "pick reaction sounds, and uncheck comments to drop them."))
        outer.addWidget(scroll)
        outer.addWidget(buttons)

    def result_segments(self) -> list[Segment]:
        out = []
        for seg, (include, edit, speech, reaction) in zip(self.segments, self.rows):
            if include.isChecked():
                text = edit.toPlainText().strip()
                spoken = speech.toPlainText().strip() if speech else None
                out.append(seg.model_copy(update={"text": text, "speech": spoken if spoken and spoken != text else None,
                                                  "reaction": reaction.currentData()}))
        return out


class PiperDownloadDialog(QDialog):
    """List Piper voices for the reading language, flag incompatible ones, download one."""

    def __init__(self, lang: str, models_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Download Piper voice")
        self.resize(560, 360)
        self.lang, self.models_dir = lang, Path(models_dir)
        self.catalog: dict = {}
        self.tasks: list[Task] = []
        self.downloaded: Path | None = None

        self.status = QLabel("Loading voice list…")
        self.list = QListWidget()
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.download_btn = QPushButton("Download")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._download)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(self.bar, 1)
        row.addWidget(self.download_btn)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.status)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        self.list.currentItemChanged.connect(lambda cur, _: self.download_btn.setEnabled(
            cur is not None and bool(cur.data(Qt.UserRole))))
        self._run(Task(self._load), self._loaded)

    def _run(self, task: Task, on_done) -> None:
        self.tasks.append(task)
        task.done.connect(on_done)
        task.failed.connect(lambda msg: self.status.setText(f"Error: {msg}"))
        task.start()

    def _load(self) -> list[tuple[dict, list[str]]]:
        from ..tts.piper import downloadable_voices, fetch_catalog, remote_missing_phonemes

        self.catalog = fetch_catalog()
        return [(v, remote_missing_phonemes(v)) for v in downloadable_voices(self.lang, self.catalog)]

    def _loaded(self, voices: list[tuple[dict, list[str]]]) -> None:
        self.status.setText(f"{len(voices)} Piper voice(s) for '{self.lang}'. Installed into {self.models_dir}")
        for v, missing in voices:
            size = sum(m["size_bytes"] for p, m in v["files"].items() if p.endswith(".onnx")) / 1e6
            installed = (self.models_dir / f"{v['key']}.onnx").exists()
            text = f"{v['key']}   ·  {v['quality']}  ·  {v['num_speakers']} speaker(s)  ·  {size:.0f} MB"
            if installed:
                text += "   ✓ installed"
            if missing:
                text += f"   ⚠ incompatible with this Piper (missing {', '.join(missing)})"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, "" if (installed or missing) else v["key"])
            if missing:
                item.setForeground(Qt.gray)
            self.list.addItem(item)

    def _download(self) -> None:
        from ..tts.piper import download_voice

        key = self.list.currentItem().data(Qt.UserRole)
        self.download_btn.setEnabled(False)
        self.bar.setVisible(True)
        self.status.setText(f"Downloading {key}…")
        task = Task(lambda progress: download_voice(key, self.models_dir, catalog=self.catalog, progress=progress),
                    with_progress=True)
        task.progress.connect(lambda done, total: (self.bar.setMaximum(max(total, 1)), self.bar.setValue(done)))
        task.failed.connect(lambda msg: QMessageBox.warning(self, "Download failed", msg))

        def finished(path: Path) -> None:
            self.downloaded = path
            self.status.setText(f"Installed {path.name}")
            self.list.currentItem().setText(self.list.currentItem().text() + "   ✓ installed")
            self.list.currentItem().setData(Qt.UserRole, "")

        self._run(task, finished)
