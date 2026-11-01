from pathlib import Path

import pytest
from pydantic import ValidationError

from threads2video.config import AppConfig, load_config, save_config

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.language == "vi"
    assert cfg.tts.speed == pytest.approx(1.10)
    assert cfg.audio.music_volume == 20 and cfg.audio.sfx_volume == 40
    assert cfg.video.size == (1080, 1920)
    assert cfg.translate.api_key == ""


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_roundtrip(tmp_path, suffix, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = AppConfig()
    cfg.selection.top_comments = 7
    cfg.video.aspect = "16:9"
    cfg.video.background = Path("bg/videos")
    cfg.translate.api_key = "k"
    path = tmp_path / f"cfg{suffix}"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded == cfg
    assert loaded.video.size == (1920, 1080)


def test_partial_file_and_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("selection:\n  op_replies: 4\nsomething_old: 1\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.selection.op_replies == 4
    assert cfg.selection.top_comments == 5


def test_env_overrides_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert load_config(None).translate.api_key == "from-env"


def test_validation_errors(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("audio:\n  music_volume: 150\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)
    with pytest.raises(ValidationError):
        AppConfig().video.aspect = "4:3"


def test_api_key_hidden_from_repr():
    cfg = AppConfig()
    cfg.translate.api_key = "secret"
    assert "secret" not in repr(cfg)


def test_example_config_is_valid(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = load_config(EXAMPLE)
    assert cfg.translate.api_key == ""
