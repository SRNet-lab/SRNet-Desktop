"""Per-author-key sliding-window limits (pattern from room_host parse_chat_rate, keyed by
author signing key instead of transport peer)."""
from __future__ import annotations

import collections
import threading
import time


class RateLimiter:
    def __init__(self, rules: list[tuple[int, int]], clock=time.monotonic):
        self.rules = rules  # [(max_posts, window_seconds), ...]
        self.hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.clock, self.lock = clock, threading.Lock()

    def allow(self, key: str) -> bool:
        if not self.rules:
            return True
        now = self.clock()
        horizon = max(w for _, w in self.rules)
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - horizon:
                q.popleft()
            for mx, w in self.rules:
                if sum(1 for t in q if t > now - w) >= mx:
                    return False
            q.append(now)
            return True
