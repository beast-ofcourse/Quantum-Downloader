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
import threading
from typing import Any, Dict, List

from ..archiver import run_archiver
from ..config import DEFAULT_CONFIG_PATH, Config
from ..downloader import Downloader
from ..indexer import dry_run_summary
from ..manifest import BaseManifest, Manifest
from ..planner import filter_videos, plan_downloads
from ..resolver import ResolutionError, resolve_channel, resolve_playlist
from ..storage import manifest_path, storage_key
from .events import EventBus
from .jobs import Job, JobStore
from .reporter import WebReporter

# Request option keys that map directly onto Config fields (via merge_cli).
_OPTION_KEYS = (
    "playlist",
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
    "template",
    "log_file",
)


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

        The fast resolve/plan work happens inline; the blocking
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
        # The web service decides where files land (the server's output dir).
        cli_opts["output_dir"] = options.get("output_dir") or self.output_dir
        cfg.merge_cli(cli_opts)

        # 2. Validate / resolve the URL.
        try:
            if options.get("playlist"):
                result = resolve_playlist(job.url, quiet=True)
            else:
                result = resolve_channel(job.url, quiet=True)
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
        path = manifest_path(output_dir, key)
        if not os.path.exists(path):
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
            if not name.endswith(".manifest.json"):
                continue
            path = os.path.join(output_dir, name)
            try:
                manifest = Manifest.open(path, backend="auto")
            except Exception:
                continue
            key = name[: -len(".manifest.json")]
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
    else:
        job.status = "done"
    # Expose the already-complete count under the UI's "skipped" label so the
    # final report shows the right number (Summary uses `already_complete`).
    report = dict(summary.__dict__)
    report["skipped"] = summary.already_complete
    job.report = report
    service.store.update(job)
    service.bus.publish(job.id, {"type": "complete", "report": report})
