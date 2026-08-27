"""Tests for channel/playlist URL normalization and resolution."""

from unittest.mock import MagicMock, patch

import pytest

from ytchannel.resolver import (
    ResolutionError,
    normalize_channel_url,
    normalize_playlist_url,
    resolve_channel,
    resolve_playlist,
)


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


# --- playlist URL normalization ------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.youtube.com/playlist?list=PLabc", "https://www.youtube.com/playlist?list=PLabc"),
        ("https://www.youtube.com/watch?v=VID&list=PLabc", "https://www.youtube.com/playlist?list=PLabc"),
        ("PLabc1234567", "https://www.youtube.com/playlist?list=PLabc1234567"),
        ("youtube.com/playlist?list=PLabc", "https://www.youtube.com/playlist?list=PLabc"),
    ],
)
def test_normalize_playlist_url(raw, expected):
    assert normalize_playlist_url(raw) == expected


def test_normalize_playlist_url_rejects_channel():
    with pytest.raises(ValueError):
        normalize_playlist_url("https://www.youtube.com/@handle")


def test_normalize_playlist_url_rejects_non_youtube():
    with pytest.raises(ValueError):
        normalize_playlist_url("https://example.com/playlist?list=PLx")


def test_normalize_playlist_url_rejects_bare_word():
    with pytest.raises(ValueError):
        normalize_playlist_url("fireship")


# --- resolve_playlist (mocked yt-dlp) -----------------------------------------
def _mock_ydl(info):
    fake = MagicMock()
    fake.__enter__.return_value.extract_info.return_value = info
    return fake


def test_resolve_playlist_returns_playlist_shape():
    info = {
        "title": "My Playlist",
        "id": "PLabc",
        "entries": [{"id": "v1", "title": "V1"}, {"id": "v2", "title": "V2"}],
    }
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_playlist("https://www.youtube.com/playlist?list=PLabc")
    assert result["target_type"] == "playlist"
    assert result["target_name"] == "My Playlist"
    assert result["target_id"] == "PLabc"
    assert len(result["videos"]) == 2
    assert result["videos"][0]["video_id"] == "v1"


def test_resolve_playlist_empty_raises():
    info = {"title": "P", "id": "PLx", "entries": []}
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        with pytest.raises(ResolutionError):
            resolve_playlist("https://www.youtube.com/playlist?list=PLx")


def test_resolve_playlist_single_video_raises():
    info = {"title": "P", "id": "PLx", "entries": None}
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        with pytest.raises(ResolutionError):
            resolve_playlist("https://www.youtube.com/playlist?list=PLx")


def test_resolve_channel_returns_channel_shape():
    info = {
        "channel": "Cool Chan",
        "channel_id": "UC123",
        "entries": [{"id": "v1", "title": "V1"}],
    }
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_channel("https://www.youtube.com/@cool")
    assert result["target_type"] == "channel"
    assert result["target_name"] == "Cool Chan"
    assert result["target_id"] == "UC123"
    assert result["videos"][0]["video_id"] == "v1"


def test_normalize_channel_url_rejects_lookalike_hosts():
    # A substring host check is bypassable; the exact allowlist must reject these.
    for bad in (
        "http://youtube.com.evil.com/@x/videos",
        "http://evil.youtube.com.attack/@x/videos",
        "http://notyoutube.com/@x/videos",
        "https://youtube.com.evil.com/@x",
    ):
        with pytest.raises(ValueError):
            normalize_channel_url(bad)
    # Legitimate hosts still normalize correctly.
    assert normalize_channel_url("https://www.youtube.com/@handle/videos").endswith("/videos")
    assert normalize_channel_url("https://m.youtube.com/@handle").endswith("/videos")


def test_normalize_playlist_url_rejects_lookalike_hosts():
    for bad in (
        "http://youtube.com.evil.com/playlist?list=PLx",
        "http://evil.youtube.com.attack/playlist?list=PLx",
    ):
        with pytest.raises(ValueError):
            normalize_playlist_url(bad)
