"""Manifest: the source of truth for download state, keyed by video ID.

A manifest is a JSON file mapping each video ID to an entry recording its
download status. Keeping state in a manifest (rather than inferring it from
files on disk) makes the tool resilient to filename changes and lets a crashed
run resume cleanly.

Schema (one entry per video_id):
    {
      "video_id": str,
      "title": str,
      "status": "pending" | "downloading" | "complete" | "failed",
      "file_path": str | null,
      "downloaded_at": str | null,   # ISO-8601 UTC
      "attempts": int,
      "last_error": str | null,
      "permanent": bool              # True for failures that should NOT be retried
    }

Writes are atomic (temp file + os.replace) so a crash mid-write cannot corrupt
the manifest.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

VALID_STATUS = {"pending", "downloading", "complete", "failed"}


class ManifestError(Exception):
    """Raised when a manifest cannot be read or written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Manifest:
    def __init__(self, path: str):
        self.path = path
        self.channel_name: Optional[str] = None
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.load()

    # --- persistence -------------------------------------------------------
    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                raise ManifestError(f"Failed to read manifest {self.path}: {e}")
            if not isinstance(data, dict):
                raise ManifestError(f"Manifest {self.path} is malformed")
            self.channel_name = data.get("channel_name")
            self.entries = data.get("videos", {})
            if not isinstance(self.entries, dict):
                raise ManifestError(f"Manifest {self.path} entries are malformed")

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        data = {"channel_name": self.channel_name, "videos": self.entries}
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise ManifestError(f"Failed to write manifest {self.path}: {e}")

    # --- reconciliation ----------------------------------------------------
    def reconcile(self, videos: List[Dict[str, Any]], channel_name: Optional[str] = None) -> None:
        """Merge a fresh channel index into the manifest.

        - Any entry left as 'downloading' from a previous crashed run is reset
          to 'pending' (it did not finish).
        - New videos are added as 'pending'.
        - Already-'complete' videos are left untouched.
        - Titles are refreshed for existing entries.
        """
        if channel_name:
            self.channel_name = channel_name
        for entry in self.entries.values():
            if entry.get("status") == "downloading":
                entry["status"] = "pending"
        for v in videos:
            vid = v.get("video_id")
            if not vid:
                continue
            if vid not in self.entries:
                self.entries[vid] = {
                    "video_id": vid,
                    "title": v.get("title"),
                    "status": "pending",
                    "file_path": None,
                    "downloaded_at": None,
                    "attempts": 0,
                    "last_error": None,
                    "permanent": False,
                }
            else:
                if v.get("title") is not None:
                    self.entries[vid]["title"] = v["title"]
        self.save()

    # --- state transitions -------------------------------------------------
    def mark_downloading(self, video_id: str) -> None:
        entry = self.entries[video_id]
        entry["status"] = "downloading"
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        self.save()

    def mark_complete(self, video_id: str, file_path: Optional[str]) -> None:
        entry = self.entries[video_id]
        entry["status"] = "complete"
        entry["file_path"] = file_path
        entry["downloaded_at"] = _now()
        entry["last_error"] = None
        entry["permanent"] = False
        self.save()

    def mark_failed(self, video_id: str, error: str, permanent: bool = False) -> None:
        entry = self.entries[video_id]
        entry["status"] = "failed"
        entry["last_error"] = str(error)
        entry["permanent"] = bool(permanent)
        self.save()

    # --- queries -----------------------------------------------------------
    def is_complete(self, video_id: str) -> bool:
        entry = self.entries.get(video_id)
        return entry is not None and entry.get("status") == "complete"

    def get_pending(self) -> List[str]:
        """Return video IDs that still need work.

        Includes 'pending' and previously-'downloading' (reset) entries, plus
        non-permanent 'failed' entries (so transient failures are retried).
        Permanent failures are excluded.
        """
        result: List[str] = []
        for vid, entry in self.entries.items():
            status = entry.get("status")
            if status in ("pending", "downloading"):
                result.append(vid)
            elif status == "failed" and not entry.get("permanent"):
                result.append(vid)
        return result

    def get_failed(self) -> List[str]:
        return [vid for vid, e in self.entries.items() if e.get("status") == "failed"]
