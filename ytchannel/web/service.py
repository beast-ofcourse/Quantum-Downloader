"""Web service layer: turns API requests into real archiver runs.

The :class:`Service` owns the cross-cutting state the web UI needs beyond the
core engine: a per-target run guard (only one active download per manifest at a
time), a cancellation flag map, and the helpers to build a :class:`Config` from
request options and to read live manifest snapshots for late WebSocket
subscribers.

The blocking :func:`run_archiver` call is executed in a daemon worker thread
(not via ``asyncio.to_thread`` inside an ``asyncio`` task) so that job progress
is independent of the ASGI event loop's lifecycle. This keeps the run alive and
cancellable whether the server is a long-lived uvicorn process or a
``TestClient`` driving several apps on a shared loop.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, List, Set

from ..archiver import run_archiver
from ..config import DEFAULT_CONFIG_PATH, Config
from ..downloader import Downloader
from ..indexer import dry_run_summary
from ..manifest import BaseManifest, Manifest
from ..planner import filter_videos, plan_downloads
from ..resolver import ResolutionError, resolve_target
from ..storage import manifest_path, storage_key
from .events import EventBus
from .jobs import Job, JobStore
from .reporter import WebReporter

# Request option keys that map directly onto Config fields (via merge_cli).
# NOTE: `playlist`, `template`, and `log_file` are deliberately excluded — they
# are not Config fields (`playlist`) or are filesystem-write vectors (`template`,
# `log_file`) that must never be client-controlled on the web API. `output_dir`
# is always pinned to the server's own directory (see start_job).
_OPTION_KEYS = (
    "quality",
    "audio_only",
    "limit",
    "after",
    "before",
    "write_thumbnail",
    "write_description",
    "write_subs",
    "cookies",
    "delay",
    "proxy",
    "cookies_from_browser",
    "manifest_backend",
    "concurrency",
    "quiet",
    "verbose",
)

# Keys the web API is allowed to accept at all. Anything else (output_dir,
# template, log_file, or unknown keys) is dropped before it reaches the engine.
_WEB_SAFE_KEYS: Set[str] = {
    "playlist",
    "dry_run",
    "quality",
    "audio_only",
    "limit",
    "after",
    "before",
    "write_thumbnail",
    "write_description",
    "write_subs",
    "cookies",
    "delay",
    "proxy",
    "cookies_from_browser",
    "manifest_backend",
    "concurrency",
    "quiet",
    "verbose",
}

# Hard ceiling on concurrent worker jobs so a client cannot exhaust threads.
MAX_CONCURRENT_JOBS = 8


def validate_web_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce web API job options.

    Drops any key not in :data:`_WEB_SAFE_KEYS` (so ``output_dir``, ``template``,
    ``log_file`` and unknown keys can never reach the engine), coerces types, and
    enforces ranges. Raises ``ValueError`` with a human-readable message on bad
    input so the caller can return HTTP 400.
    """
    cleaned: Dict[str, Any] = {}
    for key, value in options.items():
        if key not in _WEB_SAFE_KEYS:
            continue
        cleaned[key] = value

    if "concurrency" in cleaned:
        try:
            c = int(cleaned["concurrency"])
        except (TypeError, ValueError):
            raise ValueError("concurrency must be an integer")
        if c < 1:
            raise ValueError("concurrency must be >= 1")
        cleaned["concurrency"] = min(c, 16)

    if "limit" in cleaned and cleaned["limit"] is not None:
        try:
            lim = int(cleaned["limit"])
        except (TypeError, ValueError):
            raise ValueError("limit must be an integer")
        if lim < 0:
            raise ValueError("limit must be >= 0")
        cleaned["limit"] = lim

    if "delay" in cleaned and cleaned["delay"] is not None:
        try:
            d = float(cleaned["delay"])
        except (TypeError, ValueError):
            raise ValueError("delay must be a number")
        if d < 0:
            raise ValueError("delay must be >= 0")
        cleaned["delay"] = d

    if "manifest_backend" in cleaned and cleaned["manifest_backend"] not in (
        "auto",
        "json",
        "sqlite",
    ):
        raise ValueError("manifest_backend must be one of: auto, json, sqlite")

    for flag in (
        "playlist",
        "dry_run",
        "audio_only",
        "write_thumbnail",
        "write_description",
        "write_subs",
        "quiet",
        "verbose",
    ):
        if flag in cleaned:
            cleaned[flag] = bool(cleaned[flag])

    for text in (
        "quality",
        "cookies",
        "proxy",
        "cookies_from_browser",
        "after",
        "before",
    ):
        if text in cleaned and cleaned[text] is not None:
            cleaned[text] = str(cleaned[text])

    for date_key in ("after", "before"):
        val = cleaned.get(date_key)
        if val and not re.match(r"^\d{8}$", str(val)):
            raise ValueError(f"{date_key} must be a date in YYYYMMDD format")

    return cleaned


