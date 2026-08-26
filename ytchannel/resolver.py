"""Channel URL resolution and flat playlist extraction.

Turns a channel URL in any common form into a canonical URL and extracts the
full list of video IDs + basic metadata *without* downloading anything, using
yt-dlp's flat playlist extraction (fast, no per-video requests).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

# Known channel "tab" segments. We want the /videos tab so extraction targets
# the upload playlist rather than e.g. /shorts or /about.
_TAB_SEGMENTS = {
    "videos",
    "shorts",
    "streams",
    "featured",
    "about",
    "playlists",
    "community",
    "live",
}


class ResolutionError(Exception):
    """Raised when a channel URL cannot be resolved to a video playlist."""


def normalize_channel_url(url: str) -> str:
    """Normalize a channel URL to its /videos tab form.

    Accepts:
      - https://www.youtube.com/@handle
      - https://www.youtube.com/c/name
      - https://www.youtube.com/user/name
      - https://www.youtube.com/channel/UCxxxx
      - bare forms like '@handle' or 'youtube.com/c/name'
    and returns a canonical https URL ending in '/videos'.
    """
    if not url or not url.strip():
        raise ValueError("Empty channel URL")
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {url}")
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        raise ValueError(
            "youtu.be short links point to a single video, not a channel. "
            "Provide a channel URL such as https://www.youtube.com/@handle/videos"
        )
    if "youtube.com" not in host and "youtu.be" not in host:
        raise ValueError(f"Not a YouTube URL: {url}")

    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        raise ValueError(f"URL does not point to a channel: {url}")

    last = segments[-1]
    if last in _TAB_SEGMENTS and last != "videos":
        segments[-1] = "videos"
    elif last != "videos":
        segments.append("videos")

    new_path = "/" + "/".join(segments)
    return urlunparse(parsed._replace(path=new_path))


def _extract_videos(info: Any) -> List[Dict[str, Any]]:
    entries = info.get("entries")
    videos: List[Dict[str, Any]] = []
    if not entries:
        return videos
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        videos.append(
            {
                "video_id": vid,
                "title": e.get("title"),
                "url": e.get("url")
                or f"https://www.youtube.com/watch?v={vid}",
                "upload_date": e.get("upload_date"),  # often None in flat mode
                "duration": e.get("duration"),
                "view_count": e.get("view_count"),
            }
        )
    return videos


def resolve_channel(url: str, quiet: bool = True) -> Dict[str, Any]:
    """Resolve a channel URL into a dict with channel metadata and a video list.

    Returns:
        {
          "channel_name": str,
          "channel_id": str | None,
          "url": str,            # normalized
          "videos": List[Dict],  # each with video_id/title/url/upload_date/...
        }
    """
    norm = normalize_channel_url(url)
    ydl_opts: Any = {
        "extract_flat": True,
        "quiet": quiet,
        "no_warnings": quiet,
        "ignoreerrors": True,  # skip individual bad entries instead of aborting
        "skip_download": True,
        "geo_bypass": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(norm, download=False)
    except DownloadError as e:
        raise ResolutionError(f"Failed to resolve channel: {e}") from e
    except ExtractorError as e:
        raise ResolutionError(f"Failed to resolve channel: {e}") from e

    if not info:
        raise ResolutionError(f"Could not resolve any data from URL: {norm}")

    entries = info.get("entries")
    if entries is None:
        # Got a single video (or non-playlist). We need a channel/playlist.
        raise ResolutionError(
            "URL did not resolve to a channel or playlist (got a single video?). "
            "Provide a channel URL such as https://www.youtube.com/@handle/videos"
        )

    videos = _extract_videos(info)
    if not videos:
        raise ResolutionError(
            "No videos found for this channel. It may be empty, private, or "
            "unavailable in your region."
        )

    channel_name = (
        info.get("channel")
        or info.get("uploader")
        or info.get("title")
        or "channel"
    )
    return {
        "channel_name": channel_name,
        "channel_id": info.get("channel_id") or info.get("id"),
        "url": norm,
        "videos": videos,
    }
