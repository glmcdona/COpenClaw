from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class RateLimiter:
    max_calls: int
    window_seconds: int
    _store: Dict[str, list[float]] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds
        calls = [t for t in self._store.get(key, []) if t >= window_start]
        if len(calls) >= self.max_calls:
            self._store[key] = calls
            return False
        calls.append(now)
        self._store[key] = calls
        return True