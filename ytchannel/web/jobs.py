"""Job persistence for the web UI.

A :class:`Job` is the durable record of one download request: its URL, the
options it was created with, its lifecycle ``status``, and the final
``RunReport``. :class:`JobStore` persists one JSON file per job (atomic
temp-file + ``os.replace``, mirroring :class:`~ytchannel.manifest.Manifest`)
and keeps an in-memory index so list queries are cheap and jobs survive a
server restart.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEFAULT_STORE_DIR = os.path.join(
    os.path.expanduser("~"), ".local", "share", "ytchannel", "jobs"
)


@dataclass
class Job:
    """One download request and its outcome."""

    id: str
    url: str
    options: dict
    status: str  # queued | running | done | failed | cancelled
    created_at: str
    report: Optional[dict] = None
    # Stable storage key (target_type_target_id) set once resolved; lets the
    # WebSocket handler fetch a live manifest snapshot without re-resolving.
    target_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "options": self.options,
            "status": self.status,
            "created_at": self.created_at,
            "report": self.report,
            "target_key": self.target_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        return cls(
            id=data["id"],
            url=data["url"],
            options=data.get("options", {}) or {},
            status=data["status"],
            created_at=data["created_at"],
            report=data.get("report"),
            target_key=data.get("target_key"),
        )


class JobStore:
    """Filesystem-backed job store with an in-memory index."""

    def __init__(self, store_dir: str = DEFAULT_STORE_DIR) -> None:
        self.store_dir = store_dir
        os.makedirs(self.store_dir, exist_ok=True)
        self._index: Dict[str, Job] = {}
        self._load_all()

    def _load_all(self) -> None:
        for name in os.listdir(self.store_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.store_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            try:
                job = Job.from_dict(data)
            except (KeyError, TypeError):
                continue
            self._index[job.id] = job

    def _path(self, job_id: str) -> str:
        return os.path.join(self.store_dir, f"{job_id}.json")

    def _write(self, job: Job) -> None:
        """Atomically persist a job (temp file + os.replace)."""
        path = self._path(job.id)
        data = job.to_dict()
        fd, tmp = tempfile.mkstemp(dir=self.store_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise

    def create(self, url: str, options: dict) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            url=url,
            options=options,
            status="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._write(job)
        self._index[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._index.get(job_id)

    def list_jobs(self) -> List[Job]:
        return sorted(self._index.values(), key=lambda j: j.created_at, reverse=True)

    def update(self, job: Job) -> None:
        self._write(job)
        self._index[job.id] = job
