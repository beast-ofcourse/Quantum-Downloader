"""Offline tests for the web API (FastAPI TestClient).

No real yt-dlp / network: the downloader and resolver used by the service are
monkeypatched with canned fakes. Job state and downloads go to tmp_path.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import ytchannel.resolver as resolver_mod
import ytchannel.web.service as service_mod
from ytchannel.web.app import create_app
from ytchannel.web.events import EventBus
from ytchannel.web.jobs import JobStore

FAKE_VIDEOS = [
    {"video_id": "v1", "title": "V1", "url": "https://youtube.com/watch?v=v1"},
    {"video_id": "v2", "title": "V2", "url": "https://youtube.com/watch?v=v2"},
    {"video_id": "v3", "title": "V3", "url": "https://youtube.com/watch?v=v3"},
]


def _fake_resolve_target(url: str, playlist: bool = False, quiet: bool = True) -> Dict[str, Any]:
    if playlist:
        return {
            "target_type": "playlist",
            "target_name": "TestPlaylist",
            "target_id": "PLtest123",
            "url": url,
            "videos": list(FAKE_VIDEOS),
        }
    return {
        "target_type": "channel",
        "target_name": "TestChannel",
        "target_id": "UCtest123",
        "url": url,
        "videos": list(FAKE_VIDEOS),
    }


class FakeDownloader:
    """Instant, offline downloader that marks each video complete."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def download(
        self, video: Dict[str, Any], manifest: Any, reporter: Any = None
    ) -> Dict[str, Any]:
        manifest.mark_complete(video["video_id"], "p.mp4")
        return {
            "video_id": video["video_id"],
            "status": "complete",
            "file_path": "p.mp4",
        }


