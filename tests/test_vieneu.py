import json

import pytest

from threads2video.tts.vieneu import MANIFEST, VieNeuTTS, is_downloaded


def test_is_downloaded_needs_manifest_and_complete_files(tmp_path):
    assert not is_downloaded(tmp_path)
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "a.onnx").write_bytes(b"x" * 10)
    (tmp_path / MANIFEST).write_text(json.dumps({"model/a.onnx": 10}))
    assert is_downloaded(tmp_path)
    (tmp_path / "model" / "a.onnx").write_bytes(b"x" * 5)  # interrupted download
    assert not is_downloaded(tmp_path)


def test_only_vietnamese(tmp_path):
    p = VieNeuTTS(tmp_path)
    assert "Vietnamese" in p.unavailable_reason("en")
    assert p.voices("en") == []


def test_voices_and_missing_model(tmp_path):
    pytest.importorskip("vieneu")
    p = VieNeuTTS(tmp_path)
    assert "not downloaded" in p.unavailable_reason("vi")
    voices = p.voices("vi")
    assert len(voices) >= 10 and len({v.id for v in voices}) == len(voices)
    # Default (female) then the voice used for the OP when the configured one belongs to another engine.
    assert [v.id for v in voices[:2]] == ["Trúc Ly", "Hải Đăng"]
