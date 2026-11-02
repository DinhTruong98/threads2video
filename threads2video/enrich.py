"""AI step (Gemini model on Vertex AI or the Gemini API): spoken text without abbreviations, and a reaction sound per comment.

The whole thread goes to the model in one request, in video order, so it can use
context: "T" is "tao" in "T nói rồi" but a letter in "áo size T". Images attached
to the post or comments are sent too (ai.use_images), because the joke is often
in the picture. The answer is cached on disk, so re-running a video costs nothing.

Failures never stop the video: the segments are returned unchanged and a warning
is logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .config import AppConfig
from .models import Segment
from .render.cards import AssetCache
from .translate import GeminiClient, TranslationError, language_name
from .translate.gemini import TranslationCache

log = logging.getLogger(__name__)

PROMPT_VERSION = 1
# Mood → when the audience would make that sound. Keys match folders in compose/reactions/.
REACTIONS = {
    "laugh": "genuinely funny: a joke, a roast, witty sarcasm, an absurd situation",
    "rimshot": "a punchline, pun or corny one-liner (ba-dum-tss)",
    "gasp": "shocking, outrageous or scandalous",
    "aww": "sweet, cute or wholesome",
    "applause": "something to cheer or loudly agree with",
    "wow": "impressive or amazing",
    "crickets": "awkward, cringe or a joke that falls flat",
    "fail": "a fail, bad luck or something sad (sad trombone)",
}
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 4_000_000

SPEECH_RULES = """- "speech": the text exactly as a native {language} narrator should read it aloud. Write out every
  abbreviation, acronym, teencode and shorthand as full words, deciding ambiguous ones from the context of the
  thread. Vietnamese examples: k/ko/kh/hok/hông → không, đc/dc → được, r → rồi, vs → với, j/gì z → gì vậy,
  z/v (end of sentence) → vậy, ng → người, ny → người yêu, ae → anh em, trc → trước, bth → bình thường,
  nma → nhưng mà, cx → cũng, ms → mới, bik/bít → biết, tks/thanks → cảm ơn, T/t (pronoun) → tao, M/m (pronoun) → mày,
  CM/cm → chúng mày, ck → chồng, vk → vợ, ib → nhắn tin, cmt → bình luận, sp → sản phẩm, tr → trời, khum → không.
  Keep everything else as written: same words, order, tone, slang and swearing. Do not summarise, translate,
  censor, explain or add words. Drop emoji and the "#" of hashtags; read @mentions as the name without "@".
  Leave names, brands and English words as they are. If nothing needs changing, return the text unchanged."""

REACTION_RULES = """- "reaction": the sound a live audience would make right after hearing this item, one of:
{moods}
  - none: anything else.
  Be selective: most items get "none", usually no more than one item in three gets a sound, and never the same
  sound twice in a row. Use the images: the joke is often in the picture."""

PROMPT = """You prepare a Threads post and its comments for a narrated short video in {language}.
Items are in video order; item 0 is the post. "op": true marks the post's author.
{images_note}
For every item return an object with "i" (the item number) and:
{rules}

Items:
{items}"""


def _image_parts(segments: list[Segment], assets: AssetCache) -> list[dict]:
    parts, count = [], 0
    for i, seg in enumerate(segments):
        for m in seg.media:
            if m.kind != "image" or count >= MAX_IMAGES:
                continue
            uri = assets.fetch(m.url)
            if not uri:
                continue
            mime, _, data = uri.removeprefix("data:").partition(";base64,")
            if len(data) * 3 // 4 > MAX_IMAGE_BYTES or not mime.startswith("image/"):
                continue
            parts += [{"text": f"Image attached to item {i}:"}, {"inlineData": {"mimeType": mime, "data": data}}]
            count += 1
    return parts


def _schema(speech: bool, reactions: bool) -> dict:
    props: dict = {"i": {"type": "INTEGER"}}
    if speech:
        props["speech"] = {"type": "STRING"}
    if reactions:
        props["reaction"] = {"type": "STRING", "enum": [*REACTIONS, "none"]}
    return {"type": "ARRAY", "items": {"type": "OBJECT", "properties": props, "required": list(props)}}


def _plausible_speech(original: str, speech: str) -> bool:
    """Expanding abbreviations lengthens text a little; a much longer answer is the model rambling."""
    return bool(speech.strip()) and len(speech) <= 2 * len(original) + 60


def enrich_segments(segments: list[Segment], cfg: AppConfig, job_dir: Path,
                    client: GeminiClient | None = None) -> list[Segment]:
    """Fill `speech` and/or `reaction` on each segment (as enabled in cfg.ai); unchanged on failure."""
    ai = cfg.ai
    if not segments or not (ai.speech_text or ai.reactions):
        return segments
    try:
        client = client or GeminiClient(cfg.translate.api_key, cfg.translate.model, service=cfg.translate.service)
    except TranslationError as e:
        log.warning("AI step skipped: %s", e)
        return segments
    language = language_name(cfg.language)
    items = [{"i": i, "author": s.author.username, "op": s.is_op, "text": s.text} for i, s in enumerate(segments)]
    images = _image_parts(segments, AssetCache(job_dir / "assets")) if ai.use_images else []
    rules = []
    if ai.speech_text:
        rules.append(SPEECH_RULES.format(language=language))
    if ai.reactions:
        rules.append(REACTION_RULES.format(moods="\n".join(f"  - {k}: {v}" for k, v in REACTIONS.items())))
    prompt = PROMPT.format(
        language=language, rules="\n".join(rules), items=json.dumps(items, ensure_ascii=False, indent=1),
        images_note=f"{len(images) // 2} image(s) attached to items follow the text." if images else "")
    parts = [{"text": prompt}, *images]

    cache = TranslationCache(Path(cfg.cache_dir) / "ai.json")
    key = hashlib.sha1(json.dumps([PROMPT_VERSION, parts], ensure_ascii=False).encode()).hexdigest()
    cached = cache.get(client.model, "enrich", key)
    try:
        answer = json.loads(cached) if cached else client.generate_json(
            parts, _schema(ai.speech_text, ai.reactions), temperature=0.2)
    except TranslationError as e:
        log.warning("AI step failed, continuing without it: %s", e)
        return segments
    if not cached:
        cache.put_many(client.model, "enrich", [(key, json.dumps(answer, ensure_ascii=False))])

    by_index = {a.get("i"): a for a in answer if isinstance(a, dict)} if isinstance(answer, list) else {}
    out, changed, reactions = [], 0, []
    for i, seg in enumerate(segments):
        a = by_index.get(i, {})
        update: dict = {}
        speech = a.get("speech")
        if ai.speech_text and isinstance(speech, str):
            if not _plausible_speech(seg.text, speech):
                log.warning("AI speech text for segment %d ignored (implausible): %.80s", i, speech)
            elif speech.strip() != seg.text.strip():
                update["speech"] = speech.strip()
                changed += 1
        reaction = a.get("reaction")
        if ai.reactions and reaction in REACTIONS:
            update["reaction"] = reaction
            reactions.append(f"{i}:{reaction}")
        out.append(seg.model_copy(update=update) if update else seg)
    if len(by_index) < len(segments):
        log.warning("AI answered for %d of %d segments", len(by_index), len(segments))
    log.info("AI (%s%s): %s%s", client.model, ", from cache" if cached else "",
             f"rewrote {changed} text(s) for reading" if ai.speech_text else "",
             (", " if ai.speech_text else "") + f"reactions {', '.join(reactions) or 'none'}" if ai.reactions else "")
    return out
