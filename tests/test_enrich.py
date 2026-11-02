import json
import random

import pytest

from threads2video.compose.audio import REACTIONS_DIR, pick_reaction
from threads2video.config import AppConfig
from threads2video.enrich import REACTIONS, enrich_segments
from threads2video.models import Author, MediaItem, Segment
from threads2video.translate import TranslationError


class FakeGemini:
    model = "fake-model"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def generate_json(self, parts, schema, temperature=0.3):
        self.calls.append((parts, schema))
        if self.error:
            raise self.error
        return self.answer


def seg(i, text, media=()):
    return Segment(id=str(i), kind="post" if i == 0 else "reply", author=Author(id=f"u{i}", username=f"user{i}"),
                   is_op=i == 0, text=text, text_original=text, media=list(media))


def cfg_for(tmp_path, speech=True, reactions=True, images=False):
    cfg = AppConfig(cache_dir=tmp_path / "cache")
    cfg.ai.speech_text, cfg.ai.reactions, cfg.ai.use_images = speech, reactions, images
    return cfg


SEGS = [seg(0, "T nói r, ko tin thì thôi"), seg(1, "Oracle đóng cửa VN thật hả"), seg(2, "áo size T còn ko shop")]


def test_speech_and_reactions_applied(tmp_path):
    gem = FakeGemini([{"i": 0, "speech": "Tao nói rồi, không tin thì thôi", "reaction": "none"},
                      {"i": 1, "speech": "Oracle đóng cửa VN thật hả", "reaction": "gasp"},
                      {"i": 2, "speech": "áo size T còn không shop", "reaction": "laugh"}])
    out = enrich_segments(SEGS, cfg_for(tmp_path), tmp_path, client=gem)
    assert out[0].speech == "Tao nói rồi, không tin thì thôi" and out[0].reaction is None
    assert out[1].speech is None  # unchanged text is not duplicated
    assert out[2].speech == "áo size T còn không shop" and out[2].reaction == "laugh"
    assert [s.text for s in out] == [s.text for s in SEGS]  # cards keep the original
    parts, schema = gem.calls[0]
    assert '"T nói r, ko tin thì thôi"' in parts[0]["text"]
    assert set(schema["items"]["properties"]) == {"i", "speech", "reaction"}
    assert set(schema["items"]["properties"]["reaction"]["enum"]) == {*REACTIONS, "none"}


def test_cached_answer_is_reused(tmp_path):
    gem = FakeGemini([{"i": 0, "speech": "Tao nói rồi", "reaction": "laugh"}])
    first = enrich_segments(SEGS[:1], cfg_for(tmp_path), tmp_path, client=gem)
    again = enrich_segments(SEGS[:1], cfg_for(tmp_path), tmp_path, client=gem)
    assert len(gem.calls) == 1 and again == first


def test_only_enabled_parts_requested_and_used(tmp_path):
    gem = FakeGemini([{"i": 0, "reaction": "aww"}])
    out = enrich_segments(SEGS[:1], cfg_for(tmp_path, speech=False), tmp_path, client=gem)
    assert set(gem.calls[0][1]["items"]["properties"]) == {"i", "reaction"}
    assert "abbreviation" not in gem.calls[0][0][0]["text"]
    assert out[0].speech is None and out[0].reaction == "aww"


def test_rambling_or_unknown_answers_ignored(tmp_path):
    gem = FakeGemini([{"i": 0, "speech": "Tao nói rồi. " * 40, "reaction": "explosion"}])
    out = enrich_segments(SEGS[:1], cfg_for(tmp_path), tmp_path, client=gem)
    assert out[0].speech is None and out[0].reaction is None


def test_failure_keeps_segments(tmp_path):
    gem = FakeGemini(error=TranslationError("Gemini error 500"))
    assert enrich_segments(SEGS, cfg_for(tmp_path), tmp_path, client=gem) == SEGS
    # No key at all: skipped with a warning, no exception.
    assert enrich_segments(SEGS, cfg_for(tmp_path), tmp_path) == SEGS


def test_images_are_sent_with_their_item(tmp_path, monkeypatch):
    monkeypatch.setattr("threads2video.enrich.AssetCache.fetch",
                        lambda self, url: "data:image/jpeg;base64,/9j/AAAA" if url else None)
    segs = [SEGS[0], seg(1, "nhìn cái ảnh này", media=[MediaItem(kind="image", url="https://cdn/x.jpg")])]
    gem = FakeGemini([{"i": 0, "speech": "x", "reaction": "none"}, {"i": 1, "speech": "y", "reaction": "laugh"}])
    enrich_segments(segs, cfg_for(tmp_path, images=True), tmp_path, client=gem)
    parts = gem.calls[0][0]
    assert parts[1] == {"text": "Image attached to item 1:"}
    assert parts[2] == {"inlineData": {"mimeType": "image/jpeg", "data": "/9j/AAAA"}}


def test_builtin_reaction_sounds_for_every_mood(tmp_path):
    rng = random.Random(1)
    for mood in REACTIONS:
        assert pick_reaction(mood, None, rng).is_relative_to(REACTIONS_DIR / mood)
    own = tmp_path / "laugh"
    own.mkdir()
    (own / "mine.mp3").write_bytes(b"x")
    assert pick_reaction("laugh", tmp_path, rng) == own / "mine.mp3"
    assert pick_reaction("gasp", tmp_path, rng).is_relative_to(REACTIONS_DIR)  # no own gasp: built-in
    assert (REACTIONS_DIR / "CREDITS.md").exists()
