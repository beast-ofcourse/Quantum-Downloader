"""URL resolution and flat playlist/standalone extraction for multiple platforms.

Turns a YouTube (channel/playlist/single), Instagram (post/reel/IGTV), or
JioHotstar (movie/show/episode) URL into a canonical target dict with a video
list — *without* downloading anything.

Every resolver returns the same shared shape:
    {
      "target_type": str,      # "channel" | "playlist" | "video"
                               # (single media items from any platform use "video")
      "target_name": str,      # display name for the manifest/folder
      "target_id": str | None, # stable id for the storage key
      "platform": str,         # "youtube" | "instagram" | "hotstar"
      "url": str,              # the URL to drive the downloader with
      "videos": List[Dict],    # each with video_id/title/url/upload_date/...
    }

Platform detection is driven by an exact host allowlist (not substring match)
so look-alike hosts ("youtube.com.evil.com") are rejected — important because
yt-dlp follows redirects and a crafted host could pivot to internal endpoints
(SSRF).
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

# Hosts we are willing to resolve at all. An exact allowlist (not a substring
# match) so look-alikes like "youtube.com.evil.com" or "evil.youtube.com.attack"
# are rejected — important because yt-dlp follows redirects and a crafted host
# could pivot to internal/metadata endpoints (SSRF).
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}

_INSTAGRAM_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "instagr.am",
    "www.instagr.am",
}

_HOTSTAR_HOSTS = {
    "hotstar.com",
    "www.hotstar.com",
    "in.hotstar.com",
    "star.hotstar.com",
}

_ALLOWED_HOSTS = _YOUTUBE_HOSTS | _INSTAGRAM_HOSTS | _HOTSTAR_HOSTS


class ResolutionError(Exception):
    """Raised when a URL cannot be resolved to a video list."""


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def _host_of(url: str) -> str:
    """Return the lowercased host of ``url`` (sans port), or '' if invalid."""
    if not url or not url.strip():
        return ""
    raw = url.strip()
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urlparse(raw)
    netloc = parsed.netloc.lower().split(":")[0]
    return netloc


def classify_url(url: str) -> str:
    """Return the platform for ``url``: 'youtube' | 'instagram' | 'hotstar'.

    Raises ``ValueError`` for an empty URL or a host we don't support.
    """
    if not url or not url.strip():
        raise ValueError("Empty URL")
    host = _host_of(url)
    if not host:
        raise ValueError(f"Invalid URL: {url}")
    if host in _YOUTUBE_HOSTS:
        return "youtube"
    if host in _INSTAGRAM_HOSTS:
        return "instagram"
    if host in _HOTSTAR_HOSTS:
        return "hotstar"
    raise ValueError(
        f"Unsupported host: {host}. "
        f"Supported: YouTube, Instagram, JioHotstar."
    )


# ---------------------------------------------------------------------------
# YouTube — channels / playlists
# ---------------------------------------------------------------------------
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
    host = parsed.netloc.lower().split(":")[0]
    if host not in _YOUTUBE_HOSTS:
        raise ValueError(f"Not a YouTube URL: {url}")
    if host == "youtu.be":
        raise ValueError(
            "youtu.be short links point to a single video, not a channel. "
            "Provide a channel URL such as https://www.youtube.com/@handle/videos"
        )

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
    host = parsed.netloc.lower().split(":")[0]
    if host not in _YOUTUBE_HOSTS:
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
                # Use the per-entry URL when present — this is the genuine,
                # platform-specific media URL (watch?v=, instagram.com/p/...,
                # hotstar.com/.../watch) rather than a synthetic one. The
                # downloader falls back to it directly.
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
        "platform": "youtube",
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
        "platform": "youtube",
        "url": norm,
        "videos": videos,
    }


# ---------------------------------------------------------------------------
# Single video (any supported platform)
# ---------------------------------------------------------------------------
def resolve_single_video(url: str, quiet: bool = True) -> Dict[str, Any]:
    """Resolve a single video (YouTube / Instagram / JioHotstar) into the shape.

    Uses a full (non-flat) extraction so we get a concrete title, upload date,
    and a real per-media URL. Returns ``target_type == "video"`` with a single
    entry in ``videos`` whose ``url`` is the original URL.
    """
    if not url or not url.strip():
        raise ValueError("Empty URL")
    platform = classify_url(url)
    ydl_opts: Any = {
        "quiet": quiet,
        "no_warnings": quiet,
        "ignoreerrors": False,
        "skip_download": True,
        "geo_bypass": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        raise ResolutionError(f"Failed to resolve video: {e}") from e
    except ExtractorError as e:
        raise ResolutionError(f"Failed to resolve video: {e}") from e

    if not isinstance(info, dict):
        raise ResolutionError(f"Could not resolve a video from URL: {url}")

    # A playlist page without a specific video resolves to a list; for a
    # "single video" request we want exactly one media item, so drill into the
    # first entry if a list came back.
    if info.get("entries") and not info.get("_type") == "video":
        entry = next((e for e in info["entries"] if e), None)
        if entry is not None:
            info = entry

    vid = info.get("id")
    if not vid:
        raise ResolutionError(f"Could not resolve a video id from URL: {url}")

    platform_label = {
        "youtube": "YouTube",
        "instagram": "Instagram",
        "hotstar": "JioHotstar",
    }[platform]
    title = info.get("title") or f"{platform_label} video"
    media_url = info.get("webpage_url") or info.get("url") or url

    return {
        "target_type": "video",
        "target_name": title,
        "target_id": f"{platform}-{vid}",
        "platform": platform,
        "url": url,
        "videos": [
            {
                "video_id": f"{platform}-{vid}",
                "title": title,
                "url": media_url,
                "upload_date": info.get("upload_date"),
                "duration": info.get("duration"),
                "view_count": info.get("view_count"),
            }
        ],
    }


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------
def resolve_target(
    url: str,
    playlist: bool = False,
    quiet: bool = True,
) -> Dict[str, Any]:
    """Resolve any supported URL into the shared target shape.

    Auto-detects the platform from the host. For YouTube:

    * ``playlist=True`` forces playlist resolution;
    * a URL with a ``list=`` parameter (incl. ``watch?v=…&list=…``) resolves to
      its playlist (the project documents ``watch?v=…&list=…`` as a playlist);
    * a bare ``/watch`` link or ``youtu.be`` short link resolves to a single
      video;
    * a channel URL resolves to a channel (falling back to a single video if it
      resolves to one).

    Instagram and JioHotstar URLs resolve to a single video.

    Raises ``ValueError`` (unsupported host) or ``ResolutionError`` (could not
    resolve) so callers can surface a clean error.
    """
    if not url or not url.strip():
        raise ValueError("Empty URL")
    platform = classify_url(url)
    host = _host_of(url)
    parsed = urlparse(url)
    has_list = bool(parse_qs(parsed.query).get("list"))

    if platform == "youtube":
        # A list= parameter always wins, even on a /watch link — the project
        # documents `watch?v=…&list=…` as a playlist, so check it before the
        # single-video shortcut below.
        if has_list or playlist:
            return resolve_playlist(url, quiet=quiet)
        # youtu.be short links or /watch urls are single videos.
        if host == "youtu.be" or "/watch" in (parsed.path or ""):
            return resolve_single_video(url, quiet=quiet)
        try:
            return resolve_channel(url, quiet=quiet)
        except ResolutionError:
            # A channel URL that resolves to a single video (e.g. a /watch link
            # with a channel path) falls back to single-video resolution.
            return resolve_single_video(url, quiet=quiet)
    if platform == "instagram":
        return resolve_single_video(url, quiet=quiet)
    if platform == "hotstar":
        return resolve_single_video(url, quiet=quiet)
    raise ValueError(f"Unsupported host: {host}")
