"""Indexing: turn a resolved channel into a structured metadata export."""

from __future__ import annotations

import csv
import json
from typing import Any, Dict, List


def export_json(result: Dict[str, Any], output_path: str) -> None:
    payload = {
        "channel_name": result["channel_name"],
        "channel_id": result.get("channel_id"),
        "url": result["url"],
        "video_count": len(result["videos"]),
        "videos": result["videos"],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def export_csv(result: Dict[str, Any], output_path: str) -> None:
    fields = ["video_id", "title", "url", "upload_date", "duration", "view_count"]
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for v in result["videos"]:
            writer.writerow(v)


def dry_run_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compute a human-readable summary of what a download would entail.

    Note: flat extraction does not include per-video file sizes, so the
    estimated total size is reported as None (unknown) rather than guessed.
    """
    videos: List[Dict[str, Any]] = result["videos"]
    count = len(videos)
    dates = [v["upload_date"] for v in videos if v.get("upload_date")]
    date_range = None
    if dates:
        sorted_dates = sorted(dates)
        date_range = (sorted_dates[0], sorted_dates[-1])
    total_duration = sum((v.get("duration") or 0) for v in videos)
    return {
        "count": count,
        "date_range": date_range,
        "total_duration_seconds": total_duration,
        "estimated_size": None,  # not available from flat metadata
    }