class Service:
    """Drives download jobs on behalf of the web API."""

    def __init__(
        self,
        bus: EventBus,
        store: JobStore,
        output_dir: str = "./downloads",
        downloader_cls: type = Downloader,
    ) -> None:
        self.bus = bus
        self.store = store
        self.output_dir = output_dir
        self.downloader_cls = downloader_cls
        # One active run per target manifest (W0.5 / W4.2). A threading.Lock
        # (not asyncio.Lock) because the run itself lives in a worker thread.
        self._target_locks: Dict[str, threading.Lock] = {}
        # job_id -> cancel requested.
        self._cancelled: Dict[str, bool] = {}

    async def start_job(self, job: Job) -> None:
        """Resolve, plan, and launch (or dry-run) the job's download.

        The fast resolve/plan work happens inline (so ``target_key`` is known
        immediately for late WebSocket subscribers); the blocking
        :func:`run_archiver` call is handed to a daemon thread so progress and
        cancellation are independent of the request/event-loop lifecycle.
        """
        options = job.options

        # 1. Build config from defaults + request options.
        try:
            cfg = Config.from_file(DEFAULT_CONFIG_PATH)
        except Exception:
            cfg = Config()
        cli_opts: Dict[str, Any] = {}
        for key in _OPTION_KEYS:
            if key in options and options[key] is not None:
                cli_opts[key] = options[key]
        # The web service decides where files land. The client can never choose
        # the output directory (a client-supplied path would be an arbitrary
        # file-write primitive), so it is always pinned to the server's dir.
        cli_opts["output_dir"] = self.output_dir
        cfg.merge_cli(cli_opts)

        # 2. Validate / resolve the URL.
        try:
            result = resolve_target(job.url, playlist=options.get("playlist", False), quiet=True)
        except (ResolutionError, ValueError) as e:
            job.status = "failed"
            job.report = {"error": str(e)}
            self.store.update(job)
            self.bus.publish(job.id, {"type": "failed", "error": str(e)})
            return

        key = storage_key(result)
        job.target_key = key
        videos = result["videos"]
        videos = filter_videos(videos, cfg)

        # 3. Dry run: compute the plan summary and stop (no download).
        if options.get("dry_run"):
            summary = dry_run_summary({**result, "videos": videos})
            job.report = summary
            job.status = "done"
            self.store.update(job)
            self.bus.publish(job.id, {"type": "complete", "report": summary})
            return

        # 4. Real run: reconcile + plan against the manifest.
        manifest = Manifest.open(
            manifest_path(cfg.output_dir, key), backend=cfg.manifest_backend
        )
        name = result.get("target_name") or result.get("channel_name") or "target"
        manifest.reconcile(videos, name)
        plan = plan_downloads(videos, manifest, cfg)

        # 5. Launch the (blocking) archiver in a worker thread.
        lock = self._target_locks.setdefault(key, threading.Lock())
        job.status = "running"
        self.store.update(job)
        threading.Thread(
            target=self._run_thread,
            args=(lock, job, cfg, manifest, plan, key),
            daemon=True,
        ).start()

    def _run_thread(
        self,
        lock: threading.Lock,
        job: Job,
        cfg: Config,
        manifest: BaseManifest,
        plan: Any,
        key: str,
    ) -> None:
        """Worker-thread entry point: serialize per target, run, report."""
        with lock:
            _execute_run(self, job, cfg, manifest, plan, key)

    def cancel_job(self, job_id: str) -> bool:
        """Request cancellation of a job; returns True if the job exists."""
        self._cancelled[job_id] = True
        job = self.store.get(job_id)
        if job is None:
            return False
        if job.status in ("running", "queued"):
            job.status = "cancelled"
            self.store.update(job)
            self.bus.publish(job_id, {"type": "cancelled"})
        return True

    def snapshot(self, output_dir: str, key: str) -> dict:
        """Best-effort live state of a target manifest (for late subscribers)."""
        # A target may be stored as JSON (default) or SQLite (--manifest sqlite);
        # try both candidate paths.
        candidates = [
            manifest_path(output_dir, key),
            os.path.splitext(manifest_path(output_dir, key))[0] + ".sqlite",
        ]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if path is None:
            return {"exists": False}
        try:
            manifest = Manifest.open(path, backend="auto")
        except Exception:
            return {"exists": False}
        try:
            completed = sum(
                1 for e in manifest.entries.values() if e.get("status") == "complete"
            )
            failed = len(manifest.get_failed())
            pending = len(manifest.get_pending())
            entries = [
                {
                    "video_id": vid,
                    "title": e.get("title"),
                    "status": e.get("status"),
                }
                for vid, e in manifest.entries.items()
            ]
            return {
                "exists": True,
                "completed": completed,
                "failed": failed,
                "pending": pending,
                "entries": entries,
            }
        except Exception:
            return {"exists": True, "completed": 0, "failed": 0, "pending": 0, "entries": []}

    def list_targets(self, output_dir: str) -> List[dict]:
        """Scan the output dir for manifests and summarize each target."""
        results: List[dict] = []
        if not os.path.isdir(output_dir):
            return results
        for name in os.listdir(output_dir):
            if name.endswith(".manifest.json"):
                path = os.path.join(output_dir, name)
                key = name[: -len(".manifest.json")]
            elif name.endswith(".manifest.sqlite"):
                path = os.path.join(output_dir, name)
                key = name[: -len(".manifest.sqlite")]
            elif name.endswith(".sqlite"):
                path = os.path.join(output_dir, name)
                key = name[: -len(".sqlite")]
            else:
                continue
            try:
                manifest = Manifest.open(path, backend="auto")
            except Exception:
                continue
            completed = sum(
                1 for e in manifest.entries.values() if e.get("status") == "complete"
            )
            failed = len(manifest.get_failed())
            pending = len(manifest.get_pending())
            results.append(
                {
                    "key": key,
                    "target_name": manifest.channel_name or key,
                    "completed": completed,
                    "failed": failed,
                    "pending": pending,
                }
            )
        return results


