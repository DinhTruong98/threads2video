"""VieNeu-TTS v3 Turbo (local, offline, Vietnamese) run on CPU with ONNX Runtime.

https://github.com/pnnbao97/VieNeu-TTS (code and model: Apache-2.0). The model
(~540 MB) is downloaded as plain files into our own models folder: loading it
from the Hugging Face cache fails with recent onnxruntime, which rejects ONNX
external data reached through the cache's symlinks.
"""

from __future__ import annotations

import json
import logging
import threading
import wave
from pathlib import Path
from typing import Callable

from .base import TTSError, TTSProvider, Voice

log = logging.getLogger(__name__)

HF_BASE = "https://huggingface.co"
MODEL_REPO = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
MODEL_SUBFOLDER = "onnx_update"  # fp32 graphs; "onnx_int8" is faster but can distort on CPUs without VNNI
CODEC_REPO = "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"
# (repo, subfolder, files, local folder): what VieNeu's ONNX engine loads.
_PARTS = [
    (MODEL_REPO, MODEL_SUBFOLDER, [
        "vieneu_prefill.onnx", "vieneu_decode_step.onnx", "vieneu_acoustic_cached.onnx",
        "vieneu_backbone_shared.data", "vieneu_v3_heads.npz", "config.json", "tokenizer.json",
    ], "model"),
    (CODEC_REPO, "", [
        "moss_audio_tokenizer_decode_full.onnx", "moss_audio_tokenizer_decode_shared.data",
        "moss_audio_tokenizer_decode_step.onnx", "codec_browser_onnx_meta.json",
        "moss_audio_tokenizer_encode.onnx", "moss_audio_tokenizer_encode.data",
    ], "codec"),
]
MANIFEST = "manifest.json"
# Default voice first, then the OP voice used when the configured one belongs to another engine.
_PREFERRED = ["Trúc Ly", "Hải Đăng"]

Progress = Callable[[int, int], None]  # (bytes_done, bytes_total)

_models: dict[Path, object] = {}
_load_lock = threading.Lock()
_infer_lock = threading.Lock()


def _remote_sizes(repo: str, subfolder: str) -> dict[str, int]:
    import httpx

    url = f"{HF_BASE}/api/models/{repo}/tree/main" + (f"/{subfolder}" if subfolder else "")
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return {Path(e["path"]).name: int(e.get("lfs", {}).get("size") or e["size"]) for e in resp.json()
            if e.get("type") == "file"}


def is_downloaded(models_dir: Path) -> bool:
    manifest = Path(models_dir) / MANIFEST
    if not manifest.exists():
        return False
    sizes = json.loads(manifest.read_text(encoding="utf-8"))
    return all((Path(models_dir) / rel).is_file() and (Path(models_dir) / rel).stat().st_size == size
               for rel, size in sizes.items())


def download_model(models_dir: Path, progress: Progress | None = None) -> None:
    """Download the model and audio codec into models_dir, resuming files already complete."""
    import httpx

    models_dir = Path(models_dir)
    plan = []
    for repo, sub, files, local in _PARTS:
        sizes = _remote_sizes(repo, sub)
        missing = [f for f in files if f not in sizes and not f.endswith(".json")]
        if missing:
            raise TTSError(f"{repo} no longer has {missing}; update threads2video")
        plan += [(f"{repo}/resolve/main/{sub + '/' if sub else ''}{f}", f"{local}/{f}", sizes[f])
                 for f in files if f in sizes]
    total, done = sum(size for *_, size in plan), 0
    for url, rel, size in plan:
        dest = models_dir / rel
        if dest.exists() and dest.stat().st_size == size:
            done += size
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        log.info("Downloading %s (%.1f MB)", rel, size / 1e6)
        with httpx.stream("GET", f"{HF_BASE}/{url}", follow_redirects=True, timeout=60) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            raise TTSError(f"Incomplete download of {rel}; try again")
        tmp.replace(dest)
    (models_dir / MANIFEST).write_text(json.dumps({rel: size for _, rel, size in plan}, indent=1), encoding="utf-8")
    log.info("VieNeu-TTS model ready in %s", models_dir)


def preset_voices() -> list[dict]:
    """Built-in voices from the vieneu package (no model load): name, gender, description, featured rank."""
    from importlib import resources

    data = json.loads((resources.files("vieneu") / "assets" / "voices_v3_turbo.json").read_text(encoding="utf-8"))
    voices = [{"name": n, "gender": v.get("gender", ""), "description": v.get("description", ""),
               "featured": v.get("featured") or 99} for n, v in data.get("presets", {}).items()]
    return sorted(voices, key=lambda v: (_PREFERRED.index(v["name"]) if v["name"] in _PREFERRED else len(_PREFERRED),
                                         v["featured"]))


def _load(models_dir: Path):
    with _load_lock:
        if models_dir not in _models:
            from vieneu import Vieneu
            from vieneu._v3_turbo_engine import onnx_runtime_lite as engine

            # The engine fetches the codec from the Hub itself; hand it our local copy instead.
            original = engine.OnnxV3LiteEngine._fetch
            engine.OnnxV3LiteEngine._fetch = staticmethod(lambda repo, files, sub: models_dir / "codec")
            try:
                log.info("Loading VieNeu-TTS model (first use takes a few seconds)")
                _models[models_dir] = Vieneu(mode="v3turbo", backend="onnx", backbone_repo=str(models_dir / "model"),
                                             onnx_dir=str(models_dir / "model"))
            finally:
                engine.OnnxV3LiteEngine._fetch = original
        return _models[models_dir]


class VieNeuTTS(TTSProvider):
    name = "vieneu"

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = Path(models_dir).resolve()

    def unavailable_reason(self, lang: str) -> str | None:
        if lang.split("-")[0].lower() != "vi":
            return "VieNeu-TTS only reads Vietnamese"
        try:
            import vieneu  # noqa: F401
        except ImportError:
            return "vieneu is not installed (pip install vieneu)"
        if not is_downloaded(self.models_dir):
            return f"VieNeu model not downloaded to {self.models_dir}; use 'Download VieNeu model' (~540 MB)"
        return None

    def voices(self, lang: str) -> list[Voice]:
        if lang.split("-")[0].lower() != "vi":
            return []
        return [Voice(self.name, v["name"], label=f"{v['name']} — {v['description']}") for v in preset_voices()]

    def synthesize(self, text: str, voice: Voice, out_stem: Path) -> Path:
        import numpy as np

        out = out_stem.with_suffix(".wav")
        try:
            model = _load(self.models_dir)
            with _infer_lock:  # one ONNX session set, already multi-threaded
                audio = np.asarray(model.infer(text, voice=voice.id), dtype=np.float32).reshape(-1)
            if not audio.size:
                raise TTSError("no audio generated")
            pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
            with wave.open(str(out), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(model.sample_rate)
                wf.writeframes(pcm.tobytes())
        except TTSError:
            raise
        except Exception as e:
            raise TTSError(f"VieNeu-TTS failed for {voice}: {e}") from e
        return out
