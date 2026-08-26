"""Manifest: the source of truth for download state, keyed by video ID.

A manifest maps each video ID to an entry recording its download status.
Keeping state in a manifest (rather than inferring it from files on disk)
makes the tool resilient to filename changes and lets a crashed run resume
cleanly.

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

Two backends are supported behind a common interface:

* :class:`JsonManifest` — the original JSON-file backend (atomic temp-file +
  ``os.replace`` writes). This is what ``Manifest(path)`` constructs, so legacy
  and test behavior is preserved exactly.
* :class:`SqliteManifest` — a SQLite backend (stdlib ``sqlite3``) for large
  manifests. The DB lives at a sibling path (``<name>.sqlite``).

Both backends share all query/reconcile/verification logic via
:class:`BaseManifest`, so they behave identically. Construct a manifest through
the :meth:`Manifest.open` factory to pick a backend (``auto`` | ``json`` |
``sqlite``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .utils.organize import build_channel_dir

VALID_STATUS = {"pending", "downloading", "complete", "failed"}

# Media extensions we treat as downloadable content. Sidecar files (thumbnails,
# .description, .json metadata, etc.) are deliberately excluded so they are not
# mistaken for orphaned media during verification.
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi",
    ".mp3", ".m4a", ".ogg", ".wav", ".flac", ".aac", ".opus",
}

# Serializes writes across threads (Phase 4.1 concurrency). Both backends wrap
# their save() body in this lock so concurrent downloads cannot interleave
# writes to the same manifest.
_SAVE_LOCK = threading.Lock()


class ManifestError(Exception):
    """Raised when a manifest cannot be read or written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaseManifest(ABC):
    """Shared manifest interface and backend-agnostic logic.

    Subclasses implement :meth:`load` and :meth:`save`; everything else
    (reconcile, state transitions, queries, file verification) is implemented
    once here so both backends behave identically.
    """

    def __init__(self, path: str):
        self.path = path
        self.channel_name: Optional[str] = None
        self.entries: Dict[str, Dict[str, Any]] = {}

    # --- persistence (backend-specific) -----------------------------------
    @abstractmethod
    def load(self) -> None:
        ...

    @abstractmethod
    def save(self) -> None:
        ...

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

    # --- verification ------------------------------------------------------
    def check_files(self, output_dir: str) -> Dict[str, Any]:
        """Verify which 'complete' entries still have their files on disk.

        Returns a report with three buckets:
          - complete_present: complete entries whose file exists
          - complete_missing: complete entries whose file is gone
          - orphan_on_disk: media files on disk not referenced by any entry
        """
        channel_dir = build_channel_dir(output_dir, self.channel_name)
        referenced = {
            os.path.basename(e["file_path"])
            for e in self.entries.values()
            if e.get("file_path")
        }

        complete_present: List[Dict[str, Any]] = []
        complete_missing: List[Dict[str, Any]] = []
        for entry in self.entries.values():
            if entry.get("status") != "complete" or not entry.get("file_path"):
                continue
            p = entry["file_path"]
            resolved = p
            if not os.path.isabs(p):
                candidate = os.path.join(output_dir, p)
                if os.path.exists(candidate):
                    resolved = candidate
            if os.path.exists(resolved):
                complete_present.append(entry)
            else:
                complete_missing.append(entry)

        orphan_on_disk: List[str] = []
        if os.path.isdir(channel_dir):
            for root, _dirs, files in os.walk(channel_dir):
                for f in files:
                    if os.path.splitext(f)[1].lower() not in MEDIA_EXTENSIONS:
                        continue
                    if f in referenced:
                        continue
                    orphan_on_disk.append(os.path.abspath(os.path.join(root, f)))

        return {
            "complete_present": complete_present,
            "complete_missing": complete_missing,
            "orphan_on_disk": orphan_on_disk,
        }


class JsonManifest(BaseManifest):
    """JSON-file manifest backend (the original implementation)."""

    def __init__(self, path: str):
        super().__init__(path)
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
        with _SAVE_LOCK:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            data = {"channel_name": self.channel_name, "videos": self.entries}
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                # On Windows, os.replace can fail with EACCES/WinError 5 if a
                # concurrent reader (e.g. a live progress snapshot) briefly holds
                # the target file open. Retry the atomic swap a few times rather
                # than losing the write.
                last_err: OSError | None = None
                for _ in range(10):
                    try:
                        os.replace(tmp, self.path)
                        break
                    except OSError as e:  # transient lock contention
                        last_err = e
                        time.sleep(0.02)
                else:
                    raise last_err or OSError("manifest replace failed")
            except OSError as e:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                raise ManifestError(f"Failed to write manifest {self.path}: {e}")


