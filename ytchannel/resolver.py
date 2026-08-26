"""URL resolution and flat playlist extraction for channels and playlists.

Turns a channel or playlist URL in any common form into a canonical URL and
extracts the full list of video IDs + basic metadata *without* downloading
anything, using yt-dlp's flat playlist extraction (fast, no per-video requests).

Both resolvers return the same shape:
    {
      "target_type": "channel" | "playlist",
      "target_name": str,
      "target_id": str | None,
      "url": str,            # normalized
      "videos": List[Dict],  # each with video_id/title/url/upload_date/...
    }
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse, urlunparse

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
    """Raised when a channel/playlist URL cannot be resolved to a video list."""


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


def normalize_playlist_url(url: str) -> str:
    """Normalize a playlist URL (or bare playlist id) to its canonical form.

    Accepts:
      - https://www.youtube.com/playlist?list=PLxxxx
      - https://www.youtube.com/watch?v=VID&list=PLxxxx   (uses the playlist)
      - a bare playlist id like PLxxxx (prepended with the canonical URL)
    and returns 'https://www.youtube.com/playlist?list=<id>'.
    """
    if not url or not url.strip():
        raise ValueError("Empty playlist URL")
    raw = url.strip()
    # Bare playlist id (no scheme, no slashes) — e.g. "PLxxxx...".
    if re.match(r"^[A-Za-z0-9_-]{10,}$", raw):
        return "https://www.youtube.com/playlist?list=" + raw
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {raw}")
    host = parsed.netloc.lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        raise ValueError(f"Not a YouTube URL: {raw}")
    qs = parse_qs(parsed.query)
    lists = qs.get("list")
    if not lists:
        raise ValueError(
            f"URL does not contain a playlist (list=) parameter: {raw}"
        )
    return "https://www.youtube.com/playlist?list=" + lists[0]


def _list_id_from_url(url: str) -> Any:
    qs = parse_qs(urlparse(url).query)
    lists = qs.get("list")
    return lists[0] if lists else None


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


def _flat_extract(url: str, quiet: bool) -> Any:
    """Run a flat, no-download extraction and return the yt-dlp info dict."""
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
            return ydl.extract_info(url, download=False)
    except DownloadError as e:
        raise ResolutionError(f"Failed to resolve: {e}") from e
    except ExtractorError as e:
        raise ResolutionError(f"Failed to resolve: {e}") from e


def resolve_channel(url: str, quiet: bool = True) -> Dict[str, Any]:
    """Resolve a channel URL into a dict with channel metadata and a video list.

    Returns the shared shape documented in this module's docstring.
    """
    norm = normalize_channel_url(url)
    info = _flat_extract(norm, quiet)
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

    target_name = (
        info.get("channel")
        or info.get("uploader")
        or info.get("title")
        or "channel"
    )
    return {
        "target_type": "channel",
        "target_name": target_name,
        "target_id": info.get("channel_id") or info.get("id"),
        "url": norm,
        "videos": videos,
    }


def resolve_playlist(url: str, quiet: bool = True) -> Dict[str, Any]:
    """Resolve a playlist URL into a dict with playlist metadata and a video list.

    Returns the shared shape documented in this module's docstring, with
    target_type == "playlist".
    """
    norm = normalize_playlist_url(url)
    info = _flat_extract(norm, quiet)
    if not info:
        raise ResolutionError(f"Could not resolve any data from URL: {norm}")

    entries = info.get("entries")
    if entries is None:
        # Got a single video, not a playlist.
        raise ResolutionError(
            "URL did not resolve to a playlist (got a single video?). "
            "Provide a playlist URL with a list= parameter, e.g. "
            "https://www.youtube.com/playlist?list=PLxxxx"
        )

    videos = _extract_videos(info)
    if not videos:
        raise ResolutionError(
            "No videos found in this playlist. It may be empty, private, or "
            "unavailable in your region."
        )

    target_name = info.get("title") or "playlist"
    target_id = info.get("id") or _list_id_from_url(norm)
    return {
        "target_type": "playlist",
        "target_name": target_name,
        "target_id": target_id,
        "url": norm,
        "videos": videos,
    }
