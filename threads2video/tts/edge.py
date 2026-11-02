"""Microsoft Edge 'Read Aloud' voices via edge-tts (free, online, undocumented endpoint)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .base import TTSError, TTSProvider, Voice

log = logging.getLogger(__name__)

# Used when the voice list cannot be fetched (offline); edge has only two Vietnamese voices.
_KNOWN = {
    "vi": [("vi-VN-HoaiMyNeural", "HoaiMy (female)"), ("vi-VN-NamMinhNeural", "NamMinh (male)")],
    "en": [("en-US-AvaNeural", "Ava (female)"), ("en-US-AndrewNeural", "Andrew (male)")],
}
# Pitch offsets that turn each base voice into a few distinct-sounding variants,
# so "random voice per author" has more than two options.
_PITCH_VARIANTS = ["+0Hz", "+12Hz", "-10Hz"]
# Base time limit for one synthesis call, plus a little per character.
HARD_TIMEOUT_S = 30


class EdgeTTS(TTSProvider):
    name = "edge"
    # The service randomly returns "no audio" (30-60% for some words, e.g. names);
    # it is not rate limiting, so retry quickly and often with the same voice.
    # Microsoft's service refuses some text/voice pairs every time ("No audio was received"),
    # so a few quick retries catch the random failures and the rest go to the next provider.
    retries = 2
    retry_delay = 0.5
    native_pitch = True

    def __init__(self) -> None:
        self._catalog: dict[str, list[tuple[str, str]]] = {}

    def _base_voices(self, lang: str) -> list[tuple[str, str]]:
        lang = lang.split("-")[0].lower()
        if lang not in self._catalog:
            try:
                import edge_tts

                all_voices = asyncio.run(edge_tts.list_voices())
                found = [(v["ShortName"], f"{v['ShortName'].split('-')[-1].removesuffix('Neural')} ({v['Gender'].lower()})")
                         for v in all_voices if v["Locale"].lower().startswith(lang + "-")]
            except Exception as e:  # offline or endpoint change
                log.warning("Could not list edge-tts voices (%s); using built-in list", e)
                found = []
            self._catalog[lang] = found or _KNOWN.get(lang, [])
        return self._catalog[lang]

    def unavailable_reason(self, lang: str) -> str | None:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return "edge-tts is not installed (pip install edge-tts)"
        return None if self._base_voices(lang) else f"no edge-tts voices for language {lang!r}"

    def voices(self, lang: str) -> list[Voice]:
        base = self._base_voices(lang)
        return [Voice(self.name, vid, pitch=p, label=label if p == "+0Hz" else f"{label} {p}")
                for p in _PITCH_VARIANTS for vid, label in base]

    def synthesize(self, text: str, voice: Voice, out_stem: Path) -> Path:
        import edge_tts

        out = out_stem.with_suffix(".mp3")
        communicate = edge_tts.Communicate(text, voice.id, pitch=voice.pitch or "+0Hz",
                                           connect_timeout=15, receive_timeout=30)
        # The websocket has been seen to hang indefinitely despite edge-tts's own
        # timeouts; a hard limit turns that into an ordinary (retried) failure.
        limit = HARD_TIMEOUT_S + len(text) / 20
        try:
            asyncio.run(asyncio.wait_for(communicate.save(str(out)), timeout=limit))
        except asyncio.TimeoutError as e:
            raise TTSError(f"edge-tts timed out after {limit:.0f}s for {voice}") from e
        except Exception as e:
            raise TTSError(f"edge-tts failed for {voice}: {e}") from e
        if not out.exists() or out.stat().st_size == 0:
            raise TTSError(f"edge-tts returned no audio for {voice}")
        return out
