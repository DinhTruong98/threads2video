"""Build and run the single FFmpeg pass that produces the final MP4.

Inputs: background (file looped from a random offset, or a generated gradient),
the narration and SFX tracks, optional music, one input per card and per
caption. Each card/caption input is a looped still delayed with -itsoffset to
its time window, so FFmpeg only decodes it while it is on screen.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import AppConfig
from ..ffmpeg import FFmpeg
from ..models import Segment
from ..tts.text import clean_for_speech
from .audio import (AUDIO_EXTS, VIDEO_EXTS, build_narration, build_sfx, default_pop, pick_media, pick_reaction,
                    to_pcm_wav)
from .timeline import Timeline, build_timeline, split_caption_chunks

log = logging.getLogger(__name__)

MUSIC_FADE_IN_S = 0.5
# A reaction sound starts this long after the comment's narration ends.
REACTION_DELAY_S = 0.1
# Slide-in distance as a fraction of the frame height.
SLIDE_FRACTION = 0.04


@dataclass
class Overlay:
    image: Path
    start: float
    end: float
    x: str
    y: str
    animated: bool = True


@dataclass
class ComposePlan:
    size: tuple[int, int]
    fps: int
    timeline: Timeline
    narration: Path
    sfx: Path | None
    sfx_volume: float
    music: Path | None
    music_volume: float
    music_fade_out: float
    background: Path | None
    background_offset: float
    cards: list[Overlay]
    captions: list[Overlay] = field(default_factory=list)
    animation: str = "fade"
    animation_s: float = 0.25
    crf: int = 20
    preset: str = "veryfast"
    reactions: Path | None = None
    reaction_volume: float = 0.6


def _f(x: float) -> str:
    return f"{x:.3f}"


def card_y(position: str, subtitles: bool = False) -> str:
    if subtitles:
        # Captions take the bottom (or top, for bottom cards) ~22% of the frame.
        return {"center": "(H*0.76-h)/2", "top": "H*0.06", "bottom": "H*0.94-h"}[position]
    return {"center": "(H-h)/2", "top": "H*0.08", "bottom": "H*0.92-h"}[position]


def caption_y(card_position: str) -> str:
    return "H*0.13-h/2" if card_position == "bottom" else "H*0.87-h/2"


def build_command(plan: ComposePlan, out: Path) -> list[str]:
    """FFmpeg arguments (without the binary) for the final render. Pure: no I/O."""
    w, h = plan.size
    total = plan.timeline.total
    args: list[str] = []
    graph: list[str] = []

    # 0: background
    if plan.background:
        args += ["-stream_loop", "-1", "-ss", _f(plan.background_offset), "-i", str(plan.background)]
    else:
        args += ["-f", "lavfi", "-i",
                 f"gradients=s={w}x{h}:r={plan.fps}:speed=0.006:nb_colors=3:"
                 f"c0=0x1d2b64:c1=0x4e4376:c2=0x0f3443:d={_f(total)}"]
    graph.append(f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
                 f"fps={plan.fps},format=yuv420p[bg]")

    # Audio inputs and mix.
    stereo = "aformat=sample_rates=48000:channel_layouts=stereo"
    mix = []
    args += ["-i", str(plan.narration)]
    graph.append(f"[1:a]{stereo}[nar]")
    mix.append("[nar]")
    n_in = 2
    if plan.sfx is not None:
        args += ["-i", str(plan.sfx)]
        graph.append(f"[{n_in}:a]{stereo},volume={plan.sfx_volume:.3f}[sfx]")
        mix.append("[sfx]")
        n_in += 1
    if plan.reactions is not None:
        args += ["-i", str(plan.reactions)]
        graph.append(f"[{n_in}:a]{stereo},volume={plan.reaction_volume:.3f}[rea]")
        mix.append("[rea]")
        n_in += 1
    if plan.music is not None:
        args += ["-stream_loop", "-1", "-i", str(plan.music)]
        fade_out = min(plan.music_fade_out, total / 2)
        graph.append(f"[{n_in}:a]{stereo},volume={plan.music_volume:.3f},atrim=0:{_f(total)},"
                     f"afade=t=in:d={MUSIC_FADE_IN_S},afade=t=out:st={_f(total - fade_out)}:d={_f(fade_out)}[mus]")
        mix.append("[mus]")
        n_in += 1
    if len(mix) == 1:
        graph.append("[nar]alimiter=limit=0.95:level=0[aout]")
    else:
        graph.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0,"
                     f"alimiter=limit=0.95:level=0[aout]")

    # Card and caption overlays, chained onto the background.
    last = "bg"
    for k, ov in enumerate(plan.cards + plan.captions):
        args += ["-itsoffset", _f(ov.start), "-loop", "1", "-framerate", str(plan.fps),
                 "-t", _f(ov.end - ov.start), "-i", str(ov.image)]
        chain = "format=rgba"
        y = ov.y
        if ov.animated and plan.animation != "none" and plan.animation_s > 0:
            chain += f",fade=t=in:st={_f(ov.start)}:d={_f(plan.animation_s)}:alpha=1"
            if plan.animation == "slide":
                off = round(h * SLIDE_FRACTION)
                y = f"{ov.y}+{off}*max(0,1-(t-{_f(ov.start)})/{_f(plan.animation_s)})"
        graph.append(f"[{n_in}:v]{chain}[ov{k}]")
        graph.append(f"[{last}][ov{k}]overlay=x='{ov.x}':y='{y}':eval=frame:eof_action=pass[v{k}]")
        last = f"v{k}"
        n_in += 1

    return [
        *args,
        "-filter_complex", ";".join(graph),
        "-map", f"[{last}]", "-map", "[aout]",
        "-t", _f(total), "-r", str(plan.fps),
        "-c:v", "libx264", "-preset", plan.preset, "-crf", str(plan.crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out),
    ]


def plan_composition(segments: list[Segment], cfg: AppConfig, work_dir: Path, ffmpeg: FFmpeg, *,
                     rng: random.Random | None = None,
                     render_captions: Callable[..., list[Path]] | None = None) -> ComposePlan:
    """Resolve media, build the audio tracks and caption images; everything the command needs."""
    rng = rng or random.Random()
    missing = [i for i, s in enumerate(segments) if not (s.audio and s.duration and s.card)]
    if missing:
        raise ValueError(f"Segments {missing} have no audio or card yet; run the tts and render stages first")
    v, a = cfg.video, cfg.audio
    reaction_files = [pick_reaction(s.reaction, a.reactions_dir, rng) if s.reaction else None for s in segments]
    for s, f in zip(segments, reaction_files):
        if s.reaction and f is None:
            log.warning("No sound for reaction %r; skipping it", s.reaction)
    timeline = build_timeline([s.duration for s in segments], lead_in=v.lead_in_s, pre_roll=v.pre_roll_s,
                              gap=v.gap_s, tail=v.tail_s,
                              pauses=[v.reaction_pause_s if f else 0.0 for f in reaction_files])
    work_dir.mkdir(parents=True, exist_ok=True)

    narration = build_narration([Path(s.audio) for s in segments], timeline, work_dir / "narration.wav")

    sfx = None
    if a.sfx_enabled:
        if a.sfx_dir:
            picks = [pick_media(a.sfx_dir, AUDIO_EXTS, rng) for _ in segments]
            if not any(picks):
                log.warning("No sound files in %s; using the built-in pop", a.sfx_dir)
        else:
            picks = []
        if not any(picks):
            picks = [default_pop(ffmpeg, cfg.cache_dir)] * len(segments)
        sounds = [to_pcm_wav(ffmpeg, p, work_dir / "sfx") for p in picks]
        sfx = build_sfx(sounds, timeline, work_dir / "sfx.wav")

    reactions = None
    if any(reaction_files):
        sounds = [to_pcm_wav(ffmpeg, f, work_dir / "reactions") if f else None for f in reaction_files]
        reactions = build_sfx(sounds, timeline, work_dir / "reactions.wav", after_speech=REACTION_DELAY_S)
        log.info("Reaction sounds: %s", ", ".join(f"{i}:{s.reaction}" for i, s in enumerate(segments) if s.reaction))

    music = None
    if a.music_enabled:
        music = pick_media(a.music, AUDIO_EXTS, rng)
        if music is None and a.music:
            log.warning("Background music %s not found or has no audio files; continuing without", a.music)
        elif music is None:
            log.info("No background music configured (audio.music); continuing without")

    background = pick_media(v.background, VIDEO_EXTS, rng)
    offset = 0.0
    if background is None and v.background:
        log.warning("Background video %s not found or has no video files; using a generated gradient", v.background)
    elif background is None:
        log.info("No background video configured (video.background); using a generated gradient")
    elif v.random_start:
        bg_dur = ffmpeg.duration(background)
        # Prefer a window that fits without looping; otherwise any start point (it loops).
        offset = rng.uniform(0, bg_dur - timeline.total) if bg_dur > timeline.total else rng.uniform(0, bg_dur * 0.9)
    log.info("Background %s from %.1fs, music %s", background or "gradient", offset, music or "none")

    cards = [Overlay(Path(s.card), e.card_in, e.card_out, "(W-w)/2", card_y(v.card_position, v.subtitles))
             for s, e in zip(segments, timeline.entries)]

    captions: list[Overlay] = []
    if v.subtitles:
        if render_captions is None:
            from ..render.captions import render_captions
        chunks = [c for s, e in zip(segments, timeline.entries)
                  for c in split_caption_chunks(clean_for_speech(s.speech or s.text), e.speech_start, e.speech_end)]
        images = render_captions([c[0] for c in chunks], work_dir / "captions", frame_size=v.size)
        captions = [Overlay(img, start, end, "(W-w)/2", caption_y(v.card_position), animated=False)
                    for img, (_, start, end) in zip(images, chunks)]

    return ComposePlan(
        size=v.size, fps=v.fps, timeline=timeline,
        narration=narration, sfx=sfx, sfx_volume=a.sfx_volume / 100,
        reactions=reactions, reaction_volume=a.reaction_volume / 100,
        music=music, music_volume=a.music_volume / 100, music_fade_out=a.music_fade_out_s,
        background=background, background_offset=offset,
        cards=cards, captions=captions,
        animation=v.animation, animation_s=v.animation_s, crf=v.crf, preset=v.preset,
    )


def compose(segments: list[Segment], cfg: AppConfig, out: Path, work_dir: Path, ffmpeg: FFmpeg, *,
            progress: Callable[[float], None] | None = None, cancel: Callable[[], bool] | None = None,
            rng: random.Random | None = None) -> Path:
    plan = plan_composition(segments, cfg, work_dir, ffmpeg, rng=rng)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    log.info("Encoding %.1fs %dx%d video with %d cards → %s", plan.timeline.total, *plan.size, len(plan.cards), out)
    ffmpeg.run_progress(build_command(plan, tmp), total=plan.timeline.total, progress=progress, cancel=cancel)
    tmp.replace(out)
    return out