class SlowDownloader:
    """Sleeps per video so a run stays 'running' long enough to cancel."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def download(
        self, video: Dict[str, Any], manifest: Any, reporter: Any = None
    ) -> Dict[str, Any]:
        time.sleep(0.4)
        manifest.mark_complete(video["video_id"], "p.mp4")
        return {
            "video_id": video["video_id"],
            "status": "complete",
            "file_path": "p.mp4",
        }


@pytest.fixture
def client(tmp_path: Any, monkeypatch: Any) -> Any:
    # Patch both the service's binding and the resolver module the index
    # endpoint imports from, so every entry point uses the canned resolver.
    monkeypatch.setattr(service_mod, "resolve_target", _fake_resolve_target)
    monkeypatch.setattr(resolver_mod, "resolve_target", _fake_resolve_target)

    # The default Config applies a polite rate-limit delay between videos,
    # which would make the offline tests slow. Use a zero-delay, quiet config
    # for the web service under test (behavior of the rate limiter itself is
    # covered elsewhere).
    import ytchannel.config as _cfg_mod

    class _FastConfig(_cfg_mod.Config):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.delay = 0.0
            self.quiet = True

    monkeypatch.setattr(service_mod, "Config", _FastConfig)

    store_dir = tmp_path / "jobs"
    output_dir = tmp_path / "downloads"
    with TestClient(create_app()) as c:
        c.app.state.store = JobStore(str(store_dir))
        c.app.state.service.store = c.app.state.store
        c.app.state.service.output_dir = str(output_dir)
        c.app.state.service.downloader_cls = FakeDownloader
        yield c


def _wait_status(c: TestClient, job_id: str, states: tuple) -> Dict[str, Any]:
    j = c.get(f"/api/jobs/{job_id}").json()
    for _ in range(200):
        if j["status"] in states:
            return j
        time.sleep(0.02)
        j = c.get(f"/api/jobs/{job_id}").json()
    return j


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_job_runs_and_completes(client: TestClient) -> None:
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    assert r.status_code == 200
    body = r.json()
    assert "job_id" in body
    job_id = body["job_id"]
    job = _wait_status(client, job_id, ("done", "failed", "cancelled"))
    assert job["status"] == "done"
    assert job["report"]["downloaded"] == len(FAKE_VIDEOS)


def test_dry_run(client: TestClient) -> None:
    r = client.post(
        "/api/jobs",
        json={"url": "https://www.youtube.com/@test/videos", "dry_run": True},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    job = _wait_status(client, job_id, ("done", "failed"))
    assert job["status"] == "done"
    assert job["report"]["count"] == len(FAKE_VIDEOS)


def test_list_jobs(client: TestClient) -> None:
    client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    r = client.get("/api/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert isinstance(jobs, list)
    assert len(jobs) >= 1
    assert "id" in jobs[0]
    assert "status" in jobs[0]


def test_get_job_404(client: TestClient) -> None:
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_ws_progress(client: TestClient) -> None:
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    job_id = r.json()["job_id"]
    with client.websocket_connect(f"/api/jobs/{job_id}/progress") as ws:
        snap = ws.receive_json()
        complete = ws.receive_json()
    assert snap.get("exists") is True
    assert complete["type"] == "complete"


def test_cancel_job(client: TestClient) -> None:
    client.app.state.service.downloader_cls = SlowDownloader
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    job_id = r.json()["job_id"]
    # Wait until the run is actually in progress.
    for _ in range(200):
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "running":
            break
        time.sleep(0.02)
    cancel = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True
    job = _wait_status(client, job_id, ("cancelled", "done", "failed"))
    assert job["status"] == "cancelled"


def test_cross_origin_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/jobs",
        json={"url": "https://www.youtube.com/@test/videos"},
        headers={"origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


def test_no_origin_allowed(client: TestClient) -> None:
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    assert r.status_code == 200


def test_jobstore_roundtrip(tmp_path: Any) -> None:
    store = JobStore(str(tmp_path / "jobs"))
    job = store.create("https://www.youtube.com/@x/videos", {"quality": "1080p"})
    assert store.get(job.id) is not None
    # Reload from a fresh store (simulates a server restart).
    store2 = JobStore(str(tmp_path / "jobs"))
    reloaded = store2.get(job.id)
    assert reloaded is not None
    assert reloaded.url == job.url
    assert reloaded.options == job.options
    assert reloaded.status == "queued"


def test_eventbus_publish_delivers_and_drops() -> None:
    bus = EventBus()
    q = bus.subscribe("job1")
    bus.publish("job1", {"type": "progress"})
    assert q.get_nowait() == {"type": "progress"}
    # No subscribers for job2 -> dropped silently, no error.
    bus.publish("job2", {"type": "x"})
    # After unsubscribe, events for job1 are dropped.
    bus.unsubscribe("job1", q)
    bus.publish("job1", {"type": "y"})
    assert q.empty()


def test_eventbus_concurrent_publish_subscribe() -> None:
    # The bus must be thread-safe: concurrent publishers + subscribers must not
    # raise (e.g. "list changed size during iteration").
    bus = EventBus()
    q = bus.subscribe("job1")
    errors: list = []

    def publisher() -> None:
        try:
            for i in range(300):
                bus.publish("job1", {"i": i})
        except Exception as e:  # pragma: no cover - failure path
            errors.append(repr(e))

    def subscriber() -> None:
        try:
            for _ in range(300):
                q.get(timeout=3)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(repr(e))

    threads = [threading.Thread(target=publisher) for _ in range(3)] + [
        threading.Thread(target=subscriber) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_cancel_job_emits_cancelled_event(client: TestClient) -> None:
    # Regression: a cancelled run must publish a "cancelled" terminal event,
    # not a "complete" one (the old bug made cancelled jobs look successful).
    client.app.state.service.downloader_cls = SlowDownloader
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    job_id = r.json()["job_id"]
    for _ in range(200):
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "running":
            break
        time.sleep(0.02)
    client.post(f"/api/jobs/{job_id}/cancel")
    with client.websocket_connect(f"/api/jobs/{job_id}/progress") as ws:
        msg = ws.receive_json()  # snapshot (no "type")
        while True:
            msg = ws.receive_json()
            if msg.get("type") in ("cancelled", "complete", "failed"):
                break
    assert msg["type"] == "cancelled"


def test_list_targets_sqlite_key(client: TestClient, tmp_path: Any) -> None:
    # Regression: list_targets must derive the key from the .sqlite suffix,
    # not the (shorter) .manifest.json suffix.
    from ytchannel.manifest import Manifest

    key = "channel_UC1234abcd"
    m = Manifest.open(str(tmp_path / f"{key}.manifest.json"), backend="sqlite")
    m.reconcile([{"video_id": "v1", "title": "V1"}], "Chan")
    m.save()
    targets = client.app.state.service.list_targets(str(tmp_path))
    assert len(targets) == 1
    assert targets[0]["key"] == key


def test_create_job_rejects_bad_options(client: TestClient) -> None:
    # Non-dict body.
    r = client.post("/api/jobs", json="not-a-dict")
    assert r.status_code == 400
    # concurrency must be >= 1.
    r = client.post(
        "/api/jobs",
        json={"url": "https://www.youtube.com/@test/videos", "concurrency": 0},
    )
    assert r.status_code == 400
    # after must be YYYYMMDD.
    r = client.post(
        "/api/jobs",
        json={"url": "https://www.youtube.com/@test/videos", "after": "2020-01-01"},
    )
    assert r.status_code == 400
    # Unsafe keys (template/output_dir) are dropped, not rejected.
    r = client.post(
        "/api/jobs",
        json={
            "url": "https://www.youtube.com/@test/videos",
            "template": "/evil",
            "output_dir": "/x",
        },
    )
    assert r.status_code == 200


def test_create_job_rejects_when_at_capacity(
    client: TestClient, monkeypatch: Any
) -> None:
    monkeypatch.setattr("ytchannel.web.app.MAX_CONCURRENT_JOBS", 1)
    client.app.state.service.downloader_cls = SlowDownloader
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    assert r.status_code == 200
    for _ in range(200):
        if client.get(f"/api/jobs/{r.json()['job_id']}").json()["status"] == "running":
            break
        time.sleep(0.02)
    r2 = client.post("/api/jobs", json={"url": "https://www.youtube.com/@test/videos"})
    assert r2.status_code == 429


def test_jobstore_recovers_running_as_failed(tmp_path: Any) -> None:
    store = JobStore(str(tmp_path / "jobs"))
    job = store.create("https://www.youtube.com/@x/videos", {})
    job.status = "running"
    store.update(job)
    # Reload (simulates a server restart).
    store2 = JobStore(str(tmp_path / "jobs"))
    assert store2.get(job.id).status == "failed"
