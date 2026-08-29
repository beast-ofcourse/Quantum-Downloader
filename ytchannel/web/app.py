"""FastAPI application factory for the local web UI.

Wires together the :class:`EventBus`, :class:`JobStore`, and :class:`Service`,
exposes the REST + WebSocket API described in ``plans/web-ui.md`` (W1), and
serves the static SPA from ``ytchannel/web/static``. All state-changing routes
and the WebSocket handshake enforce a same-origin check (W0.1 / W4.1) to block
cross-site browser requests while still allowing API/test clients that send no
``Origin`` header.
"""

from __future__ import annotations

import asyncio
import os
import queue
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .events import EventBus
from .jobs import JobStore
from .service import MAX_CONCURRENT_JOBS, Service, validate_web_options

STATIC_DIR = Path(__file__).parent / "static"


def verify_origin(request: Request) -> None:
    """Reject cross-site browser requests; allow no-Origin / local clients.

    A missing ``Origin`` header means a non-browser client (curl, TestClient),
    which is allowed. A present ``Origin`` must match the server's own origin or
    be a localhost variant (127.0.0.1 / localhost), otherwise we return 403.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.url.hostname
    if host is None:
        return
    origin_host = urlparse(origin).hostname
    if origin_host in ("127.0.0.1", "localhost", host):
        return
    port = request.url.port
    server_origin = f"http://{host}:{port}" if port else f"http://{host}"
    if origin == server_origin:
        return
    raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    host = websocket.url.hostname
    if host is None:
        return True
    origin_host = urlparse(origin).hostname
    if origin_host in ("127.0.0.1", "localhost", host):
        return True
    port = websocket.url.port
    server_origin = f"http://{host}:{port}" if port else f"http://{host}"
    return origin == server_origin


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = EventBus()
    store = JobStore()
    service = Service(bus, store)
    app.state.bus = bus
    app.state.store = store
    app.state.service = service
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Quantum-Downloader Web UI", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/jobs")
    async def create_job(
        request: Request,
        _: None = Depends(verify_origin),
    ) -> Dict[str, Any]:
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        url = data.get("url")
        if not url or not isinstance(url, str):
            raise HTTPException(status_code=400, detail="url is required")
        try:
            options = validate_web_options(
                {k: v for k, v in data.items() if k != "url"}
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        store = request.app.state.store
        # Bound concurrent work so a client cannot exhaust threads (DoS).
        active = sum(
            1 for j in store.list_jobs() if j.status in ("running", "queued")
        )
        if active >= MAX_CONCURRENT_JOBS:
            raise HTTPException(
                status_code=429, detail="too many active jobs; try again later"
            )

        job = store.create(url, options)
        # Launch the run. start_job does the fast resolve/plan inline and hands
        # the blocking archiver call to a worker thread, so this returns
        # immediately while progress is carried by the WebSocket/events.
        await request.app.state.service.start_job(job)
        return {"job_id": job.id, "status": job.status}

    @app.get("/api/jobs")
    async def list_jobs(request: Request) -> List[Dict[str, Any]]:
        jobs = request.app.state.store.list_jobs()
        return [
            {
                "id": j.id,
                "url": j.url,
                "status": j.status,
                "created_at": j.created_at,
                "summary": j.report,
            }
            for j in jobs
        ]

    @app.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> Dict[str, Any]:
        job = request.app.state.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {
            "id": job.id,
            "url": job.url,
            "options": job.options,
            "status": job.status,
            "created_at": job.created_at,
            "report": job.report,
            "target_key": job.target_key,
        }

    @app.websocket("/api/jobs/{job_id}/progress")
    async def ws_progress(websocket: WebSocket, job_id: str) -> None:
        if not _origin_allowed(websocket):
            await websocket.close(code=403)
            return
        await websocket.accept()
        bus = websocket.app.state.bus
        store = websocket.app.state.store
        service = websocket.app.state.service

        job = store.get(job_id)
        if job is None:
            await websocket.send_json({"type": "error", "message": "job not found"})
            await websocket.close()
            return

        # Snapshot first (best-effort; wait briefly for target_key if the job
        # is still resolving so a late subscriber sees correct progress).
        key = job.target_key
        for _ in range(100):
            if key is not None:
                break
            await asyncio.sleep(0.05)
            job = store.get(job_id)
            key = job.target_key if job else None
        output_dir = (
            (job.options.get("output_dir") or service.output_dir)
            if job
            else service.output_dir
        )
        snap = service.snapshot(output_dir, key) if key is not None else {"exists": False}
        await websocket.send_json(snap)

        # Stream live events until a terminal event.
        q = bus.subscribe(job_id)
        try:
            job = store.get(job_id)
            if job is not None and job.status in ("done", "cancelled", "failed"):
                if job.status == "done":
                    await websocket.send_json({"type": "complete", "report": job.report})
                elif job.status == "cancelled":
                    await websocket.send_json({"type": "cancelled"})
                else:
                    await websocket.send_json(
                        {"type": "failed", "error": (job.report or {}).get("error")}
                    )
            else:
                get_task = None
                try:
                    while True:
                        # Poll with a timeout so a client disconnect can cancel
                        # the blocked get() instead of leaking the task forever.
                        get_task = asyncio.ensure_future(
                            asyncio.to_thread(q.get, 0.5)
                        )
                        try:
                            event = await get_task
                        except queue.Empty:
                            continue
                        await websocket.send_json(event)
                        if event.get("type") in ("complete", "cancelled", "failed"):
                            break
                finally:
                    if get_task is not None:
                        get_task.cancel()
        finally:
            bus.unsubscribe(job_id, q)
        await websocket.close()

    @app.get("/api/targets")
    async def list_targets(request: Request) -> List[Dict[str, Any]]:
        svc = request.app.state.service
        return svc.list_targets(svc.output_dir)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(
        request: Request, job_id: str, _: None = Depends(verify_origin)
    ) -> Dict[str, bool]:
        cancelled = request.app.state.service.cancel_job(job_id)
        return {"cancelled": cancelled}

    @app.post("/api/index")
    async def index_export(
        request: Request, _: None = Depends(verify_origin)
    ) -> FileResponse:
        from ..indexer import export_csv, export_json
        from ..resolver import ResolutionError, resolve_target
        from ..utils.organize import sanitize_segment

        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        url = data.get("url")
        if not url or not isinstance(url, str):
            raise HTTPException(status_code=400, detail="url is required")
        playlist = bool(data.get("playlist", False))
        fmt = data.get("format", "json")
        try:
            result = resolve_target(url, playlist=playlist, quiet=True)
        except (ResolutionError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

        suffix = ".csv" if fmt == "csv" else ".json"
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            if fmt == "csv":
                export_csv(result, tmp)
                media_type = "text/csv"
            else:
                export_json(result, tmp)
                media_type = "application/json"
        except Exception:
            # Don't leak the temp file if export fails.
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise HTTPException(status_code=500, detail="failed to build export")
        target_name = result.get("target_name") or result.get("target_id") or "target"
        filename = f"{sanitize_segment(target_name)}{suffix}"
        # Delete the temp file after the response is sent (BackgroundTask runs
        # once the response has been streamed to the client).
        return FileResponse(
            tmp,
            media_type=media_type,
            filename=filename,
            background=BackgroundTask(os.remove, tmp),
        )

    # Serve the static SPA. The frontend agent owns ytchannel/web/static, so we
    # only mount it when it actually exists (otherwise API-only use still works).
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:

        @app.get("/")
        async def index_placeholder() -> Dict[str, str]:
            return {
                "status": "ok",
                "message": "Web UI static assets not installed; API available under /api.",
            }

    return app
