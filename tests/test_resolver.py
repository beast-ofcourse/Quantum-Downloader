"""Tests for channel/playlist URL normalization and resolution."""

from unittest.mock import MagicMock, patch

import pytest
from yt_dlp.utils import DownloadError

from ytchannel.resolver import (
    ResolutionError,
    classify_url,
    normalize_channel_url,
    normalize_playlist_url,
    resolve_channel,
    resolve_playlist,
    resolve_single_video,
    resolve_target,
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


# --- platform classification -------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.youtube.com/@handle/videos", "youtube"),
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://www.instagram.com/p/ABC123/", "instagram"),
        ("https://www.instagram.com/reel/ABC123/", "instagram"),
        ("https://www.instagram.com/tv/ABC123/", "instagram"),
        ("https://www.hotstar.com/in/movies/foo/123", "hotstar"),
        ("https://in.hotstar.com/tv/bar/456", "hotstar"),
    ],
)
def test_classify_url(raw, expected):
    assert classify_url(raw) == expected


def test_classify_url_rejects_unsupported():
    with pytest.raises(ValueError):
        classify_url("https://example.com/p/abc")
    with pytest.raises(ValueError):
        classify_url("https://tiktok.com/@user/video/1")
    with pytest.raises(ValueError):
        classify_url("")


# --- single-video resolution (mocked yt-dlp) ---------------------------------
def test_resolve_single_video_youtube_shape():
    info = {
        "id": "vid1",
        "title": "My Clip",
        "webpage_url": "https://www.youtube.com/watch?v=vid1",
        "upload_date": "20240101",
        "duration": 42,
    }
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_single_video("https://www.youtube.com/watch?v=vid1")
    assert result["target_type"] == "video"
    assert result["platform"] == "youtube"
    assert len(result["videos"]) == 1
    v = result["videos"][0]
    assert v["video_id"] == "youtube-vid1"
    assert v["title"] == "My Clip"
    assert v["url"] == "https://www.youtube.com/watch?v=vid1"


def test_resolve_single_video_instagram_shape():
    info = {
        "id": "ig1",
        "title": "IG Post",
        "webpage_url": "https://www.instagram.com/p/ig1/",
    }
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_single_video("https://www.instagram.com/p/ig1/")
    assert result["target_type"] == "video"
    assert result["platform"] == "instagram"
    assert result["videos"][0]["video_id"] == "instagram-ig1"
    assert result["videos"][0]["url"] == "https://www.instagram.com/p/ig1/"


def test_resolve_single_video_hotstar_shape():
    info = {
        "id": "hs1",
        "title": "Hotstar Movie",
        "webpage_url": "https://www.hotstar.com/in/movies/x/1",
    }
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_single_video("https://www.hotstar.com/in/movies/x/1")
    assert result["platform"] == "hotstar"
    assert result["videos"][0]["video_id"] == "hotstar-hs1"


def test_resolve_single_video_extract_error_raises():
    with patch(
        "ytchannel.resolver.yt_dlp.YoutubeDL",
        side_effect=DownloadError("boom"),
    ):
        with pytest.raises(ResolutionError):
            resolve_single_video("https://www.instagram.com/p/ig1/")


def test_resolve_single_video_playlist_page_drills_to_entry():
    # A watch URL that resolves to a list: take the first concrete entry.
    info = {
        "entries": [
            {"id": "vid1", "title": "V1", "webpage_url": "https://www.youtube.com/watch?v=vid1"},
            {"id": "vid2", "title": "V2", "webpage_url": "https://www.youtube.com/watch?v=vid2"},
        ]
    }
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_single_video("https://www.youtube.com/watch?v=vid1&list=PLx")
    assert len(result["videos"]) == 1
    assert result["videos"][0]["video_id"] == "youtube-vid1"


# --- unified resolve_target ---------------------------------------------------
def test_resolve_target_youtube_watch_is_single_video():
    info = {"id": "vid1", "title": "Clip", "webpage_url": "https://www.youtube.com/watch?v=vid1"}
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_target("https://www.youtube.com/watch?v=vid1")
    assert result["target_type"] == "video"


def test_resolve_target_youtu_be_is_single_video():
    info = {"id": "vid1", "title": "Clip", "webpage_url": "https://youtu.be/vid1"}
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_target("https://youtu.be/vid1")
    assert result["target_type"] == "video"
    assert result["platform"] == "youtube"


def test_resolve_target_watch_with_list_is_playlist():
    # Regression: a watch?v=...&list=PL... URL must resolve to its playlist,
    # not be demoted to a single video (the project documents it as a playlist).
    info = {"title": "My Playlist", "id": "PLabc", "entries": [{"id": "v1"}]}
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_target("https://www.youtube.com/watch?v=vid1&list=PLabc")
    assert result["target_type"] == "playlist"
    assert result["platform"] == "youtube"


def test_resolve_target_instagram_delegates_to_single_video():
    info = {"id": "ig1", "title": "IG", "webpage_url": "https://www.instagram.com/p/ig1/"}
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_target("https://www.instagram.com/p/ig1/")
    assert result["platform"] == "instagram"


def test_resolve_target_hotstar_delegates_to_single_video():
    info = {"id": "hs1", "title": "HS", "webpage_url": "https://www.hotstar.com/in/movies/x/1"}
    with patch("ytchannel.resolver.yt_dlp.YoutubeDL", return_value=_mock_ydl(info)):
        result = resolve_target("https://www.hotstar.com/in/movies/x/1")
    assert result["platform"] == "hotstar"


def test_resolve_target_rejects_unsupported_host():
    with pytest.raises(ValueError):
        resolve_target("https://example.com/p/abc")
