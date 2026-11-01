import json

import httpx
import pytest

from threads2video.translate.gemini import GeminiTranslator, TranslationError, _batches, language_name


def fake_gemini(handler_log, *, reply=None, status=200, error="", fail_batches=False):
    """httpx transport imitating generateContent: 'translates' by prefixing 'VI:'."""

    def handler(request: httpx.Request) -> httpx.Response:
        handler_log.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": error}})
        if request.method == "GET":
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]}]})
        body = json.loads(request.content)
        prompt = body["contents"][0]["parts"][0]["text"]
        texts = json.loads(prompt.split("Input:\n", 1)[1])
        out = reply(texts) if reply else ["VI:" + t for t in texts]
        if fail_batches and len(texts) > 1:
            out = out[:-1]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(out)}]}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_translate_keeps_order_skips_empty_and_dedupes(tmp_path):
    log = []
    tr = GeminiTranslator("key", client=fake_gemini(log), cache_path=tmp_path / "c.json")
    out = tr.translate(["Hello", "", "World", "Hello"], "vi")
    assert out == ["VI:Hello", "", "VI:World", "VI:Hello"]
    assert len(log) == 1
    req = log[0]
    assert req.headers["x-goog-api-key"] == "key"
    assert req.url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")
    body = json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "Vietnamese" in body["contents"][0]["parts"][0]["text"]
    assert "key" not in req.url.query.decode()  # key goes in a header, never the URL


def test_cache_avoids_repeat_requests(tmp_path):
    log = []
    GeminiTranslator("k", client=fake_gemini(log), cache_path=tmp_path / "c.json").translate(["A", "B"], "vi")
    tr2 = GeminiTranslator("k", client=fake_gemini(log), cache_path=tmp_path / "c.json")
    assert tr2.translate(["B", "A", "C"], "vi") == ["VI:B", "VI:A", "VI:C"]
    assert len(log) == 2  # second translator only asked for "C"
    # Another language or model is a different cache entry.
    GeminiTranslator("k", client=fake_gemini(log), cache_path=tmp_path / "c.json").translate(["A"], "ja")
    assert len(log) == 3


def test_bad_batch_falls_back_to_single_requests():
    log = []
    tr = GeminiTranslator("k", client=fake_gemini(log, fail_batches=True))
    assert tr.translate(["one", "two", "three"], "vi") == ["VI:one", "VI:two", "VI:three"]
    assert len(log) == 4  # 1 failed batch + 3 singles


@pytest.mark.parametrize("status, error, match", [
    (400, "API key not valid. Please pass a valid API key.", "rejected the API key"),
    (404, "models/nope is not found", "not found"),
    (403, "Permission denied on resource", "Gemini error 403"),
])
def test_errors_are_clear(status, error, match):
    tr = GeminiTranslator("k", model="nope", client=fake_gemini([], status=status, error=error))
    with pytest.raises(TranslationError, match=match):
        tr.translate(["x"], "vi")


def test_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"message": "quota"}}, headers={"retry-after": "1"})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '["ok"]'}]}}]})

    tr = GeminiTranslator("k", client=httpx.Client(transport=httpx.MockTransport(handler)), retries=3)
    assert tr.translate(["x"], "vi") == ["ok"]
    assert len(calls) == 3


def test_missing_key():
    with pytest.raises(TranslationError, match="API key is missing"):
        GeminiTranslator("  ")


def test_list_models_filters_generate_content():
    tr = GeminiTranslator("k", client=fake_gemini([]))
    assert tr.list_models() == ["gemini-3.5-flash-lite"]


def test_batches_and_language_names():
    texts = ["x" * 3000] * 5 + ["y"] * 60
    batches = _batches(texts)
    assert sum(map(len, batches)) == len(texts)
    assert all(len(b) <= 25 and sum(map(len, b)) <= 8000 for b in batches)
    assert language_name("vi") == "Vietnamese" and language_name("vi-VN") == "Vietnamese"
    assert language_name("xx") == "xx"
