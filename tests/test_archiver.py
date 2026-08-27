"""Tests for the archiver driver (sequential + concurrent paths)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console

from ytchannel.archiver import batch_eta, run_archiver
from ytchannel.config import Config
from ytchannel.downloader import DownloadReporter
from ytchannel.manifest import Manifest
from ytchannel.planner import plan_downloads

# Module-level record of worker thread names (guarded by a lock) so the
# concurrent test can prove more than one thread actually did the work.
_RECORDED_THREADS: List[str] = []
_RECORDED_LOCK = threading.Lock()


def _make_manifest(tmp_path: Path, n: int) -> Any:
    manifest = Manifest(str(tmp_path / "m.manifest.json"))
    videos = [{"video_id": f"v{i}", "title": f"V{i}"} for i in range(n)]
    manifest.reconcile(videos, "Test")
    return manifest, videos


def test_run_archiver_sequential_still_works(tmp_path: Path) -> None:
    """The default (concurrency=1) path must behave exactly as before."""
    manifest, videos = _make_manifest(tmp_path, 3)
    cfg = Config(concurrency=1, delay=0)
    plan = plan_downloads(videos, manifest, cfg)

    class FakeDownloader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def download(
            self, video: Dict[str, Any], manifest: Manifest, reporter: Any = None
        ) -> Dict[str, Any]:
            manifest.mark_complete(video["video_id"], "p.mp4")
            return {
                "video_id": video["video_id"],
                "status": "complete",
                "file_path": "p.mp4",
            }

    result = run_archiver(
        cfg,
        manifest,
        plan,
        target_key="x",
        downloader_cls=FakeDownloader,
        console=Console(),
    )
    assert result.downloaded == 3
    for v in videos:
        assert manifest.is_complete(v["video_id"])


def test_run_archiver_concurrent_parallel(tmp_path: Path) -> None:
    """With concurrency>1 the work is actually spread across threads."""
    _RECORDED_THREADS.clear()
    manifest, videos = _make_manifest(tmp_path, 4)
    cfg = Config(concurrency=2, delay=0)
    plan = plan_downloads(videos, manifest, cfg)

    class FakeDownloader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def download(
            self, video: Dict[str, Any], manifest: Manifest, reporter: Any = None
        ) -> Dict[str, Any]:
            time.sleep(0.05)
            with _RECORDED_LOCK:
                _RECORDED_THREADS.append(threading.current_thread().name)
            manifest.mark_complete(video["video_id"], "p.mp4")
            return {
                "video_id": video["video_id"],
                "status": "complete",
                "file_path": "p.mp4",
            }

    result = run_archiver(
        cfg,
        manifest,
        plan,
        target_key="x",
        downloader_cls=FakeDownloader,
        console=Console(),
    )
    assert result.downloaded == 4
    for v in videos:
        assert manifest.is_complete(v["video_id"])
    # More than one distinct worker thread name proves parallel execution.
    assert len(set(_RECORDED_THREADS)) > 1


def test_batch_eta() -> None:
    # Linear extrapolation: 20s for 2 of 10 -> 80s remaining.
    assert batch_eta(2, 10, 20.0) == 80.0
    # No progress yet -> unknown.
    assert batch_eta(0, 10, 5.0) is None
    # Already finished -> unknown.
    assert batch_eta(10, 10, 5.0) is None


class _RecordingReporter(DownloadReporter):
    """Captures start/finish calls so we can assert the injected reporter fired."""

    def __init__(self) -> None:
        self.starts: List[str] = []
        self.finishes = 0

    def video_start(self, title: str) -> None:
        self.starts.append(title)

    def video_finish(self) -> None:
        self.finishes += 1


def test_run_archiver_injected_reporter_records_calls(tmp_path: Path) -> None:
    """A caller-supplied reporter must receive video_start/video_finish."""
    manifest, videos = _make_manifest(tmp_path, 2)
    cfg = Config(concurrency=1, delay=0)
    plan = plan_downloads(videos, manifest, cfg)
    reporter = _RecordingReporter()

    class FakeDownloader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def download(
            self, video: Dict[str, Any], manifest: Manifest, reporter: Any = None
        ) -> Dict[str, Any]:
            if reporter is not None:
                reporter.video_start(video["title"])
                reporter.video_finish()
            manifest.mark_complete(video["video_id"], "p.mp4")
            return {
                "video_id": video["video_id"],
                "status": "complete",
                "file_path": "p.mp4",
            }

    result = run_archiver(
        cfg,
        manifest,
        plan,
        target_key="x",
        downloader_cls=FakeDownloader,
        reporter=reporter,
    )
    assert result.downloaded == 2
    assert reporter.starts == ["V0", "V1"]
    assert reporter.finishes == 2


def test_run_archiver_should_cancel_stops_loop(tmp_path: Path) -> None:
    """should_cancel returning True after the first video stops the run."""
    manifest, videos = _make_manifest(tmp_path, 3)
    cfg = Config(concurrency=1, delay=0)
    plan = plan_downloads(videos, manifest, cfg)

    state = {"count": 0}

    def should_cancel() -> bool:
        return state["count"] >= 1

    class FakeDownloader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def download(
            self, video: Dict[str, Any], manifest: Manifest, reporter: Any = None
        ) -> Dict[str, Any]:
            state["count"] += 1
            manifest.mark_complete(video["video_id"], "p.mp4")
            return {
                "video_id": video["video_id"],
                "status": "complete",
                "file_path": "p.mp4",
            }

    result = run_archiver(
        cfg,
        manifest,
        plan,
        target_key="x",
        downloader_cls=FakeDownloader,
        should_cancel=should_cancel,
    )
    assert result.interrupted is True
    assert result.downloaded == 1
