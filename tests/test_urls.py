import pytest

from threads2video.fetcher.urls import parse_post_url


@pytest.mark.parametrize(
    "url, code, user",
    [
        ("https://www.threads.com/@zuck/post/Dd1MqeVG3vT", "Dd1MqeVG3vT", "zuck"),
        ("https://www.threads.net/@some.user/post/Ab_c-1/?xmt=xyz", "Ab_c-1", "some.user"),
        ("threads.com/@a/post/XYZ", "XYZ", "a"),
        ("https://www.threads.com/t/XYZ123", "XYZ123", None),
    ],
)
def test_parse_post_url(url, code, user):
    ref = parse_post_url(url)
    assert (ref.code, ref.username) == (code, user)
    assert ref.url.startswith("https://www.threads.com/")


@pytest.mark.parametrize("url", ["https://x.com/@a/post/1", "https://www.threads.com/@zuck", "not a url"])
def test_rejects_non_post_urls(url):
    with pytest.raises(ValueError):
        parse_post_url(url)
