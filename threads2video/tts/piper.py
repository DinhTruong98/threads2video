"""Piper (local, offline) voices from rhasspy/piper-voices, run with the piper-tts package."""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
import wave
from pathlib import Path
from typing import Callable

from .base import TTSError, TTSProvider, Voice

log = logging.getLogger(__name__)

VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
CATALOG_URL = VOICES_BASE + "voices.json"
# Best-sounding model first; it becomes the default when installed.
_PREFERRED = {"vi": ["vi_VN-vais1000-medium", "vi_VN-25hours_single-low", "vi_VN-vivos-x_low"]}
# Sentences used to check that a model knows every phoneme espeak-ng produces.
_SAMPLES = {"vi": "Xin chào các bạn, hôm nay trời đẹp quá! Người Việt Nam rất thân thiện."}
# Single-speaker models get pitch-shifted variants so authors can sound different.
_PITCH_VARIANTS = ["+0%", "+6%", "-6%", "+12%"]

Progress = Callable[[int, int], None]  # (bytes_done, bytes_total)


def fetch_catalog(timeout: float = 30) -> dict:
    import httpx

    resp = httpx.get(CATALOG_URL, follow_redirects=True, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def downloadable_voices(lang: str, catalog: dict | None = None) -> list[dict]:
    """Catalog entries for a language family, e.g. 'vi' → the three vi_VN models."""
    family = lang.split("-")[0].split("_")[0].lower()
    catalog = catalog if catalog is not None else fetch_catalog()
    return sorted((v for v in catalog.values() if v["language"]["family"] == family), key=lambda v: v["key"])


def missing_phonemes(model_config: dict) -> list[str]:
    """Phonemes the installed espeak-ng produces that a model was not trained on.

    Older models (e.g. vi_VN-25hours_single, vi_VN-vivos) lack the tone digits
    current espeak-ng emits for Vietnamese, so they would speak without tones.
    Needs only the small .onnx.json, so it can run before downloading a model.
    """
    from piper import PiperVoice
    from piper.config import PiperConfig

    cfg = PiperConfig.from_dict(model_config)
    family = (model_config.get("language") or {}).get("family") or cfg.espeak_voice.split("-")[0]
    sample = _SAMPLES.get(family, "Hello, this is a test. How are you today?")
    phonemes = {ph for sentence in PiperVoice(session=None, config=cfg).phonemize(sample) for ph in sentence}
    return sorted(ph for ph in phonemes - set(cfg.phoneme_id_map) if not unicodedata.combining(ph))


def remote_missing_phonemes(entry: dict) -> list[str]:
    """missing_phonemes() for a catalog entry, fetching only its .onnx.json."""
    import httpx

    rel = next(p for p in entry["files"] if p.endswith(".onnx.json"))
    resp = httpx.get(VOICES_BASE + rel, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return missing_phonemes(resp.json())


def download_voice(key: str, models_dir: Path, *, catalog: dict | None = None,
                   progress: Progress | None = None) -> Path:
    """Download one voice (.onnx + .onnx.json) into models_dir, verifying MD5; returns the .onnx path."""
    import httpx

    catalog = catalog if catalog is not None else fetch_catalog()
    if key not in catalog:
        raise TTSError(f"Unknown Piper voice {key!r}")
    entry = catalog[key]
    files = {p: meta for p, meta in entry["files"].items() if p.endswith((".onnx", ".onnx.json"))}
    total = sum(m["size_bytes"] for m in files.values())
    done = 0
    models_dir.mkdir(parents=True, exist_ok=True)
    for rel, meta in sorted(files.items(), key=lambda kv: kv[0].endswith(".onnx")):  # small .json first
        dest = models_dir / Path(rel).name
        if dest.exists() and dest.stat().st_size == meta["size_bytes"]:
            done += meta["size_bytes"]
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        md5 = hashlib.md5()
        log.info("Downloading %s (%.1f MB)", dest.name, meta["size_bytes"] / 1e6)
        with httpx.stream("GET", VOICES_BASE + rel, follow_redirects=True, timeout=60) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 16):
                    f.write(chunk)
                    md5.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if md5.hexdigest() != meta["md5_digest"]:
            tmp.unlink(missing_ok=True)
            raise TTSError(f"Checksum mismatch downloading {dest.name}; try again")
        tmp.replace(dest)
    return models_dir / f"{key}.onnx"


class PiperTTS(TTSProvider):
    name = "piper"

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = Path(models_dir)
        self._loaded: dict[str, object] = {}
        self._compat: dict[str, list[str]] = {}

    def installed(self, lang: str) -> list[tuple[str, int, str]]:
        """(model key, speaker count, quality) for installed models of a language, best first."""
        family = lang.split("-")[0].lower()
        out = []
        for onnx in self.models_dir.glob(f"{family}_*.onnx"):
            cfg_path = onnx.with_suffix(".onnx.json")
            if not cfg_path.exists():
                continue
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            out.append((onnx.stem, int(cfg.get("num_speakers") or 1), (cfg.get("audio") or {}).get("quality", "")))
        pref = _PREFERRED.get(family, [])
        return sorted(out, key=lambda m: (pref.index(m[0]) if m[0] in pref else len(pref), m[0]))

    def missing_phonemes(self, key: str) -> list[str]:
        if key not in self._compat:
            cfg = json.loads((self.models_dir / f"{key}.onnx.json").read_text(encoding="utf-8"))
            self._compat[key] = missing_phonemes(cfg)
            if self._compat[key]:
                log.warning("Piper voice %s is incompatible with this piper-tts (missing phonemes %s); skipping it",
                            key, self._compat[key])
        return self._compat[key]

    def unavailable_reason(self, lang: str) -> str | None:
        try:
            import piper  # noqa: F401
        except ImportError:
            return "piper-tts is not installed (pip install piper-tts)"
        if not self.installed(lang):
            return f"no Piper voice for {lang!r} in {self.models_dir}; use 'Download Piper voice'"
        if not self.voices(lang):
            return f"installed Piper voices for {lang!r} are incompatible with this piper-tts version"
        return None

    def voices(self, lang: str) -> list[Voice]:
        single, multi = [], []
        for key, speakers, quality in self.installed(lang):
            if self.missing_phonemes(key):
                continue
            if speakers == 1:
                single += [Voice(self.name, key, pitch=p, label=f"{key} ({quality}){'' if p == '+0%' else ' ' + p}")
                           for p in _PITCH_VARIANTS]
            else:
                multi += [Voice(self.name, key, speaker=i, label=f"{key} speaker {i}") for i in range(speakers)]
        # Default first: the best model at its natural pitch.
        return sorted(single, key=lambda v: v.pitch != "+0%") + multi

    def _voice(self, key: str):
        if key not in self._loaded:
            from piper import PiperVoice

            path = self.models_dir / f"{key}.onnx"
            if not path.exists():
                raise TTSError(f"Piper model not installed: {path}")
            self._loaded[key] = PiperVoice.load(path)
        return self._loaded[key]

    def synthesize(self, text: str, voice: Voice, out_stem: Path) -> Path:
        from piper import SynthesisConfig

        pv = self._voice(voice.id)
        out = out_stem.with_suffix(".wav")
        try:
            with wave.open(str(out), "wb") as wf:
                pv.synthesize_wav(text, wf, syn_config=SynthesisConfig(speaker_id=voice.speaker))
        except Exception as e:
            raise TTSError(f"Piper failed for {voice}: {e}") from e
        return out
