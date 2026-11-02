"""Gemini model client (generateContent, JSON output) and batch translation on top of it.

Two services serve the same models with the same request format: Vertex AI with an API
key ("express mode", aiplatform.googleapis.com) and the Gemini API (Google AI Studio).

Called over plain HTTPS (httpx) rather than through an SDK, to keep the
packaged app small. Texts are sent in batches as a JSON array and must come
back as an array of the same length; anything else falls back to one request
per text. Results are cached on disk by (model, language, text).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
VERTEX_BASE = "https://aiplatform.googleapis.com/v1/publishers/google"
SERVICES = {"vertex": "Vertex AI", "gemini": "Gemini API"}
DEFAULT_MODEL = "gemini-3.5-flash-lite"
SUGGESTED_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]
MAX_BATCH_TEXTS = 25
MAX_BATCH_CHARS = 8000

LANGUAGE_NAMES = {
    "vi": "Vietnamese", "en": "English", "ja": "Japanese", "ko": "Korean", "zh": "Simplified Chinese",
    "th": "Thai", "id": "Indonesian", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
}

PROMPT = """You translate social media posts and comments from Threads into {language} for a narrated short video.

Rules:
- Translate every item of the input JSON array; return a JSON array of strings with exactly {count} items, in the same order.
- Natural, casual {language} as a native speaker would write it online; keep the tone (jokes, sarcasm, slang, profanity level).
- Keep @mentions, #hashtags, URLs, emoji and line breaks as they are. Do not add notes or explanations.
- If an item is already in {language}, return it unchanged.

Input:
{texts}"""


class TranslationError(RuntimeError):
    pass


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.split("-")[0].lower(), code)


class TranslationCache:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if path and path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("Ignoring unreadable translation cache %s", path)

    @staticmethod
    def key(model: str, lang: str, text: str) -> str:
        return hashlib.sha1(f"{model}\0{lang}\0{text}".encode()).hexdigest()

    def get(self, model: str, lang: str, text: str) -> str | None:
        return self._data.get(self.key(model, lang, text))

    def put_many(self, model: str, lang: str, pairs: list[tuple[str, str]]) -> None:
        with self._lock:
            for src, dst in pairs:
                self._data[self.key(model, lang, src)] = dst
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")


class GeminiClient:
    """Key, model, retries and JSON-mode generation; shared by translation and the AI enrich step."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, service: str = "gemini",
                 cache_path: Path | None = None, client: httpx.Client | None = None, retries: int = 3,
                 timeout: float = 60) -> None:
        self.service, self.name = service, SERVICES[service]
        if not api_key.strip():
            raise TranslationError(f"{self.name} API key is missing (set translate.api_key)")
        self.api_key, self.model = api_key.strip(), model.strip().removeprefix("models/")
        self.client = client or httpx.Client(timeout=timeout)
        self.retries = retries
        self.cache = TranslationCache(cache_path)

    # --- HTTP -------------------------------------------------------------------------

    def _request(self, method: str, url: str, **kw) -> dict:
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.client.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as e:
                if attempt == self.retries:
                    raise TranslationError(f"Network error talking to {self.name}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return resp.json()
            detail = _error_message(resp)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                wait = float(resp.headers.get("retry-after") or 2 ** attempt)
                log.warning("%s %d (%s); retrying in %.0fs", self.name, resp.status_code, detail, wait)
                time.sleep(min(wait, 60))
                continue
            if resp.status_code in (400, 401, 403) and "API key" in detail:
                raise TranslationError(f"{self.name} rejected the API key: {detail}")
            if resp.status_code == 404:
                raise TranslationError(f"{self.name} model {self.model!r} not found; pick another model ({detail})")
            raise TranslationError(f"{self.name} error {resp.status_code}: {detail}")
        raise AssertionError("unreachable")

    def list_models(self) -> list[str]:
        """Model ids usable with generateContent (also validates the key).

        Vertex AI API keys cannot list models, so there a one-word test request checks the
        key and the chosen model, and the suggested models are returned.
        """
        if self.service == "vertex":
            self.generate_json([{"text": 'Reply with the JSON string "ok".'}], {"type": "STRING"})
            return list(dict.fromkeys([self.model, *SUGGESTED_MODELS]))
        names, token = [], None
        while True:
            params = {"pageSize": 200, **({"pageToken": token} if token else {})}
            data = self._request("GET", f"{API_BASE}/models", params=params)
            for m in data.get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    names.append(m["name"].removeprefix("models/"))
            token = data.get("nextPageToken")
            if not token:
                return names

    def generate_json(self, parts: list[dict], schema: dict, *, temperature: float = 0.3):
        """One generateContent call whose answer must be JSON matching `schema`; returns it parsed.

        `parts` are content parts: {"text": ...} or {"inlineData": {"mimeType", "data"}}.
        """
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json",
                                 "responseSchema": schema},
        }
        base = VERTEX_BASE if self.service == "vertex" else API_BASE
        data = self._request("POST", f"{base}/models/{self.model}:generateContent", json=body)
        try:
            out_parts = data["candidates"][0]["content"]["parts"]
            return json.loads("".join(p.get("text", "") for p in out_parts))
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            reason = (data.get("candidates") or [{}])[0].get("finishReason") or data.get("promptFeedback")
            raise TranslationError(f"Unexpected {self.name} response ({reason or e})") from e


class GeminiTranslator(GeminiClient):
    def _generate(self, texts: list[str], lang: str) -> list[str]:
        prompt = PROMPT.format(language=language_name(lang), count=len(texts),
                               texts=json.dumps(texts, ensure_ascii=False))
        out = self.generate_json([{"text": prompt}], {"type": "ARRAY", "items": {"type": "STRING"}})
        if not isinstance(out, list) or len(out) != len(texts) or not all(isinstance(x, str) for x in out):
            raise TranslationError(f"{self.name} returned {len(out) if isinstance(out, list) else type(out).__name__} "
                                   f"items for {len(texts)} texts")
        return out

    # --- public -----------------------------------------------------------------------

    def translate(self, texts: list[str], target_lang: str) -> list[str]:
        """Translate texts (order kept). Empty strings and cached texts are not sent."""
        result: list[str | None] = [t if not t.strip() else self.cache.get(self.model, target_lang, t) for t in texts]
        todo = list(dict.fromkeys(t for t, r in zip(texts, result) if r is None))
        done: dict[str, str] = {}
        for batch in _batches(todo):
            try:
                translated = self._generate(batch, target_lang)
            except TranslationError as e:
                if len(batch) == 1 or "API key" in str(e) or "not found" in str(e):
                    raise
                log.warning("Batch translation failed (%s); translating one by one", e)
                translated = [self._generate([t], target_lang)[0] for t in batch]
            done.update(zip(batch, translated))
            self.cache.put_many(self.model, target_lang, list(zip(batch, translated)))
        if todo:
            log.info("Translated %d text(s) to %s with %s", len(todo), language_name(target_lang), self.model)
        return [r if r is not None else done[t] for t, r in zip(texts, result)]


def _batches(texts: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    size = 0
    for t in texts:
        if not out or len(out[-1]) >= MAX_BATCH_TEXTS or size + len(t) > MAX_BATCH_CHARS:
            out.append([])
            size = 0
        out[-1].append(t)
        size += len(t)
    return out


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return resp.text[:200]
