"""Shared test helpers."""

import math
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from threads2video.tts import TTSError, TTSProvider, Voice  # noqa: E402

RATE = 22_050


def write_tone(path: Path, seconds: float, pad: float = 0.5) -> None:
    """pad s silence + seconds of 440 Hz tone + pad s silence."""
    frames = bytearray()
    silence = struct.pack("<h", 0) * int(pad * RATE)
    frames += silence
    for i in range(int(seconds * RATE)):
        frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / RATE)))
    frames += silence
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(bytes(frames))


class FakeTTS(TTSProvider):
    def __init__(self, name="fake", n_voices=4, fail=False, seconds_per_char=0.05):
        self.name, self.n, self.fail, self.spc = name, n_voices, fail, seconds_per_char
        self.calls = []

    def voices(self, lang):
        return [Voice(self.name, f"v{i}") for i in range(self.n)]

    def synthesize(self, text, voice, out_stem):
        self.calls.append((text, str(voice)))
        if self.fail:
            raise TTSError("service down")
        out = out_stem.with_suffix(".wav")
        write_tone(out, len(text) * self.spc)
        return out


