"""Make post text speakable: no links, emoji, @ or # signs, and clear sentence breaks."""

from __future__ import annotations

import re
import unicodedata

from ..selection import URL_RE

_DROP_CATEGORIES = {"So", "Sk", "Cs", "Co", "Cn"}
_INVISIBLE = {"‍", "️", "︎", "⃣"}


def _strip_symbols(text: str) -> str:
    return "".join(
        " " if unicodedata.category(c) in _DROP_CATEGORIES else c
        for c in text
        if c not in _INVISIBLE
    )


_ACRONYMS = {"AI", "USA", "US", "UK", "EU", "CEO", "OK", "TV", "IT", "VN", "PC", "GPT", "NASA", "FBI", "COVID"}


def _is_acronym(word: str) -> bool:
    return word in _ACRONYMS or (len(word) <= 4 and not re.search(r"[AEIOUYĂÂÊÔƠƯ]", word, re.IGNORECASE))


def _uncap(text: str) -> str:
    """'NO ONE WANTS AI' → 'No one wants AI'; TTS engines may spell out shouted words."""
    letters = [c for c in text if c.isalpha() and c.lower() != c.upper()]
    if len(letters) < 12 or sum(c.isupper() for c in letters) < 0.7 * len(letters):
        return text
    t = re.sub(r"\w+", lambda m: m[0] if _is_acronym(m[0]) else m[0].lower(), text)
    return re.sub(r"(^|[.!?]\s+)(\w)", lambda m: m[1] + m[2].upper(), t)


def clean_for_speech(text: str) -> str:
    t = URL_RE.sub(" ", text)
    t = _strip_symbols(t)
    t = _uncap(t)
    t = re.sub(r"[@#]([\w.]+)", lambda m: m[1].replace("_", " ").replace(".", " "), t)
    # Line breaks become sentence breaks unless the line already ends with punctuation.
    lines = [ln.strip() for ln in t.splitlines()]
    t = " ".join(ln if re.search(r"[.!?…:;,]$", ln) else ln + "." for ln in lines if ln)
    t = re.sub(r"([!?])[!?]+", r"\1", t)
    t = re.sub(r"\.{4,}", "...", t)
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^[.,;:\s]+", "", t)
    if t and not re.search(r"[.!?…]$", t):
        t += "."
    return t if re.search(r"[^\W_]", t) else ""
