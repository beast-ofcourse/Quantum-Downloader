"""Tests for the downloader: format selection, error classification, and
manifest state transitions (using a mocked yt-dlp so no network is needed)."""

from unittest.mock import MagicMock, patch

from yt_dlp.utils import DownloadError

from ytchannel.downloader import Downloader, classify_error
from ytchannel.manifest import Manifest
from ytchannel.utils.rate_limit import RateLimiter


def test_format_selector_quality_and_audio():
    # Default 'best' omits the format option entirely (None) for max compatibility.
    assert Downloader("/o", "c", quality="best").format_selector() is None
    assert Downloader("/o", "c", quality="worst").format_selector() == "worst"
    assert Downloader("/o", "c", quality="1080p").format_selector() == (
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    )
    assert Downloader("/o", "c", quality="720").format_selector() == (
        "bestvideo[height<=720]+bestaudio/best[height<=720]"
    )
    assert Downloader("/o", "c", audio_only=True).format_selector() == "bestaudio/best"


def test_downloader_uses_target_key_for_dir():
    # The second positional arg is the stable storage key (channel/playlist id).
    d = Downloader("/o", "CoolPlaylist")
    assert "CoolPlaylist" in d.channel_dir


def test_classify_error():
    assert classify_error("This video is private video") == "permanent"
    assert classify_error("Video unavailable in your country") == "permanent"
    assert classify_error("HTTP Error 429: too many requests") == "transient"
    assert classify_error("Connection reset by peer") == "transient"
    assert classify_error("some unexpected thing") == "unknown"


def _make_manifest(tmp_path, vid="v1"):
    m = Manifest(str(tmp_path / "m.json"))
    m.reconcile([{"video_id": vid, "title": "V1", "url": "http://x"}], "C")
    return m


def _fake_ydl(extract_info_return=None, side_effect=None):
    fake_ydl = MagicMock()
    fake_ctx = MagicMock()
    if side_effect is not None:
        fake_ctx.extract_info.side_effect = side_effect
    else:
        fake_ctx.extract_info.return_value = extract_info_return
        # prepare_filename resolves the final on-disk path from the info dict.
        if isinstance(extract_info_return, dict):
            fake_ctx.prepare_filename.return_value = extract_info_return.get(
                "filepath", "/dl/C/out.mp4"
            )
    fake_ydl.__enter__.return_value = fake_ctx
    return fake_ydl


def test_download_marks_complete(tmp_path):
    m = _make_manifest(tmp_path)
    fake = _fake_ydl(extract_info_return={"filepath": "/dl/C/20230101_V1.mp4"})
    with patch("ytchannel.downloader.yt_dlp.YoutubeDL", return_value=fake):
        d = Downloader("/dl", "C", rate_limiter=RateLimiter(base_delay=0))
        outcome = d.download(m.entries["v1"], m)
    assert outcome["status"] == "complete"
    assert m.is_complete("v1")
    assert m.entries["v1"]["file_path"] == "/dl/C/20230101_V1.mp4"


def test_download_permanent_failure_not_retried(tmp_path):
    m = _make_manifest(tmp_path)
    fake = _fake_ydl(side_effect=DownloadError("This video is private video"))
    with patch("ytchannel.downloader.yt_dlp.YoutubeDL", return_value=fake):
        d = Downloader("/dl", "C", max_retries=3, rate_limiter=RateLimiter(base_delay=0))
        outcome = d.download(m.entries["v1"], m)
    assert outcome["status"] == "failed"
    assert outcome.get("permanent") is True
    assert "v1" not in m.get_pending()


def test_download_transient_retries_then_fails(tmp_path):
    m = _make_manifest(tmp_path)
    fake = _fake_ydl(side_effect=DownloadError("HTTP Error 429: too many"))
    with patch("ytchannel.downloader.yt_dlp.YoutubeDL", return_value=fake):
        d = Downloader("/dl", "C", max_retries=2, rate_limiter=RateLimiter(base_delay=0))
        outcome = d.download(m.entries["v1"], m)
    assert outcome["status"] == "failed"
    assert outcome.get("permanent") is False
    assert "v1" in m.get_pending()  # eligible for retry on next run
    assert m.entries["v1"]["attempts"] == 1


def test_ydl_opts_date_filter():
    d = Downloader("/o", "c", after="20240101", before="20241231")
    opts = d._build_ydl_opts(lambda data: None)
    assert opts.get("dateafter") == "20240101" and opts.get("datebefore") == "20241231"


def test_ydl_opts_proxy():
    d = Downloader("/o", "c", proxy="http://h:1")
    opts = d._build_ydl_opts(lambda data: None)
    assert opts.get("proxy") == "http://h:1"


def test_ydl_opts_cookies_from_browser():
    d = Downloader("/o", "c", cookies_from_browser="chrome")
    opts = d._build_ydl_opts(lambda data: None)
    assert opts.get("cookiesfrombrowser") == ["chrome"]


def test_cookies_and_browser_conflict():
    import pytest

    with pytest.raises(ValueError):
        Downloader("/o", "c", cookies="x.txt", cookies_from_browser="chrome")


def test_ydl_opts_verbose_flag():
    opts = Downloader("/o", "c", verbose=True)._build_ydl_opts(lambda d: None)
    assert opts.get("verbose") is True
    assert opts.get("no_warnings") is False


def test_ydl_opts_quiet_default():
    # Default (and --quiet) keeps the silent behavior.
    opts = Downloader("/o", "c")._build_ydl_opts(lambda d: None)
    assert opts.get("quiet") is True
    assert opts.get("no_warnings") is True


def test_ydl_opts_template_override():
    opts = Downloader("/o", "c", template="%(title)s.%(ext)s")._build_ydl_opts(
        lambda d: None
    )
    assert opts["outtmpl"] == "%(title)s.%(ext)s"
