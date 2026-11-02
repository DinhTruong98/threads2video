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
    monkeypatch.setenv("VERTEX_API_KEY", "vertex-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    assert load_config(None).translate.api_key == "vertex-key"  # Vertex AI is the default service
    p = tmp_path / "c.yaml"
    p.write_text("translate:\n  service: gemini\n", encoding="utf-8")
    assert load_config(p).translate.api_key == "gemini-key"


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


def test_settings_missing_from_an_older_file_come_from_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("tts:\n  provider: piper\n  piper_models_dir: /data/piper\nvideo:\n  background: null\n")
    defaults = AppConfig()
    defaults.tts.vieneu_models_dir = Path("/home/me/models/vieneu")
    defaults.video.background = Path("/bg")
    cfg = load_config(path, defaults)
    assert cfg.tts.provider == "piper" and cfg.tts.piper_models_dir == Path("/data/piper")
    assert cfg.tts.vieneu_models_dir == Path("/home/me/models/vieneu")  # added after the file was written
    assert cfg.video.background is None  # explicit values in the file still win


def test_home_folder_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("video:\n  background: ~/Movies/bg\naudio:\n  music: ~/Movies/music\n")
    cfg = load_config(path)
    assert cfg.video.background == Path.home() / "Movies/bg"
    assert cfg.audio.music == Path.home() / "Movies/music"
    cfg.video.background = Path("~/clips")  # as typed into the GUI
    assert cfg.video.background == Path.home() / "clips"
