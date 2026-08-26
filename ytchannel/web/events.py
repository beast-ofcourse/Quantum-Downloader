"""In-process pub/sub for live download progress events.

One :class:`EventBus` lives in the web app. The :class:`~ytchannel.web.service.Service`
owns it and the :class:`~ytchannel.web.reporter.WebReporter` publishes to it; WebSocket
handlers subscribe per job and stream the events to browsers.
"""

from __future__ import annotations

import queue
from typing import Dict, List


class EventBus:
    """Fan-out event bus keyed by job id.

    Each subscriber gets its own :class:`queue.Queue` (thread-safe). Publishing
    delivers a copy of the event to every subscriber for that job. If there are
    no subscribers the event is dropped silently — progress is ephemeral and a
    missed event is not an error.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[queue.Queue]] = {}

    def subscribe(self, job_id: str) -> "queue.Queue":
        """Register a new subscriber and return its personal queue."""
        q: "queue.Queue" = queue.Queue()
        self._subscribers.setdefault(job_id, []).append(q)
        return q

    def publish(self, job_id: str, event: dict) -> None:
        """Deliver ``event`` to every subscriber queue for ``job_id``."""
        for q in self._subscribers.get(job_id, []):
            q.put(event)

    def unsubscribe(self, job_id: str, q: "queue.Queue") -> None:
        """Remove a subscriber queue (e.g. on WebSocket disconnect)."""
        subs = self._subscribers.get(job_id)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                self._subscribers.pop(job_id, None)
