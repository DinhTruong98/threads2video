import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from threads2video.config import TTSConfig
from threads2video.models import Author, Segment
from threads2video.tts import TTSError, TTSManager, TTSProvider, Voice, VoiceAssigner, clean_for_speech, wav_duration
from threads2video.tts.base import split_voice_string

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")


@pytest.mark.parametrize(
    "raw, spoken",
    [
        ("Hay quá 😂😂 xem https://x.com/abc nhé", "Hay quá xem nhé."),
        ("Dòng một\nDòng hai!", "Dòng một. Dòng hai!"),
        ("@nguyen_van.a nói đúng #threads", "nguyen van a nói đúng threads."),
        ("Thật sao???!!", "Thật sao?"),
        ("Chờ đã.......", "Chờ đã..."),
        ("👍🏻❤️🔥", ""),
        ("NO ONE WANTS AI. WE WANT CLEAN WATER", "No one wants AI. We want clean water."),
        ("Mình thích AI và USA", "Mình thích AI và USA."),
        ("TP HCM HÔM NAY MƯA TO QUÁ", "TP HCM hôm nay mưa to quá."),
    ],
)
def test_clean_for_speech(raw, spoken):
    assert clean_for_speech(raw) == spoken


def test_voice_string_roundtrip():
    v = Voice("piper", "vi_VN-vivos-x_low", speaker=3, pitch="+6%")
    assert str(v) == "vi_VN-vivos-x_low#3@+6%"
    assert split_voice_string(str(v)) == ("vi_VN-vivos-x_low", 3, "+6%")
    assert str(Voice("edge", "vi-VN-HoaiMyNeural", pitch="+0Hz")) == "vi-VN-HoaiMyNeural"


from conftest import FakeTTS  # noqa: E402


def seg(i, author, op=False, text="Một câu bình luận thử nghiệm."):
    return Segment(id=str(i), kind="post" if i == 0 else "reply", author=Author(id=author, username=author),
                   is_op=op, text=text, text_original=text)


def test_voice_assigner_same_author_same_voice():
    cfg = TTSConfig(voice="v0", op_voice="v1")
    a = VoiceAssigner(cfg, "vi", seed="post1")
    p = FakeTTS(n_voices=6)
    segs = [seg(0, "op", op=True), seg(1, "alice"), seg(2, "bob"), seg(3, "alice"), seg(4, "op", op=True)]
    voices = [a.voice(p, a.slot(s)) for s in segs]
    assert str(voices[0]) == str(voices[4]) == "v1"
    assert voices[1] == voices[3]
    assert voices[1] != voices[2]
    assert all(str(v) != "v1" for v in voices[1:4])  # commenters never get the OP voice


def test_voice_assigner_deterministic_per_seed_and_off_switch():
    p = FakeTTS(n_voices=8)
    segs = [seg(i, f"user{i}") for i in range(1, 6)]

    def run(seed, **kw):
        a = VoiceAssigner(TTSConfig(**kw), "vi", seed=seed)
        return [str(a.voice(p, a.slot(s))) for s in segs]

    assert run("x") == run("x")
    assert run("x") != run("y")
    assert set(run("x", random_voice_per_author=False, voice="v3")) == {"v3"}


@needs_ffmpeg
def test_durations_trim_and_speed(tmp_path):
    from threads2video.ffmpeg import find_ffmpeg

    text = "a" * 20  # 1.0 s of tone with 0.5 s silence each side
    results = {}
    for speed in (1.0, 1.1):
        m = TTSManager(TTSConfig(provider="piper", fallback=[], speed=speed), "vi", find_ffmpeg(),
                       providers={"piper": FakeTTS("piper")})
        segs = m.synthesize_segments([seg(0, "op", op=True, text=text)], tmp_path / str(speed))
        results[speed] = segs[0].duration
        assert segs[0].duration == pytest.approx(wav_duration(Path(segs[0].audio)))
        with wave.open(segs[0].audio) as wf:
            assert (wf.getframerate(), wf.getnchannels()) == (48_000, 1)
    # 1 s of padding trimmed to ~0.05 s lead + ~0.1 s tail (plus detection-window slack).
    assert results[1.0] == pytest.approx(1.15, abs=0.08)
    # Speed is applied after trimming, so the ratio holds up to the trim slack (real speech: <0.5%).
    assert results[1.1] == pytest.approx(results[1.0] / 1.1, abs=0.03)


