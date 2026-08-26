"""Rate limiting and retry backoff.

Keeps the tool polite toward YouTube: a small delay between downloads by
default, and exponential backoff when a transient failure occurs.
"""

from __future__ import annotations

import random
import time


class RateLimiter:
    def __init__(self, base_delay: float = 2.0, jitter: float = 1.0, max_backoff: float = 60.0):
        # Negative or zero delay means "no delay".
        self.base_delay = base_delay
        self.jitter = max(0.0, jitter)
        self.max_backoff = max(0.0, max_backoff)

    def delay(self) -> None:
        """Sleep for base_delay plus a little random jitter (no-op if delay <= 0)."""
        if self.base_delay <= 0:
            return
        wait = self.base_delay + random.uniform(0, self.jitter)
        time.sleep(wait)

    def backoff(self, attempt: int) -> None:
        """Sleep for an exponential backoff based on the retry attempt number."""
        if attempt <= 0:
            return
        wait = min(self.max_backoff, (2 ** attempt) * max(self.base_delay, 1.0))
        wait += random.uniform(0, self.jitter)
        time.sleep(wait)
