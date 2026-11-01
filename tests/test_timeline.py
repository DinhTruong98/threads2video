import pytest

from threads2video.compose.timeline import build_timeline, split_caption_chunks


def test_single_segment():
    tl = build_timeline([2.0], lead_in=0.2, pre_roll=0.15, gap=0.35, tail=0.8)
    (e,) = tl.entries
    assert e.card_in == pytest.approx(0.2)
    assert e.speech_start == pytest.approx(0.35)
    assert e.speech_end == pytest.approx(2.35)
    assert tl.total == pytest.approx(3.15)  # no trailing gap, only the tail
    assert e.card_out == pytest.approx(tl.total)


def test_cards_are_back_to_back():
    durs = [1.5, 3.0, 0.8]
    tl = build_timeline(durs, lead_in=0.2, pre_roll=0.15, gap=0.35, tail=0.8)
    for a, b in zip(tl.entries, tl.entries[1:]):
        assert a.card_out == pytest.approx(b.card_in)
        assert b.card_in == pytest.approx(a.speech_end + 0.35)
    for e, d in zip(tl.entries, durs):
        assert e.speech_end - e.speech_start == pytest.approx(d)
        assert e.card_in < e.speech_start < e.speech_end <= e.card_out
    assert tl.total == pytest.approx(0.2 + sum(durs) + 3 * 0.15 + 2 * 0.35 + 0.8)


def test_zero_padding():
    tl = build_timeline([1.0, 1.0], lead_in=0, pre_roll=0, gap=0, tail=0)
    assert [(e.card_in, e.card_out) for e in tl.entries] == [(0, 1.0), (1.0, 2.0)]
    assert tl.total == 2.0


@pytest.mark.parametrize("durs", [[], [1.0, 0.0], [-1.0]])
def test_invalid(durs):
    with pytest.raises(ValueError):
        build_timeline(durs)


def test_caption_chunks_cover_span_in_order():
    text = "Xin chào các bạn, hôm nay mình sẽ đọc một bài viết rất thú vị trên Threads nhé."
    chunks = split_caption_chunks(text, 1.0, 6.0, max_words=7)
    assert " ".join(c[0] for c in chunks) == text
    assert chunks[0][1] == pytest.approx(1.0) and chunks[-1][2] == pytest.approx(6.0)
    for (_, s1, e1), (_, s2, _) in zip(chunks, chunks[1:]):
        assert e1 == pytest.approx(s2) and s1 < e1
    assert all(len(c[0].split()) <= 7 for c in chunks)
    assert chunks[0][0] == "Xin chào các bạn,"  # breaks after punctuation


def test_caption_chunks_empty():
    assert split_caption_chunks("   ", 0, 1) == []