@needs_ffmpeg
def test_fallback_reuse_and_silence(tmp_path):
    from threads2video.ffmpeg import find_ffmpeg

    broken, good = FakeTTS("capcut", fail=True), FakeTTS("edge")
    cfg = TTSConfig(provider="capcut", fallback=["edge"])
    m = TTSManager(cfg, "vi", find_ffmpeg(), providers={"capcut": broken, "edge": good})
    segs = [seg(0, "op", op=True), seg(1, "alice", text="😂😂")]
    out = m.synthesize_segments(segs, tmp_path)
    assert out[0].voice.startswith("edge:")
    assert out[1].voice == "silence" and out[1].duration == pytest.approx(2.0, abs=0.01)
    assert len(good.calls) == 1

    # Second run with unchanged text and settings reuses the audio.
    m2 = TTSManager(cfg, "vi", find_ffmpeg(), providers={"capcut": broken, "edge": good})
    again = m2.synthesize_segments(segs, tmp_path)
    assert len(good.calls) == 1
    assert [s.duration for s in again] == [s.duration for s in out]

    # Editing the text invalidates that segment only.
    segs[0] = segs[0].model_copy(update={"text": "Đã sửa nội dung."})
    m2.synthesize_segments(segs, tmp_path)
    assert len(good.calls) == 2


@needs_ffmpeg
def test_all_providers_fail(tmp_path):
    from threads2video.ffmpeg import find_ffmpeg

    m = TTSManager(TTSConfig(provider="capcut", fallback=[]), "vi", find_ffmpeg(),
                   providers={"capcut": FakeTTS("capcut", fail=True)})
    with pytest.raises(TTSError, match="All TTS providers failed"):
        m.synthesize_segments([seg(0, "op", op=True)], tmp_path)


def test_unavailable_providers_skipped():
    class Missing(FakeTTS):
        def unavailable_reason(self, lang):
            return "not installed"

    m = TTSManager(TTSConfig(provider="capcut", fallback=["edge"]), "vi", ffmpeg=None,
                   providers={"capcut": Missing("capcut"), "edge": FakeTTS("edge")})
    assert [p.name for p in m.chain()] == ["edge"]
    m = TTSManager(TTSConfig(provider="capcut", fallback=[]), "vi", ffmpeg=None, providers={"capcut": Missing("capcut")})
    with pytest.raises(TTSError, match="No usable TTS provider"):
        m.chain()


class FailsOn(FakeTTS):
    """Fails only for texts containing `word`, like edge-tts refusing some sentences."""

    def __init__(self, name, word):
        super().__init__(name)
        self.word = word

    def synthesize(self, text, voice, out_stem):
        if self.word in text:
            self.calls.append((text, str(voice)))
            raise TTSError("No audio was received")
        return super().synthesize(text, voice, out_stem)


@needs_ffmpeg
def test_one_failing_segment_moves_the_whole_video_to_the_fallback(tmp_path):
    from threads2video.ffmpeg import find_ffmpeg

    edge, vieneu = FailsOn("edge", "Oracle"), FakeTTS("vieneu")
    cfg = TTSConfig(provider="edge", fallback=["vieneu"])
    m = TTSManager(cfg, "vi", find_ffmpeg(), providers={"edge": edge, "vieneu": vieneu})
    segs = [seg(0, "op", op=True), seg(1, "alice"), seg(2, "bob", text="Oracle làm vẫn bình thường mà."),
            seg(3, "op", op=True, text="Tin chuẩn đấy bạn ơi.")]
    out = m.synthesize_segments(segs, tmp_path)
    # Mixed engines would give the OP two different voices; everything is read by the fallback.
    assert all(s.voice.startswith("vieneu:") for s in out)
    assert out[0].voice == out[3].voice
    assert len(vieneu.calls) == 4


def test_op_voice_set_for_another_engine_still_differs_from_default():
    p = FakeTTS(n_voices=4)  # a fallback engine that knows none of the configured voices
    op = seg(0, "op", op=True)
    a = VoiceAssigner(TTSConfig(voice="vi-VN-HoaiMyNeural", op_voice="vi-VN-NamMinhNeural",
                                random_voice_per_author=False), "vi", seed="s")
    assert str(a.voice(p, a.slot(op))) == "v1" and str(a.voice(p, "default")) == "v0"
    # No OP voice configured means "same as default".
    a = VoiceAssigner(TTSConfig(voice="v2", op_voice=None), "vi", seed="s")
    assert str(a.voice(p, a.slot(op))) == "v2"
