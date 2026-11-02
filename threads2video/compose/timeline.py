"""Pure timing math: when each card appears, when its narration plays, when it leaves.

    lead_in | card 0 in → pre_roll → speech 0 → [pause 0] → gap | card 1 in → pre_roll → speech 1 → ... → tail

`pauses` adds time after a segment's narration (room for a reaction sound) while its card stays up.

A card stays on screen until the next one appears (no empty frames between
cards); the last card stays until the end of the video.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimelineEntry:
    index: int
    card_in: float
    speech_start: float
    speech_end: float
    card_out: float

    @property
    def visible(self) -> float:
        return self.card_out - self.card_in


@dataclass(frozen=True)
class Timeline:
    entries: list[TimelineEntry]
    total: float


def build_timeline(durations: list[float], *, lead_in: float = 0.2, pre_roll: float = 0.15,
                   gap: float = 0.35, tail: float = 0.8, pauses: list[float] | None = None) -> Timeline:
    if not durations:
        raise ValueError("Nothing to compose: no segments")
    if any(d <= 0 for d in durations):
        raise ValueError(f"Segment durations must be positive: {durations}")
    pauses = pauses or [0.0] * len(durations)
    starts, t = [], lead_in
    for d, p in zip(durations, pauses):
        starts.append(t)
        t += pre_roll + d + p + gap
    total = t - gap + tail
    entries = [
        TimelineEntry(
            index=i,
            card_in=start,
            speech_start=start + pre_roll,
            speech_end=start + pre_roll + d,
            card_out=starts[i + 1] if i + 1 < len(starts) else total,
        )
        for i, (start, d) in enumerate(zip(starts, durations))
    ]
    return Timeline(entries=entries, total=total)


def split_caption_chunks(text: str, start: float, end: float, max_words: int = 7) -> list[tuple[str, float, float]]:
    """Split narration text into short captions timed proportionally to their length."""
    words = text.split()
    if not words:
        return []
    chunks: list[list[str]] = [[]]
    for w in words:
        chunks[-1].append(w)
        # Break at max length, or after punctuation once the chunk has a few words.
        if len(chunks[-1]) >= max_words or (len(chunks[-1]) >= 3 and w[-1] in ".,!?;:…"):
            chunks.append([])
    chunks = [c for c in chunks if c]
    weights = [sum(len(w) + 1 for w in c) for c in chunks]
    total_w, span = sum(weights), end - start
    out, t = [], start
    for c, w in zip(chunks, weights):
        dt = span * w / total_w
        out.append((" ".join(c), t, t + dt))
        t += dt
    return out
