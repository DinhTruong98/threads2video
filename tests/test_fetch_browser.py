from contextlib import contextmanager

import pytest
from playwright.sync_api import Error as PlaywrightError

from threads2video.config import FetchConfig
from threads2video.fetcher import ProfileInUseError, fetch_thread
from threads2video.fetcher import browser


def test_profile_open_in_login_window_fails_fast_with_clear_message(tmp_path, monkeypatch):
    calls = []

    def launch_persistent(pw, profile_dir, **kw):
        calls.append(profile_dir)
        raise PlaywrightError("Failed to create a ProcessSingleton for your profile directory.")

    @contextmanager
    def fake_playwright():
        yield object()

    monkeypatch.setattr(browser.browser_env, "launch_persistent", launch_persistent)
    monkeypatch.setattr(browser.browser_env, "configure", lambda: None)
    monkeypatch.setattr(browser, "sync_playwright", fake_playwright)
    cfg = FetchConfig(profile_dir=tmp_path / "profile", retries=2)
    with pytest.raises(ProfileInUseError, match="login window is still open"):
        fetch_thread("https://www.threads.com/@someone/post/ABC123", cfg, cache_dir=tmp_path)
    assert len(calls) == 1  # not retried
