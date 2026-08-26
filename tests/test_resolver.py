"""Tests for channel URL normalization (Phase 1 requirement: deterministic)."""

import pytest

from ytchannel.resolver import normalize_channel_url


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.youtube.com/@handle", "https://www.youtube.com/@handle/videos"),
        ("https://www.youtube.com/c/name", "https://www.youtube.com/c/name/videos"),
        ("https://www.youtube.com/user/name", "https://www.youtube.com/user/name/videos"),
        ("https://www.youtube.com/channel/UCxxxx", "https://www.youtube.com/channel/UCxxxx/videos"),
        ("https://www.youtube.com/@handle/videos", "https://www.youtube.com/@handle/videos"),
        ("https://www.youtube.com/@handle/shorts", "https://www.youtube.com/@handle/videos"),
        ("https://www.youtube.com/@handle/streams", "https://www.youtube.com/@handle/videos"),
        ("https://www.youtube.com/@handle/about", "https://www.youtube.com/@handle/videos"),
        ("youtube.com/@handle", "https://youtube.com/@handle/videos"),
        ("www.youtube.com/c/name", "https://www.youtube.com/c/name/videos"),
    ],
)
def test_normalize_appends_videos_tab(raw, expected):
    assert normalize_channel_url(raw) == expected


def test_normalize_rejects_youtu_be():
    with pytest.raises(ValueError):
        normalize_channel_url("https://youtu.be/abc123")


def test_normalize_rejects_non_youtube():
    with pytest.raises(ValueError):
        normalize_channel_url("https://example.com/@handle")


def test_normalize_rejects_empty():
    with pytest.raises(ValueError):
        normalize_channel_url("")


def test_normalize_rejects_bare_path():
    with pytest.raises(ValueError):
        normalize_channel_url("https://www.youtube.com/")
