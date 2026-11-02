import json
import random
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from threads2video.compose.audio import (SAMPLE_RATE, build_narration, build_sfx, normalize_speech, pick_media,
                                         read_pcm, speech_rms_db, write_pcm)
from threads2video.compose.timeline import build_timeline
from threads2video.compose.video import ComposePlan, Overlay, build_command

FIXTURE = Path(__file__).parent / "fixtures" / "thread_sample"
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")


def tone(seconds, amp=0.3, freq=440):
    t = np.arange(round(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# --- audio -------------------------------------------------------------------------


def test_narration_placed_at_speech_start(tmp_path):
    files = []
    for i, d in enumerate([0.5, 1.0]):
        f = tmp_path / f"{i}.wav"
        write_pcm(f, tone(d))
        files.append(f)
    tl = build_timeline([0.5, 1.0], lead_in=0.2, pre_roll=0.15, gap=0.35, tail=0.8)
    track = read_pcm(build_narration(files, tl, tmp_path / "n.wav"))
    assert len(track) == round(tl.total * SAMPLE_RATE)
    for e in tl.entries:
        s, end = round(e.speech_start * SAMPLE_RATE), round(e.speech_end * SAMPLE_RATE)
        assert not track[s - 10:s].any()  # silence right before
        assert np.abs(track[s:s + 50]).max() > 0  # speech starts exactly here
        assert not track[end + 1:end + 10].any()


def test_normalize_speech_levels_and_peak():
    quiet, loud = tone(1, amp=0.02), tone(1, amp=0.6)
    assert speech_rms_db(normalize_speech(quiet)) == pytest.approx(speech_rms_db(normalize_speech(loud)), abs=0.5)
    spiky = tone(1, amp=0.01)
    spiky[100] = 0.9
    assert np.abs(normalize_speech(spiky)).max() <= 0.95 + 1e-6
    silent = np.zeros(4800, dtype=np.float32)
    assert not normalize_speech(silent).any()


def test_sfx_at_card_in(tmp_path):
    pop = tmp_path / "pop.wav"
    write_pcm(pop, tone(0.1, amp=0.5))
    tl = build_timeline([1.0, 1.0, 1.0])
    track = read_pcm(build_sfx([pop, None, pop], tl, tmp_path / "sfx.wav"))
    starts = [round(e.card_in * SAMPLE_RATE) for e in tl.entries]
    assert np.abs(track[starts[0]:starts[0] + 100]).max() > 0
    assert not track[starts[1]:starts[1] + 4800].any()
    assert np.abs(track[starts[2]:starts[2] + 100]).max() > 0


def test_read_pcm_rejects_wrong_format(tmp_path):
    f = tmp_path / "x.wav"
    with wave.open(str(f), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b"\0" * 400)
    with pytest.raises(ValueError, match="mono"):
        read_pcm(f)


def test_pick_media(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.MOV").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("x")
    exts = {".mp4", ".mov"}
    picks = {pick_media(tmp_path, exts, random.Random(i)).name for i in range(30)}
    assert picks == {"a.mp4", "b.MOV"}
    assert pick_media(tmp_path / "a.mp4", exts, random.Random()) == tmp_path / "a.mp4"
    assert pick_media(None, exts, random.Random()) is None
    assert pick_media(tmp_path / "missing", exts, random.Random()) is None


# --- command builder ---------------------------------------------------------------------


def plan(**kw):
    tl = build_timeline([1.0, 2.0])
    base = dict(size=(1080, 1920), fps=30, timeline=tl, narration=Path("n.wav"), sfx=Path("s.wav"), sfx_volume=0.4,
                music=Path("m.mp3"), music_volume=0.2, music_fade_out=2.0, background=Path("bg.mp4"),
                background_offset=12.5,
                cards=[Overlay(Path(f"c{e.index}.png"), e.card_in, e.card_out, "(W-w)/2", "(H-h)/2")
                       for e in tl.entries])
    base.update(kw)
    return ComposePlan(**base)


def test_command_inputs_and_graph():
    p = plan()
    args = build_command(p, Path("out.mp4"))
    graph = args[args.index("-filter_complex") + 1]
    # bg, narration, sfx, music, 2 cards
    assert args.count("-i") == 6
    assert args[args.index("bg.mp4") - 3:args.index("bg.mp4")] == ["-ss", "12.500", "-i"]
    assert "-stream_loop" in args[:args.index("bg.mp4")]
    assert args[args.index("m.mp3") - 3:args.index("m.mp3") - 1] == ["-stream_loop", "-1"]
    for e in p.timeline.entries:
        i = args.index(f"c{e.index}.png")
        assert args[i - 9:i] == ["-itsoffset", f"{e.card_in:.3f}", "-loop", "1", "-framerate", "30",
                                 "-t", f"{e.card_out - e.card_in:.3f}", "-i"]
        assert f"fade=t=in:st={e.card_in:.3f}" in graph
    assert graph.count("overlay=") == 2
    assert "amix=inputs=3:duration=first:normalize=0" in graph
    assert "volume=0.200" in graph and "volume=0.400" in graph
    assert f"afade=t=out:st={p.timeline.total - 2:.3f}" in graph
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in graph
    last_t = len(args) - 1 - args[::-1].index("-t")  # the output duration option
    assert args[last_t + 1] == f"{p.timeline.total:.3f}"
    assert args[-1] == "out.mp4" and "libx264" in args and "aac" in args


def test_command_minimal_and_slide():
    p = plan(background=None, music=None, sfx=None, animation="slide")
    args = build_command(p, Path("o.mp4"))
    graph = args[args.index("-filter_complex") + 1]
    assert "gradients=s=1080x1920" in args[args.index("lavfi") + 2]
    assert "amix" not in graph and "[nar]alimiter" in graph
    assert "max(0,1-(t-" in graph
    no_anim = build_command(plan(animation="none"), Path("o.mp4"))
    assert "rgba,fade=t=in" not in no_anim[no_anim.index("-filter_complex") + 1]


# --- end to end: cached fixture → MP4, no network -------------------------------------------


@pytest.fixture
def chromium():
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as e:
        pytest.skip(f"Chromium not available: {e}")


@needs_ffmpeg
def test_end_to_end_from_cached_fixture(tmp_path, chromium, monkeypatch):
    from conftest import FakeTTS

    from threads2video import pipeline
    from threads2video.config import AppConfig

    meta = json.loads((FIXTURE / "meta.json").read_text())
    raw = tmp_path / "cache" / "threads" / "SAMPLE001" / "raw"
    raw.mkdir(parents=True)
    for f in FIXTURE.glob("0*.json"):
        shutil.copy(f, raw / f.name)

    cfg = AppConfig(cache_dir=tmp_path / "cache", work_dir=tmp_path / "work", output_dir=tmp_path / "out")
    cfg.selection.top_comments = 2
    cfg.tts.provider, cfg.tts.fallback = "piper", []
    cfg.card.identity, cfg.card.show_media = "blur", False  # no avatar/image downloads
    cfg.video.preset, cfg.video.subtitles = "ultrafast", True
    monkeypatch.setattr("threads2video.tts.manager.default_providers",
                        lambda _cfg: {"piper": FakeTTS("piper", seconds_per_char=0.004)})

    stages = []
    res = pipeline.run_job(meta["url"], cfg, from_cache=True, progress=lambda st, f, m: stages.append(st))
    assert res.video and res.video.exists()
    assert [s for s in dict.fromkeys(stages)] == ["fetch", "tts", "render", "compose"]

    expected = build_timeline([s.duration for s in res.segments], lead_in=cfg.video.lead_in_s,
                              pre_roll=cfg.video.pre_roll_s, gap=cfg.video.gap_s, tail=cfg.video.tail_s).total
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height:format=duration",
         "-of", "json", str(res.video)]))
    codecs = {s["codec_name"] for s in probe["streams"]}
    video = next(s for s in probe["streams"] if s["codec_name"] == "h264")
    assert codecs == {"h264", "aac"}
    assert (video["width"], video["height"]) == (1080, 1920)
    assert float(probe["format"]["duration"]) == pytest.approx(expected, abs=0.1)
    assert len(res.segments) == 3 and all(Path(s.card).exists() for s in res.segments)


