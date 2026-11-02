import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

from threads2video import mcp_server  # noqa: E402
from threads2video.config import AppConfig, save_config  # noqa: E402

URL1 = "https://www.threads.com/@alice/post/AAA111"
URL2 = "https://www.threads.com/@bob/post/BBB222"


@pytest.fixture
def saved(tmp_path):
    """Stand-in for the desktop app's saved settings."""
    cfg = AppConfig(cache_dir=tmp_path / "cache", work_dir=tmp_path / "work", output_dir=tmp_path / "out")
    cfg.selection.top_comments = 7
    cfg.selection.review_before_tts = True
    cfg.translate.api_key = "secret-key"
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    return path


@pytest.fixture
def fake_jobs(monkeypatch, tmp_path):
    runs = []

    def run_job(url, cfg, *, from_cache, progress, cancel):
        runs.append((url, cfg, from_cache))
        progress("tts", 0.5, "TTS 1/2")
        if "BBB" in url:
            raise RuntimeError("post not found")
        video = tmp_path / f"{url[-6:]}.mp4"
        video.write_bytes(b"x")
        seg = SimpleNamespace(voice="vieneu:Hải Đăng", reaction="laugh")
        return SimpleNamespace(video=video, segments=[seg, seg], thread=None)

    monkeypatch.setattr(mcp_server, "run_job", run_job)
    return runs


def call(server, tool, args=None, progress=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args or {}, progress_callback=progress)
    return anyio.run(go)


def test_create_video_uses_saved_settings_plus_overrides(saved, fake_jobs):
    seen = []

    async def on_progress(p, total, message):
        seen.append(message)

    server = mcp_server.build_server(config_path=saved)
    res = call(server, "create_video", {"urls": [URL1], "aspect": "16:9", "ai_reactions": True,
                                        "settings": {"audio.music_volume": 5}}, progress=on_progress)
    assert not res.is_error
    job = res.structured_content
    assert job["status"] == "done" and job["errors"] == {}
    assert job["videos"][0]["path"].endswith("AAA111.mp4") and job["videos"][0]["reactions"] == ["laugh", "laugh"]
    url, cfg, from_cache = fake_jobs[0]
    assert cfg.selection.top_comments == 7  # from the saved settings
    assert (cfg.video.aspect, cfg.ai.reactions, cfg.audio.music_volume) == ("16:9", True, 5)
    assert cfg.selection.review_before_tts is False  # no one to answer the review dialog
    assert from_cache is False


def test_batch_without_waiting_and_failures_per_url(saved, fake_jobs):
    queue = mcp_server.JobQueue()
    server = mcp_server.build_server(queue, config_path=saved)
    job = call(server, "create_video", {"urls": [URL1, URL2, URL1], "wait": False}).structured_content
    assert job["status"] in ("queued", "running", "done")
    queue._pool.shutdown(wait=True)
    done = call(server, "get_job", {"job_id": job["job_id"]}).structured_content
    assert done["status"] == "done" and len(done["videos"]) == 1  # duplicate URL dropped
    assert done["errors"] == {URL2: "post not found"}
    assert [j["job_id"] for j in call(server, "list_jobs").structured_content["result"]] == [job["job_id"]]


def test_errors_reach_the_agent(saved, fake_jobs):
    server = mcp_server.build_server(config_path=saved)
    bad_url = call(server, "create_video", {"urls": ["https://example.com/x"]})
    assert bad_url.is_error and "Not a Threads post URL" in bad_url.content[0].text
    bad_setting = call(server, "create_video", {"urls": [URL1], "aspect": "4:3"})
    assert bad_setting.is_error and "Invalid setting" in bad_setting.content[0].text
    unknown = call(server, "create_video", {"urls": [URL1], "settings": {"nope.x": 1}})
    assert unknown.is_error and "Unknown setting" in unknown.content[0].text
    assert call(server, "get_job", {"job_id": "zzz"}).is_error
    assert fake_jobs == []


def test_settings_hide_the_api_key(saved):
    data = call(mcp_server.build_server(config_path=saved), "get_settings").structured_content
    assert data["selection"]["top_comments"] == 7
    assert data["translate"]["api_key"] == "(set)" and "secret-key" not in str(data)


def test_stdio_server_as_an_agent_starts_it(saved):
    # The real launch path: protocol on stdout, logs on stderr must not corrupt it.
    params = StdioServerParameters(command=sys.executable, args=["-m", "threads2video", "--mcp", "-c", str(saved)])

    async def go():
        async with Client(params) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            settings = (await c.call_tool("get_settings", {})).structured_content
            return tools, settings

    tools, settings = anyio.run(go)
    assert {"create_video", "get_job", "list_jobs", "cancel_job", "get_settings", "list_videos"} <= tools
    assert settings["selection"]["top_comments"] == 7
