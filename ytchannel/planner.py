"""Download planning: filtering and pending/complete accounting.

This module holds the pure, side-effect-free parts of the download
orchestration that used to live in ``cli.py``: date/limit filtering and the
construction of a :class:`DownloadPlan` describing what still needs work.

Keeping this logic here (rather than inline in the command) makes the 0.5
"already complete" accounting easy to test in isolation and keeps ``cli.py``
focused on argument handling and console output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import Config
from .manifest import Manifest


def _filter_dates(
    videos: List[Dict[str, Any]],
    after: Optional[str],
    before: Optional[str],
) -> List[Dict[str, Any]]:
    def parse(d: Optional[str]):
        if not d:
            return None
        try:
            return datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            return None

    after_d = parse(after)
    before_d = parse(before)
    if not after_d and not before_d:
        return videos

    out: List[Dict[str, Any]] = []
    for v in videos:
        ud = v.get("upload_date")
        if not ud:
            out.append(v)  # keep videos whose date is unknown
            continue
        try:
            d = datetime.strptime(ud, "%Y%m%d").date()
        except (ValueError, TypeError):
            out.append(v)
            continue
        if after_d and d < after_d:
            continue
        if before_d and d > before_d:
            continue
        out.append(v)
    return out


def filter_videos(videos: List[Dict[str, Any]], config: Config) -> List[Dict[str, Any]]:
    """Apply date and limit filters to a list of videos.

    The ``--limit`` sign is intentionally NOT validated here; ``cli.py`` keeps
    that check so the error message and exit code stay in one place.
    """
    videos = _filter_dates(videos, config.after, config.before)
    if config.limit is not None:
        videos = videos[: config.limit]
    return videos


@dataclass
class DownloadPlan:
    videos: List[Dict[str, Any]]
    pending: List[str]
    already_complete: int


def plan_downloads(
    videos: List[Dict[str, Any]], manifest: Manifest, config: Config
) -> DownloadPlan:
    """Build a :class:`DownloadPlan` from filtered videos and the manifest.

    ``already_complete`` is counted from the manifest *before* any download
    happens, so the summary reflects the state at start (not the videos we
    just downloaded in this run). This is the locked 0.5 fix: it counts only
    completed videos, excluding permanent failures and not-yet-downloaded.
    """
    videos = filter_videos(videos, config)
    pending_ids = set(manifest.get_pending())
    pending = [v["video_id"] for v in videos if v["video_id"] in pending_ids]
    already_complete = sum(1 for v in videos if manifest.is_complete(v["video_id"]))
    return DownloadPlan(videos=videos, pending=pending, already_complete=already_complete)
