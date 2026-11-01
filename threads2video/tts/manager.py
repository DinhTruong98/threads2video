"""Runs TTS for a list of segments: voice assignment, provider fallback, WAV normalisation.

Every segment ends up as 48 kHz mono 16-bit WAV, and its duration is read from
the WAV header (frames / rate), so the composer gets exact timings rather than
MP3 estimates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import wave
from pathlib import Path
from typing import Callable

from ..config import TTSConfig
from ..ffmpeg import FFmpeg
from ..models import Segment
from ..retry import retry
from .base import TTSError, TTSProvider, Voice
from .capcut import CapCutTTS
from .edge import EdgeTTS
from .piper import PiperTTS
from .text import clean_for_speech

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
# Card shown with no narration (e.g. an image-only post).
SILENT_HOLD_S = 2.0
SILENCE_DB = -50
# Bump when audio post-processing changes so cached segment audio is regenerated.
AUDIO_VERSION = 2

Progress = Callable[[int, int, str], None]  # (done, total, message)


def default_providers(cfg: TTSConfig) -> dict[str, TTSProvider]:
    return {"edge": EdgeTTS(), "piper": PiperTTS(cfg.piper_models_dir), "capcut": CapCutTTS()}


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


class VoiceAssigner:
    """Maps segments to voice "slots" so the same author keeps one voice per video.

    Slots are provider-independent ("op", "default" or an author index), so a
    fallback provider still gives each author a consistent voice of its own.
    """

    def __init__(self, cfg: TTSConfig, lang: str, seed: str) -> None:
        self.cfg, self.lang, self.seed = cfg, lang, seed
        self._authors: dict[str, int] = {}

    def slot(self, seg: Segment) -> str | int:
        if seg.is_op:
            return "op"
        if not self.cfg.random_voice_per_author:
            return "default"
        return self._authors.setdefault(seg.author.id, len(self._authors))

    def voice(self, provider: TTSProvider, slot: str | int) -> Voice:
        pool = provider.voices(self.lang)
        if not pool:
            raise TTSError(f"{provider.name} has no voices for {self.lang!r}")
        default = provider.parse_voice(self.cfg.voice, self.lang) or pool[0]
        op = provider.parse_voice(self.cfg.op_voice, self.lang) or default
        if slot == "op":
            return op
        if slot == "default":
            return default
        # Commenters should not sound like the OP; shuffle per video so voices vary between videos.
        others = [v for v in pool if v != op] or pool
        random.Random(f"{self.seed}:{provider.name}").shuffle(others)
        return others[int(slot) % len(others)]


class TTSManager:
    def __init__(self, cfg: TTSConfig, lang: str, ffmpeg: FFmpeg,
                 providers: dict[str, TTSProvider] | None = None) -> None:
        self.cfg, self.lang, self.ffmpeg = cfg, lang, ffmpeg
        self.providers = providers if providers is not None else default_providers(cfg)
        self._chain: list[TTSProvider] | None = None

    def chain(self) -> list[TTSProvider]:
        """Usable providers in order: the chosen one, then fallbacks."""
        if self._chain is None:
            names = list(dict.fromkeys([self.cfg.provider, *self.cfg.fallback]))
            chain = []
            for n in names:
                p = self.providers.get(n)
                reason = "unknown provider" if p is None else p.unavailable_reason(self.lang)
                if reason:
                    log.warning("TTS provider %s skipped: %s", n, reason)
                else:
                    if not p.stable:
                        log.warning("TTS provider %s is unofficial and may break; falling back on failure", n)
                    chain.append(p)
            if not chain:
                raise TTSError(f"No usable TTS provider among {names} for language {self.lang!r}")
            self._chain = chain
        return self._chain

    def _to_wav(self, src: Path, dst: Path, *, pitch_pct: float = 0.0) -> float:
        """Convert to 48 kHz mono WAV, applying the speed and an optional pitch shift; return the duration.

        Leading/trailing silence is trimmed (edge-tts pads ~0.9 s at the end) so
        pacing is the same for every provider; the composer adds the gaps.
        """
        trim = f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}dB:start_silence=0.05"
        filters = [trim, "areverse", trim.replace("0.05", "0.1"), "areverse"]
        tempo = self.cfg.speed
        if pitch_pct:
            # Resample to raise/lower pitch, then compensate the tempo change that comes with it.
            factor = 1 + pitch_pct / 100
            filters += [f"asetrate={round(self.ffmpeg.sample_rate(src) * factor)}", f"aresample={SAMPLE_RATE}"]
            tempo /= factor
        if abs(tempo - 1) > 1e-6:
            filters.append(f"atempo={tempo:.6f}")
        self.ffmpeg.run("-i", str(src), "-af", ",".join(filters),
                        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst))
        duration = wav_duration(dst)
        if duration < 0.05:
            raise TTSError(f"{src.name} is silent after trimming")
        return duration

    def _silence(self, dst: Path, seconds: float) -> float:
        self.ffmpeg.run("-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono", "-t", f"{seconds}",
                        "-c:a", "pcm_s16le", str(dst))
        return wav_duration(dst)

    def speak(self, text: str, slot: str | int, assigner: VoiceAssigner, out: Path) -> tuple[str, float]:
        """Synthesize one text to `out` (WAV), trying providers in order; returns (voice key, duration)."""
        errors = []
        for provider in self.chain():
            voice = assigner.voice(provider, slot)
            raw_stem = out.with_name(out.stem + f".{provider.name}.raw")

            def attempt() -> Path:
                return provider.synthesize(text, voice, raw_stem)

            try:
                raw = retry(attempt, attempts=provider.retries + 1, what=f"{provider.name} TTS", retry_on=(TTSError,),
                            base_delay=provider.retry_delay)
                pitch = 0.0
                if voice.pitch and not provider.native_pitch:
                    pitch = float(voice.pitch.rstrip("%"))
                duration = self._to_wav(raw, out, pitch_pct=pitch)
                raw.unlink(missing_ok=True)
                return voice.key, duration
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                log.warning("TTS with %s failed, trying next provider: %s", provider.name, e)
        raise TTSError("All TTS providers failed: " + "; ".join(errors))

    def synthesize_segments(self, segments: list[Segment], out_dir: Path,
                            progress: Progress | None = None) -> list[Segment]:
        """Fill voice/audio/duration on each segment. Unchanged segments are reused from out_dir."""
        out_dir.mkdir(parents=True, exist_ok=True)
        assigner = VoiceAssigner(self.cfg, self.lang, seed=segments[0].id if segments else "")
        result = []
        for i, seg in enumerate(segments):
            if progress:
                progress(i, len(segments), f"TTS {i + 1}/{len(segments)}")
            text = clean_for_speech(seg.text)
            slot = assigner.slot(seg)
            out = out_dir / f"{i:02d}_{seg.id}.wav"
            meta_path = out.with_suffix(".json")
            key = hashlib.sha1(json.dumps([AUDIO_VERSION, text, str(slot), assigner.seed, self.cfg.model_dump(mode="json")],
                                          ensure_ascii=False).encode()).hexdigest()
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            if out.exists() and meta.get("key") == key:
                voice_key, duration = meta["voice"], wav_duration(out)
            elif not text:
                voice_key, duration = "silence", self._silence(out, SILENT_HOLD_S)
            else:
                voice_key, duration = self.speak(text, slot, assigner, out)
            meta_path.write_text(json.dumps({"key": key, "voice": voice_key, "text": text}, ensure_ascii=False),
                                 encoding="utf-8")
            log.info("Segment %d (%s, %s): %.2fs", i, seg.author.username, voice_key, duration)
            result.append(seg.model_copy(update={"voice": voice_key, "audio": str(out), "duration": duration}))
        if progress:
            progress(len(segments), len(segments), "TTS done")
        return result

    def preview(self, text: str, out: Path, *, as_op: bool = False) -> Path:
        assigner = VoiceAssigner(self.cfg, self.lang, seed="preview")
        self.speak(clean_for_speech(text), "op" if as_op else "default", assigner, out)
        return out