def _execute_run(
    service: "Service",
    job: Job,
    cfg: Config,
    manifest: BaseManifest,
    plan: Any,
    key: str,
) -> None:
    """Run the archiver and record the outcome on the job + event bus.

    Executed inside a worker thread, so it owns the terminal status transition
    and the final ``complete`` / ``failed`` / ``cancelled`` event.
    """
    try:
        summary = run_archiver(
            cfg,
            manifest,
            plan,
            target_key=key,
            downloader_cls=service.downloader_cls,
            reporter=WebReporter(service.bus, job.id),
            should_cancel=lambda: service._cancelled.get(job.id, False),
        )
    except Exception as e:  # noqa: BLE001 - surface as a failed job
        job.status = "failed"
        job.report = {"error": str(e)}
        service.store.update(job)
        service.bus.publish(job.id, {"type": "failed", "error": str(e)})
        return

    if summary.interrupted or service._cancelled.get(job.id):
        job.status = "cancelled"
        service.store.update(job)
        service.bus.publish(job.id, {"type": "cancelled"})
    else:
        job.status = "done"
        # Expose the already-complete count under the UI's "skipped" label so the
        # final report shows the right number (Summary uses `already_complete`).
        report = dict(summary.__dict__)
        report["skipped"] = summary.already_complete
        job.report = report
        service.store.update(job)
        service.bus.publish(job.id, {"type": "complete", "report": report})
    # Drop the cancel flag so the dict does not grow without bound (L1).
    service._cancelled.pop(job.id, None)