class SqliteManifest(BaseManifest):
    """SQLite manifest backend (stdlib ``sqlite3``).

    The database lives at a sibling path: ``<name>.sqlite`` next to the
    ``<name>.manifest.json`` path passed in. State is stored as:

    * ``manifest_meta(key TEXT PRIMARY KEY, value TEXT)`` — holds ``channel_name``
    * ``videos(video_id TEXT PRIMARY KEY, data TEXT)`` — each row's ``data`` is
      the full entry dict serialized as JSON (preserving the exact schema/keys).
    """

    def __init__(self, path: str):
        super().__init__(path)
        self.db_path = os.path.splitext(path)[0] + ".sqlite"
        self.load()

    # --- persistence -------------------------------------------------------
    def load(self) -> None:
        if not os.path.exists(self.db_path):
            self.channel_name = None
            self.entries = {}
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM manifest_meta WHERE key='channel_name'")
                row = cur.fetchone()
                self.channel_name = row[0] if row is not None else None
                cur.execute("SELECT video_id, data FROM videos")
                entries: Dict[str, Dict[str, Any]] = {}
                for vid, data in cur.fetchall():
                    entries[vid] = json.loads(data)
                self.entries = entries
        except sqlite3.Error as e:
            raise ManifestError(f"Failed to read manifest db {self.db_path}: {e}")

    def save(self) -> None:
        with _SAVE_LOCK:
            directory = os.path.dirname(os.path.abspath(self.db_path))
            os.makedirs(directory, exist_ok=True)
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS manifest_meta "
                        "(key TEXT PRIMARY KEY, value TEXT)"
                    )
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS videos "
                        "(video_id TEXT PRIMARY KEY, data TEXT)"
                    )
                    cur.execute(
                        "INSERT OR REPLACE INTO manifest_meta(key, value) "
                        "VALUES('channel_name', ?)",
                        (self.channel_name,),
                    )
                    for vid, entry in self.entries.items():
                        cur.execute(
                            "INSERT OR REPLACE INTO videos(video_id, data) "
                            "VALUES(?, ?)",
                            (vid, json.dumps(entry, ensure_ascii=False)),
                        )
                    conn.commit()
            except sqlite3.Error as e:
                raise ManifestError(f"Failed to write manifest db {self.db_path}: {e}")

    # --- migration ---------------------------------------------------------
    def migrate_from_json(self, json_path: str) -> None:
        """Load an existing JSON manifest and persist it into this SQLite db.

        Non-destructive: the source JSON file is left in place.
        """
        src = JsonManifest(json_path)
        self.channel_name = src.channel_name
        self.entries = src.entries
        self.save()


class Manifest(JsonManifest):
    """Legacy alias: ``Manifest(path)`` builds a JSON manifest (backward compat).

    New code should prefer :meth:`open` to select a backend.
    """

    @classmethod
    def open(cls, path: str, backend: str = "auto") -> BaseManifest:
        """Factory: construct the appropriate manifest backend.

        backend:
          * ``"json"``    -> :class:`JsonManifest`
          * ``"sqlite"``  -> :class:`SqliteManifest` (migrating an existing JSON
                             file at ``path`` if present, non-destructively)
          * ``"auto"``    -> reuse an existing SQLite db if present; otherwise,
                             for an existing JSON file with more than 5000
                             entries, migrate to SQLite; else use JSON.
        """
        db_path = os.path.splitext(path)[0] + ".sqlite"

        if backend == "json":
            return JsonManifest(path)

        if backend == "sqlite":
            if os.path.exists(path):
                m = SqliteManifest(db_path)
                m.migrate_from_json(path)
                return m
            return SqliteManifest(db_path)

        if backend == "auto":
            if os.path.exists(db_path):
                return SqliteManifest(db_path)
            if os.path.exists(path):
                src = JsonManifest(path)
                if len(src.entries) > 5000:
                    m = SqliteManifest(db_path)
                    m.migrate_from_json(path)
                    return m
                return JsonManifest(path)
            return JsonManifest(path)

        raise ManifestError(
            f"Unknown manifest backend {backend!r}; expected auto|json|sqlite"
        )
