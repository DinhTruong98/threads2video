import json
import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from threads2video.config import AppConfig, load_config  # noqa: E402
from threads2video.gui.dialogs import ReviewDialog  # noqa: E402
from threads2video.gui.workers import overall_progress  # noqa: E402
from threads2video.models import Author, Segment  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "thread_sample"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from threads2video.gui import main_window

    monkeypatch.setattr(main_window.MainWindow, "_refresh_voices", lambda self: None)  # no network in tests
    monkeypatch.setattr(main_window.MainWindow, "_check_browser", lambda self: None)
    w = main_window.MainWindow(tmp_path / "config.yaml", AppConfig(cache_dir=tmp_path / "cache"))
    yield w
    w.close()


def test_settings_roundtrip_through_widgets(window, tmp_path):
    w = window
    w.top_comments.setValue(7)
    w.op_replies.setValue(3)
    w.music_volume.setValue(35)
    w.speed.setValue(1.25)
    w.aspect.setCurrentIndex(w.aspect.findData("16:9"))
    w.identity.setCurrentIndex(w.identity.findData("black"))
    w.background.setText(str(tmp_path / "bg"))
    w.translate.setChecked(True)
    w.api_key.setText("secret-key")
    w.model.setEditText("gemini-custom-model")
    w.language.setEditText("ja")
    saved = w.save_settings()

    loaded = load_config(tmp_path / "config.yaml")
    assert loaded == saved
    assert (loaded.selection.top_comments, loaded.selection.op_replies, loaded.audio.music_volume) == (7, 3, 35)
    assert loaded.tts.speed == 1.25 and loaded.video.aspect == "16:9" and loaded.card.identity == "black"
    assert loaded.video.background == tmp_path / "bg"
    assert loaded.translate.enabled and loaded.translate.api_key == "secret-key"
    assert loaded.translate.model == "gemini-custom-model" and loaded.language == "ja"

    # A new window shows the saved values.
    from threads2video.gui.main_window import MainWindow

    w2 = MainWindow(tmp_path / "config.yaml", AppConfig())
    assert w2.top_comments.value() == 7 and w2.aspect.currentData() == "16:9"
    assert w2.api_key.text() == "secret-key" and w2._read().language == "ja"
    w2.close()


def test_generate_rejects_bad_url(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: shown.append(a[2])))
    window.url.setPlainText("https://example.com/not-threads")
    window._generate()
    assert window.worker is None and "Not a post URL" in shown[0]


def test_review_dialog_edits_and_drops(app):
    a = Author(id="1", username="a")
    segs = [Segment(id=str(i), kind="post" if i == 0 else "reply", author=a, text_original=f"orig {i}",
                    text=f"text {i}") for i in range(3)]
    dlg = ReviewDialog(segs)
    assert not dlg.rows[0][0].isEnabled()  # the post can't be dropped
    dlg.rows[1][1].setPlainText("edited")
    dlg.rows[2][0].setChecked(False)
    out = dlg.result_segments()
    assert [s.text for s in out] == ["text 0", "edited"]
    assert out[1].text_original == "orig 1"


def test_overall_progress_is_monotonic():
    points = [overall_progress(st, f) for st in ["fetch", "select", "translate", "tts", "render", "compose"]
              for f in (0, 0.5, 1)]
    assert points == sorted(points) and points[0] == 0 and points[-1] == pytest.approx(1)


def test_pipeline_translate_stage(tmp_path, monkeypatch):
    from threads2video import pipeline

    raw = tmp_path / "cache" / "threads" / "SAMPLE001" / "raw"
    raw.mkdir(parents=True)
    for f in FIXTURE.glob("0*.json"):
        shutil.copy(f, raw / f.name)

    class FakeTranslator:
        def __init__(self, key, model, cache_path=None):
            assert key == "k" and model == "m"

        def translate(self, texts, lang):
            return [f"[{lang}] {t}" for t in texts]

    monkeypatch.setattr(pipeline, "GeminiTranslator", FakeTranslator)
    cfg = AppConfig(cache_dir=tmp_path / "cache", work_dir=tmp_path / "work")
    cfg.translate.enabled, cfg.translate.api_key, cfg.translate.model = True, "k", "m"
    url = json.loads((FIXTURE / "meta.json").read_text())["url"]
    res = pipeline.run_job(url, cfg, until="translate", from_cache="auto")
    assert all(s.text == f"[vi] {s.text_original}" for s in res.segments)
    saved = pipeline.load_segments(res.work_dir / "segments.json")
    assert saved[0].text.startswith("[vi] ")
