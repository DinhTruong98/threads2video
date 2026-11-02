"""Download the bundled reaction sounds (all CC0 on Freesound) and cut them into short clips.

    python tools/fetch_reactions.py

Writes threads2video/compose/reactions/<mood>/*.mp3 and CREDITS.md. Each clip is one
sound event (packs like "9 sitcom laughs" are split on silence), at most a few
seconds long with a fade-out, mono 48 kHz, loudness-normalised so moods mix evenly.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "threads2video" / "compose" / "reactions"

# mood: [(freesound id, preview URL, title, pieces to cut, max seconds per piece)]
SOUNDS = {
    "laugh": [
        (383207, "https://cdn.freesound.org/previews/383/383207_2247456-hq.mp3", "Sitcom Laughter 9x, Small Audience", 4, 3.5),
        (231258, "https://cdn.freesound.org/previews/231/231258_4053644-hq.mp3", "Audience laughing", 2, 3.5),
        (346688, "https://cdn.freesound.org/previews/346/346688_2104797-hq.mp3", "Crowd laughters & clap", 1, 3.5),
    ],
    "rimshot": [
        (383898, "https://cdn.freesound.org/previews/383/383898_7146007-hq.mp3", "Rimshot Joke Funny", 1, 2.5),
        (132418, "https://cdn.freesound.org/previews/132/132418_2412414-hq.mp3", "Rimshot (sweet)", 1, 2.0),
    ],
    "gasp": [
        (324898, "https://cdn.freesound.org/previews/324/324898_2104797-hq.mp3", "Crowd shock", 1, 2.5),
        (635110, "https://cdn.freesound.org/previews/635/635110_13862996-hq.mp3", "Crowd gasp", 1, 2.0),
        (264376, "https://cdn.freesound.org/previews/264/264376_1654262-hq.mp3", "small crowd gasp shock surprise", 1, 2.5),
    ],
    "aww": [
        (124996, "https://cdn.freesound.org/previews/124/124996_687791-hq.mp3", "aww", 1, 2.5),
        (547589, "https://cdn.freesound.org/previews/547/547589_4755077-hq.mp3", "awwwww", 1, 2.0),
    ],
    "applause": [
        (32260, "https://cdn.freesound.org/previews/32/32260_43834-hq.mp3", "smallcrowd", 1, 3.5),
        (182571, "https://cdn.freesound.org/previews/182/182571_71257-hq.mp3", "Short Crowd Cheer", 1, 3.5),
    ],
    "crickets": [
        (129678, "https://cdn.freesound.org/previews/129/129678_2362707-hq.mp3", "crickets", 1, 3.0),
    ],
    "fail": [
        (175409, "https://cdn.freesound.org/previews/175/175409_1326576-hq.mp3", "wah wah sad trombone", 1, 4.0),
        (362206, "https://cdn.freesound.org/previews/362/362206_6629901-hq.mp3", "horn_fail_wahwah_1", 1, 4.0),
    ],
    "wow": [
        (870654, "https://cdn.freesound.org/previews/870/870654_18412227-hq.mp3", "Group Wow 2", 1, 2.6),
        (397317, "https://cdn.freesound.org/previews/397/397317_3366726-hq.mp3", "kids_WOAH", 1, 2.5),
    ],
}


def sound_spans(src: Path, min_len: float = 0.6) -> list[tuple[float, float]]:
    """Non-silent stretches of a file, from ffmpeg's silencedetect."""
    log = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src), "-af", "silencedetect=n=-38dB:d=0.35", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    total = float(re.search(r"Duration: (\d+):(\d+):([\d.]+)", log).group(3)) + 60 * float(
        re.search(r"Duration: (\d+):(\d+)", log).group(2))
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    spans, t = [], 0.0
    for s, e in zip(starts, ends + [total] * (len(starts) - len(ends))):
        if s - t >= min_len:
            spans.append((t, s))
        t = e
    if total - t >= min_len:
        spans.append((t, total))
    return spans or [(0.0, total)]


def cut(src: Path, start: float, length: float, out: Path) -> None:
    fade = min(0.4, length / 3)
    af = f"afade=t=in:d=0.02,afade=t=out:st={length - fade:.3f}:d={fade:.3f},loudnorm=I=-18:TP=-2"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                    "-i", str(src), "-af", af, "-ac", "1", "-ar", "48000", "-c:a", "libmp3lame", "-q:a", "4", str(out)],
                   check=True)


def main() -> None:
    credits = ["# Reaction sounds\n",
               "All sounds are from [Freesound](https://freesound.org) under **CC0 1.0** (public domain): free for any use,",
               "including monetized videos, no attribution required. Clips were trimmed, faded and loudness-normalised",
               "by `tools/fetch_reactions.py`.\n", "| Mood | Clip | Source |", "|---|---|---|"]
    with tempfile.TemporaryDirectory() as tmp:
        for mood, sounds in SOUNDS.items():
            (OUT / mood).mkdir(parents=True, exist_ok=True)
            for sid, url, title, pieces, max_len in sounds:
                src = Path(tmp) / f"{sid}.mp3"
                resp = httpx.get(url, follow_redirects=True, timeout=60)
                resp.raise_for_status()
                src.write_bytes(resp.content)
                # Packs: the longest events first; single sounds: the first event.
                spans = sound_spans(src)
                chosen = sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[:pieces] if pieces > 1 else spans[:1]
                for n, (s, e) in enumerate(sorted(chosen)):
                    name = f"fs{sid}{'_' + str(n + 1) if pieces > 1 else ''}.mp3"
                    cut(src, max(0.0, s - 0.03), min(e - s + 0.25, max_len), OUT / mood / name)
                    credits.append(f"| {mood} | `{mood}/{name}` | [{title}](https://freesound.org/s/{sid}/) |")
                    print(f"{mood:9} {name:16} from {s:5.1f}s ({min(e - s, max_len):.1f}s)  {title}")
    (OUT / "CREDITS.md").write_text("\n".join(credits) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