@needs_ffmpeg
def test_encode_progress_and_cancel(tmp_path):
    from threads2video.ffmpeg import FFmpegCancelled, find_ffmpeg

    ff = find_ffmpeg()
    args = ["-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=600", "-c:v", "libx264", "-preset", "medium",
            str(tmp_path / "x.mp4")]
    seen = []
    with pytest.raises(FFmpegCancelled):
        ff.run_progress(args, total=600, progress=seen.append, cancel=lambda: len(seen) >= 1)
    assert seen and 0 <= seen[0] < 1

    done = []
    ff.run_progress(["-f", "lavfi", "-i", "testsrc2=s=160x120:r=10:d=1", str(tmp_path / "y.mp4")], total=1,
                    progress=done.append)
    assert done[-1] == 1.0


def test_reaction_sound_after_speech(tmp_path):
    from threads2video.compose.audio import build_sfx, read_pcm, write_pcm

    snd = tmp_path / "laugh.wav"
    write_pcm(snd, tone(0.2))
    tl = build_timeline([1.0, 1.0], lead_in=0.5, pre_roll=0.25, gap=0.5, tail=0.5, pauses=[1.0, 0.0])
    track = read_pcm(build_sfx([snd, None], tl, tmp_path / "r.wav", after_speech=0.1))
    first = np.flatnonzero(np.abs(track) > 0.01)[0] / SAMPLE_RATE
    assert first == pytest.approx(tl.entries[0].speech_end + 0.1, abs=0.002)


