"""WebSocket-facing progress reporter.

:class:`WebReporter` implements the same four-method interface as
:class:`~ytchannel.downloader.DownReporter` but, instead of rendering to the
console, publishes structured events onto the shared :class:`EventBus` so the
web layer can stream them to browsers.
"""

from __future__ import annotations

from typing import Any, Dict

from ..downloader import DownloadReporter
from .events import EventBus


class WebReporter(DownloadReporter):
    """Publishes download progress events for one job to the bus."""

    def __init__(self, bus: EventBus, job_id: str) -> None:
        self._bus = bus
        self._job_id = job_id

    def video_start(self, title: str) -> None:
        self._bus.publish(self._job_id, {"type": "video_start", "title": title})

    def video_progress(self, data: Dict[str, Any]) -> None:
        self._bus.publish(self._job_id, {"type": "progress", "data": data})

    def video_finish(self) -> None:
        self._bus.publish(self._job_id, {"type": "video_finish"})

    def stop(self) -> None:
        # The service publishes the terminal event itself; nothing to tear down.
        pass
