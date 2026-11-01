"""Audio tracks for the composer, built at exact sample positions.

Narration and SFX are laid out in Python on the timeline (48 kHz mono s16),
so card changes and speech line up to the sample; FFmpeg then only has to mix
them with the music.
"""

from __future__ import annotations

import hashlib
import random
import wave
from pathlib import Path

import numpy as np

from ..ffmpeg import FFmpeg
from ..tts.manager import SAMPLE_RATE
from .timeline import Timeline

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# A short synthetic "pop": a fast downward chirp with an exponential decay.
POP_EXPR = "0.9*sin(2*PI*(380+900*exp(-t*45))*t)*exp(-t*28)"
POP_SECONDS = 0.22
# Narration level after normalisation (RMS of voiced parts); the mix lands near -14 LUFS (Shorts/TikTok).
SPEECH_TARGET_DB = -14.0


def pick_media(path: Path | None, exts: set[str], rng: random.Random) -> Path | None:
    """A file as-is, or a random matching file from a folder (searched recursively)."""
    if path is None:
        return None
    p = Path(path)
    if p.is_file():
        return p
    if p.is_dir():
        files = sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts)
        if files:
            return rng.choice(files)
    return None


def default_pop(ffmpeg: FFmpeg, cache_dir: Path) -> Path:
    out = Path(cache_dir) / "sfx" / "pop.wav"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.run("-f", "lavfi", "-i", f"aevalsrc='{POP_EXPR}':s={SAMPLE_RATE}:d={POP_SECONDS}",
                   "-ac", "1", "-c:a", "pcm_s16le", str(out))
    return out


def read_pcm(path: Path) -> np.ndarray:
    """48 kHz mono s16 WAV → float32 samples in [-1, 1]."""
    with wave.open(str(path), "rb") as wf:
        if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError(f"{path} must be {SAMPLE_RATE} Hz mono 16-bit (got "
                             f"{wf.getframerate()} Hz, {wf.getnchannels()} ch, {8 * wf.getsampwidth()} bit)")
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768


def write_pcm(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def speech_rms_db(x: np.ndarray) -> float:
    """RMS of the voiced part (20 ms blocks above -45 dBFS), in dBFS."""
    block = SAMPLE_RATE // 50
    n = len(x) // block
    if n == 0:
        return -120.0
    rms = np.sqrt(np.mean(x[: n * block].reshape(n, block) ** 2, axis=1))
    voiced = rms[rms > 10 ** (-45 / 20)]
    if voiced.size == 0:
        return -120.0
    return float(20 * np.log10(np.sqrt(np.mean(voiced ** 2))))


def normalize_speech(x: np.ndarray, target_db: float = SPEECH_TARGET_DB, peak: float = 0.95) -> np.ndarray:
    """Bring speech to a common level (providers and voices differ by 10 dB+) without clipping."""
    level = speech_rms_db(x)
    if level <= -100:
        return x
    gain = 10 ** ((target_db - level) / 20)
    top = float(np.abs(x).max()) * gain
    if top > peak:
        gain *= peak / top
    return x * gain


def build_narration(audio_files: list[Path], timeline: Timeline, out: Path) -> Path:
    """Place each segment's (level-normalised) speech at its speech_start; silence elsewhere."""
    track = np.zeros(round(timeline.total * SAMPLE_RATE), dtype=np.float32)
    for f, e in zip(audio_files, timeline.entries):
        pcm = normalize_speech(read_pcm(f))
        start = round(e.speech_start * SAMPLE_RATE)
        pcm = pcm[: max(0, len(track) - start)]
        track[start:start + len(pcm)] = pcm
    write_pcm(out, track)
    return out


def to_pcm_wav(ffmpeg: FFmpeg, src: Path, out_dir: Path) -> Path:
    """Convert any audio file to 48 kHz mono s16 WAV (cached by name)."""
    tag = hashlib.sha1(f"{src.resolve()}:{src.stat().st_mtime_ns}".encode()).hexdigest()[:10]
    out = out_dir / f"{src.stem}.{tag}.wav"
    if not out.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg.run("-i", str(src), "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(out))
    return out


def build_sfx(sounds: list[Path | None], timeline: Timeline, out: Path) -> Path:
    """Mix one sound (48 kHz mono WAV) at each card's appearance; None = no sound for that card."""
    track = np.zeros(round(timeline.total * SAMPLE_RATE), dtype=np.float32)
    for snd, e in zip(sounds, timeline.entries):
        if snd is None:
            continue
        pcm = read_pcm(snd)
        start = round(e.card_in * SAMPLE_RATE)
        pcm = pcm[: max(0, len(track) - start)]
        track[start:start + len(pcm)] += pcm
    write_pcm(out, track)
    return out