@needs_ffmpeg
def test_end_to_end_with_ai_speech_and_reaction(tmp_path, chromium, monkeypatch):
    from conftest import FakeTTS

    from threads2video import pipeline
    from threads2video.config import AppConfig

    meta = json.loads((FIXTURE / "meta.json").read_text())
    raw = tmp_path / "cache" / "threads" / "SAMPLE001" / "raw"
    raw.mkdir(parents=True)
    for f in FIXTURE.glob("0*.json"):
        shutil.copy(f, raw / f.name)
    cfg = AppConfig(cache_dir=tmp_path / "cache", work_dir=tmp_path / "work", output_dir=tmp_path / "out")
    cfg.selection.top_comments = 2
    cfg.tts.provider, cfg.tts.fallback = "piper", []
    cfg.card.identity, cfg.card.show_media = "blur", False
    cfg.video.preset = "ultrafast"
    cfg.translate.api_key = "test-key"
    cfg.ai.speech_text = cfg.ai.reactions = True
    cfg.ai.use_images = False

    class FakeGemini:
        model = "fake"

        def __init__(self, *a, **kw):
            pass

        def generate_json(self, parts, schema, temperature=0.3):
            n = parts[0]["text"].count('"i":')
            return [{"i": i, "speech": "Câu đọc thử đã viết đầy đủ." if i == 1 else "", "reaction": "laugh" if i == 1
                     else "none"} for i in range(n)]

    tts = FakeTTS("piper", seconds_per_char=0.004)
    monkeypatch.setattr("threads2video.tts.manager.default_providers", lambda _cfg: {"piper": tts})
    monkeypatch.setattr("threads2video.enrich.GeminiClient", FakeGemini)
    stages = []
    res = pipeline.run_job(meta["url"], cfg, from_cache=True, progress=lambda st, f, m: stages.append(st))

    assert "ai" in stages
    assert res.segments[1].speech == "Câu đọc thử đã viết đầy đủ." and res.segments[1].reaction == "laugh"
    assert any(text == "Câu đọc thử đã viết đầy đủ." for text, _ in tts.calls)  # the rewritten text is read
    assert (res.work_dir / "compose" / "reactions.wav").exists()
    expected = build_timeline([s.duration for s in res.segments], lead_in=cfg.video.lead_in_s,
                              pre_roll=cfg.video.pre_roll_s, gap=cfg.video.gap_s, tail=cfg.video.tail_s,
                              pauses=[cfg.video.reaction_pause_s if s.reaction else 0 for s in res.segments]).total
    duration = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                              "-of", "csv=p=0", str(res.video)]))
    assert duration == pytest.approx(expected, abs=0.1)
