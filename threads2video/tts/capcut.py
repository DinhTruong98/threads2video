"""CapCut voices through the unofficial `capcut_tts_api` package (UNSTABLE).

The package (github.com/K07VN/capcut-tts-api) reverse-engineers CapCut's
internal speech service. It has no license, so it is not bundled or listed as
a dependency: users who want CapCut voices install it themselves. Any failure
here is expected sooner or later; the manager then falls back to the next
provider.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import TTSError, TTSProvider, Voice

log = logging.getLogger(__name__)

_LOCALES = {"vi": "vi-VN", "en": "en-US", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN", "th": "th-TH", "id": "id-ID"}


def _locale(lang: str) -> str:
    return lang if "-" in lang else _LOCALES.get(lang.lower(), lang)


class CapCutTTS(TTSProvider):
    name = "capcut"
    stable = False
    retries = 1

    def __init__(self, timeout: float = 60) -> None:
        self.timeout = timeout
        self._client = None
        self._voices: dict[str, list[Voice]] = {}

    def _get_client(self):
        if self._client is None:
            try:
                from capcut_tts_api import CapCutClient
            except ImportError as e:
                raise TTSError("capcut_tts_api is not installed") from e
            self._client = CapCutClient(timeout=self.timeout)
        return self._client

    def unavailable_reason(self, lang: str) -> str | None:
        try:
            self._get_client()
        except Exception as e:
            return f"CapCut TTS unavailable ({e}). Install it separately: pip install git+https://github.com/K07VN/capcut-tts-api"
        return None if self.voices(lang) else f"no CapCut voices for {lang!r}"

    def voices(self, lang: str) -> list[Voice]:
        loc = _locale(lang)
        if loc not in self._voices:
            try:
                items = self._get_client().list_voices(lang=loc)
                self._voices[loc] = [Voice(self.name, i.voice_type, label=str(i.display_name)) for i in items]
            except Exception as e:
                log.warning("CapCut voice list failed: %s", e)
                self._voices[loc] = []
        return self._voices[loc]

    def synthesize(self, text: str, voice: Voice, out_stem: Path) -> Path:
        out = out_stem.with_suffix(".mp3")
        try:
            client = self._get_client()
            result = client.generate_speech(text, voice=voice.id, timeout=self.timeout * 3)
            path = Path(client.download_tts_audio(result, str(out)))
        except Exception as e:
            raise TTSError(f"CapCut TTS failed for {voice}: {e}") from e
        if not path.exists() or path.stat().st_size == 0:
            raise TTSError("CapCut TTS returned no audio")
        return path
