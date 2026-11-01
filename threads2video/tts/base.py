from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class Voice:
    provider: str
    id: str
    speaker: int | None = None  # Piper multi-speaker models
    # Pitch offset: "+12Hz" for providers with native pitch (edge), else "+6%" applied by FFmpeg.
    pitch: str | None = None
    label: str = ""

    def __str__(self) -> str:
        s = self.id
        if self.speaker is not None:
            s += f"#{self.speaker}"
        if self.pitch and self.pitch.lstrip("+-") not in ("0Hz", "0%"):
            s += f"@{self.pitch}"
        return s

    @property
    def key(self) -> str:
        return f"{self.provider}:{self}"


def split_voice_string(s: str) -> tuple[str, int | None, str | None]:
    """'model#3@+12Hz' → ('model', 3, '+12Hz')."""
    pitch = None
    if "@" in s:
        s, pitch = s.split("@", 1)
    speaker = None
    if "#" in s:
        s, sp = s.split("#", 1)
        speaker = int(sp)
    return s, speaker, pitch


class TTSProvider(ABC):
    name: str
    # Unofficial/fragile services are marked unstable in the UI and logs.
    stable: bool = True
    # False if Voice.pitch is a percentage the manager applies with FFmpeg.
    native_pitch: bool = False
    # Network providers get retried; local ones fail fast.
    retries: int = 0
    retry_delay: float = 2.0  # first backoff in seconds, doubled per attempt

    def unavailable_reason(self, lang: str) -> str | None:
        """None if usable for this language, otherwise a human-readable reason."""
        return None

    @abstractmethod
    def voices(self, lang: str) -> list[Voice]:
        """Voice pool for a language; the first entry is the default."""

    def parse_voice(self, s: str | None, lang: str) -> Voice | None:
        """Turn a configured voice string into a Voice this provider can use, else None."""
        if not s:
            return None
        base, speaker, pitch = split_voice_string(s)
        for v in self.voices(lang):
            if v.id == base and (speaker is None or v.speaker == speaker):
                return Voice(self.name, base, speaker if speaker is not None else v.speaker, pitch or v.pitch, v.label)
        return None

    @abstractmethod
    def synthesize(self, text: str, voice: Voice, out_stem: Path) -> Path:
        """Write audio at normal speed to `out_stem` + an extension of the provider's choice; return that path.

        Speed is applied afterwards by the manager (FFmpeg atempo) so every provider
        honours it exactly: Piper's length_scale is not proportional, and edge-tts
        often returns no audio when a rate is set.
        """
