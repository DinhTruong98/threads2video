import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from threads2video.config import CardConfig, SelectionConfig, VideoConfig
from threads2video.fetcher.parser import parse_payloads
from threads2video.render import card_layout, compact_count, render_cards, time_label
from threads2video.selection import select_segments

FIXTURE = Path(__file__).parent / "fixtures" / "thread_sample"


@pytest.mark.parametrize("n, s", [(0, ""), (7, "7"), (999, "999"), (1000, "1K"), (2763, "2.8K"),
                                  (15_400, "15.4K"), (152_000, "152K"), (2_600_000, "2.6M")])
def test_compact_count(n, s):
    assert compact_count(n) == s


def test_time_label():
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    assert time_label(now - timedelta(seconds=20), now) == "1m"
    assert time_label(now - timedelta(minutes=42), now) == "42m"
    assert time_label(now - timedelta(hours=5), now) == "5h"
    assert time_label(now - timedelta(days=3), now) == "3d"
    assert time_label(datetime(2026, 9, 28, tzinfo=timezone.utc), now, "vi") == "28/09/26"
    assert time_label(datetime(2026, 9, 28, tzinfo=timezone.utc), now, "en") == "09/28/26"
    assert time_label(None, now) == ""


def test_card_layout_follows_aspect():
    portrait = card_layout(VideoConfig(aspect="9:16"), CardConfig())
    landscape = card_layout(VideoConfig(aspect="16:9"), CardConfig())
    assert portrait.card_px == round(1080 * 0.88)
    assert landscape.card_px == round(1920 * 0.55)
    # Max card height is a fraction of the frame height, expressed in template CSS px.
    assert portrait.max_height_css * portrait.scale == pytest.approx(1920 * 0.8)
    assert landscape.max_height_css * landscape.scale == pytest.approx(1080 * 0.8)


# --- real rendering (needs Playwright's Chromium) ------------------------------------


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as e:
        pytest.skip(f"Chromium not available: {e}")


def fixture_segments():
    meta = json.loads((FIXTURE / "meta.json").read_text())
    payloads = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(FIXTURE.glob("0*.json"))]
    thread = parse_payloads(payloads, code="SAMPLE001", source_url=meta["url"])
    return select_segments(thread, SelectionConfig(top_comments=3))


# Offline: no avatar/image downloads (identity hidden, media off).
OFFLINE = dict(identity="blur", show_media=False)


@pytest.mark.parametrize("aspect", ["9:16", "16:9"])
def test_render_transparent_pngs(tmp_path, chromium, aspect):
    from PIL import Image

    video, card = VideoConfig(aspect=aspect), CardConfig(**OFFLINE)
    segs = render_cards(fixture_segments(), tmp_path, video=video, card=card, lang="vi")
    layout = card_layout(video, card)
    assert len(segs) == 4
    for s in segs:
        img = Image.open(s.card)
        assert img.mode == "RGBA"
        assert abs(img.width - layout.png_width) <= 1
        assert img.height <= video.size[1]
        assert img.getpixel((0, 0))[3] == 0  # outside the rounded card
        assert img.getpixel((img.width // 2, img.height // 2))[3] == 255  # card body


def test_long_text_is_fitted_into_frame(tmp_path, chromium, caplog):
    from PIL import Image

    seg = fixture_segments()[1]
    seg = seg.model_copy(update={"text": "Một bình luận rất dài về chủ đề này. " * 120})
    video = VideoConfig(aspect="16:9")
    out = render_cards([seg], tmp_path, video=video, card=CardConfig(**OFFLINE), lang="vi")
    assert Image.open(out[0].card).height <= video.size[1]
    assert "cut to" in caplog.text


def test_identity_modes_change_pixels(tmp_path, chromium):
    from PIL import ImageChops, Image

    seg = fixture_segments()[:1]
    imgs = {}
    for mode in ("show", "blur", "black"):
        out = render_cards(seg, tmp_path / mode, video=VideoConfig(), card=CardConfig(identity=mode, show_media=False),
                           lang="vi")
        imgs[mode] = Image.open(out[0].card).convert("RGBA")
    assert ImageChops.difference(imgs["show"], imgs["blur"]).getbbox() is not None
    assert ImageChops.difference(imgs["blur"], imgs["black"]).getbbox() is not None
