"""Core download logic wrapping yt-dlp.

Handles format/quality selection, output path templating, progress reporting,
and per-video success/failure state transitions in the manifest. Transient
failures are retried with backoff; permanent failures (private, deleted,
region-blocked) are marked so they are not retried indefinitely.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional

import yt_dlp
from yt_dlp.utils import DownloadError

from .manifest import BaseManifest
from .utils.organize import build_channel_dir, output_template, safe_output_path
from .utils.rate_limit import RateLimiter

# Substrings (lowercased) that indicate a failure we should NOT retry.
# NOTE: keep these specific. "requested format is not available" is deliberately
# excluded: it is a --quality config error (or a transient format-list issue),
# not a content-unavailability, so it must NOT permanently blacklist an
# otherwise-fine video — the user can fix the quality and re-run.
PERMANENT_MARKERS = (
    "private video",
    "video unavailable",
    "this video is not available",
    "video is not available",
    "members-only",
    "members only",
    "removed by the user",
    "deleted video",
    "copyright",
    "login required",
    "sign in to confirm",
    "age-restricted",
    "this age-restricted",
)

# Substrings indicating a transient/retryable failure.
TRANSIENT_MARKERS = (
    "429",
    "http error 503",
    "http error 500",
    "http error 502",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporary",
    "try again",
    "rate",
    "throttl",
    "network is unreachable",
    "name or service not known",
)


def classify_error(message: str) -> str:
    """Classify an error message as 'permanent', 'transient', or 'unknown'."""
    m = (message or "").lower()
    for marker in PERMANENT_MARKERS:
        if marker in m:
            return "permanent"
    for marker in TRANSIENT_MARKERS:
        if marker in m:
            return "transient"
    return "unknown"


class DownloadReporter:
    """No-op reporter; the CLI substitutes a rich-based implementation."""

    def video_start(self, title: str) -> None:
        pass

    def video_progress(self, data: Dict[str, Any]) -> None:
        pass

    def video_finish(self) -> None:
        pass

    def stop(self) -> None:
        pass


class Downloader:
    def __init__(
        self,
        output_dir: str,
        target_key: str,
        quality: str = "best",
        audio_only: bool = False,
        write_thumbnail: bool = False,
        write_description: bool = False,
        write_subs: bool = False,
        cookies: Optional[str] = None,
        rate_limiter: Optional[RateLimiter] = None,
        max_retries: int = 3,
        after: Optional[str] = None,
        before: Optional[str] = None,
        proxy: Optional[str] = None,
        cookies_from_browser: Optional[str] = None,
        quiet: bool = False,
        verbose: bool = False,
        template: Optional[str] = None,
    ):
        if cookies and cookies_from_browser:
            raise ValueError(
                "Cannot use both a cookies file and --cookies-from-browser."
            )
        self.output_dir = output_dir
        self.target_key = target_key
        self.quality = quality
        self.audio_only = audio_only
        self.write_thumbnail = write_thumbnail
        self.write_description = write_description
        self.write_subs = write_subs
        self.cookies = cookies
        self.rate_limiter = rate_limiter or RateLimiter()
        self.max_retries = max(1, max_retries)
        self.after = after
        self.before = before
        self.proxy = proxy
        self.cookies_from_browser = cookies_from_browser
        self.quiet = quiet
        self.verbose = verbose
        self.template = template
        self.channel_dir = build_channel_dir(output_dir, target_key)

    # --- format selection --------------------------------------------------
    def format_selector(self) -> Optional[str]:
        """Return the yt-dlp format string, or None to let yt-dlp auto-select.

        Returning None (the default 'best' case) is deliberate: yt-dlp's default
        behavior merges the best available video+audio streams and works even in
        environments without a JavaScript runtime (deno/node). Forcing an explicit
        'best' pre-merged format can fail there with "Requested format is not
        available". Explicit qualities still use a selector and may require a JS
        runtime for full format lists.
        """
        if self.audio_only:
            return "bestaudio/best"
        q = (self.quality or "best").lower().strip()
        if q == "best":
            return None
        if q == "worst":
            return "worst"
        m = re.match(r"(\d+)", q)
        if m:
            h = m.group(1)
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        return None

    # --- yt-dlp option construction ---------------------------------------
    def _build_ydl_opts(self, progress_hook: Callable[[Dict[str, Any]], None]) -> Any:
        opts: Any = {
            "outtmpl": self.template or output_template(self.channel_dir),
            "ignoreerrors": False,
            "noplaylist": True,
            "windowsfilenames": True,  # safe on all platforms; required on Windows
            "socket_timeout": 30,  # bound a hung connection so a worker can't block forever
            "progress_hooks": [progress_hook],
        }
        if self.verbose:
            # Surface yt-dlp warnings/debug output.
            opts["verbose"] = True
            opts["no_warnings"] = False
        else:
            # Default (and --quiet): silent operation.
            opts["quiet"] = True
            opts["no_warnings"] = True
        fmt = self.format_selector()
        if fmt is not None:
            opts["format"] = fmt
        if self.cookies:
            opts["cookiefile"] = self.cookies
        if self.after:
            opts["dateafter"] = self.after
        if self.before:
            opts["datebefore"] = self.before
        if self.proxy:
            opts["proxy"] = self.proxy
        if self.cookies_from_browser:
            opts["cookiesfrombrowser"] = [self.cookies_from_browser]

        postprocessors: List[Dict[str, Any]] = []
        if self.audio_only:
            postprocessors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            )
        if self.write_thumbnail:
            postprocessors.append({"key": "ThumbnailImages", "all_thumbnails": False})
        if self.write_subs:
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = ["en", "all"]
        if self.write_description:
            opts["writedescription"] = True
        if postprocessors:
            opts["postprocessors"] = postprocessors
        return opts

    # --- single-video download --------------------------------------------
    def download(
        self,
        video: Dict[str, Any],
        manifest: BaseManifest,
        reporter: Optional[DownloadReporter] = None,
    ) -> Dict[str, Any]:
        reporter = reporter or DownloadReporter()
        video_id = video["video_id"]
        title = video.get("title") or video_id
        # Prefer the per-video URL from the resolver (this is the genuine,
        # platform-specific media URL for YouTube/Instagram/JioHotstar). The
        # old synthetic YouTuBe watch URL is only a fallback for legacy manifest
        # entries that stored no URL. yt-dlp resolves these URLs reliably.
        url = video.get("url") or f"https://www.youtube.com/watch?v={video_id}"

        reporter.video_start(title)
        manifest.mark_downloading(video_id)

        last_error: Optional[str] = None
        for attempt in range(1, self.max_retries + 1):
            captured: Dict[str, Any] = {}

            def hook(data: Dict[str, Any]) -> None:
                reporter.video_progress(data)
                # The download 'finished' event reports the output filename; keep
                # it as a fallback in case the postprocessor hook never fires.
                if data.get("status") == "finished":
                    fn = data.get("filename") or data.get("info_dict", {}).get("filepath")
                    if fn:
                        captured["path"] = fn

            def pp_hook(data: Dict[str, Any]) -> None:
                # Postprocessor 'finished' reports the final (post-merge) path.
                if data.get("status") == "finished":
                    fp = (data.get("info") or {}).get("filepath")
                    if fp:
                        captured["path"] = fp

            opts = self._build_ydl_opts(hook)
            opts["postprocessor_hooks"] = [pp_hook]
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    # Resolve the final on-disk path. yt-dlp does not reliably
                    # populate info['filepath'] for merged outputs, but
                    # prepare_filename() resolves the outtmpl against the fully
                    # extracted info dict (which has the final ext).
                    path = None
                    if isinstance(info, dict):
                        try:
                            path = ydl.prepare_filename(info)
                        except Exception:
                            path = None
                    path = (
                        path
                        or captured.get("path")
                        or (info.get("filepath") if isinstance(info, dict) else None)
                    )
                    # If we still have no concrete file path (or only fell back
                    # to the directory), treat the download as failed rather than
                    # recording the directory itself as a "complete" file.
                    if not path or path == self.channel_dir:
                        manifest.mark_failed(
                            video_id,
                            "could not determine output file path",
                            permanent=False,
                        )
                        reporter.video_finish()
                        return {
                            "video_id": video_id,
                            "status": "failed",
                            "error": "could not determine output file path",
                        }
                # Windows path-length guard: if the resolved path exceeds the
                # legacy MAX_PATH limit, rename to a truncated, collision-safe
                # path. Non-Windows is unaffected.
                if os.name == "nt" and len(path) > 259:
                    ext = os.path.splitext(path)[1] or ""
                    safe = safe_output_path(
                        self.channel_dir,
                        info.get("upload_date") if isinstance(info, dict) else None,
                        info.get("title", video_id) if isinstance(info, dict) else video_id,
                        ext,
                    )
                    try:
                        os.rename(path, safe)
                        path = safe
                    except OSError:
                        pass
                manifest.mark_complete(video_id, path)
                reporter.video_finish()
                return {
                    "video_id": video_id,
                    "status": "complete",
                    "file_path": path,
                }
            except DownloadError as e:
                last_error = str(e)
                kind = classify_error(last_error)
                if kind == "permanent":
                    manifest.mark_failed(video_id, last_error, permanent=True)
                    reporter.video_finish()
                    return {
                        "video_id": video_id,
                        "status": "failed",
                        "error": last_error,
                        "permanent": True,
                    }
                if attempt < self.max_retries:
                    self.rate_limiter.backoff(attempt)
                else:
                    manifest.mark_failed(video_id, last_error, permanent=False)
                    reporter.video_finish()
                    return {
                        "video_id": video_id,
                        "status": "failed",
                        "error": last_error,
                        "permanent": False,
                    }
            except Exception as e:  # noqa: BLE001 - surface as a failed download
                last_error = str(e)
                if attempt < self.max_retries:
                    self.rate_limiter.backoff(attempt)
                else:
                    manifest.mark_failed(video_id, last_error, permanent=False)
                    reporter.video_finish()
                    return {
                        "video_id": video_id,
                        "status": "failed",
                        "error": last_error,
                        "permanent": False,
                    }

        reporter.video_finish()
        return {"video_id": video_id, "status": "failed", "error": last_error}
