"""In-process token bucket. One process, one memory space. Restarts reset it."""

import math
import time
from threading import Lock


class TokenBucketLimiter:
    def __init__(self, capacity: int = 30, window_seconds: float = 60.0) -> None:
        self.capacity = capacity
        self.refill_per_sec = capacity / window_seconds
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            tokens, updated = self._state.get(key, (float(self.capacity), now))
            elapsed = max(0.0, now - updated)
            tokens = min(float(self.capacity), tokens + elapsed * self.refill_per_sec)
            if tokens < 1.0:
                retry = max(1, math.ceil((1.0 - tokens) / self.refill_per_sec))
                self._state[key] = (tokens, now)
                return False, retry
            self._state[key] = (tokens - 1.0, now)
            return True, 0


limiter = TokenBucketLimiter()
