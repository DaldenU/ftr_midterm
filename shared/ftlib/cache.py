"""Last-known-good cache used for graceful degradation of read paths (FT only)."""
from __future__ import annotations

import collections
import threading


class LastKnownGood:
    def __init__(self, max_entries: int = 20000):
        self._d: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    def get(self, key: str):
        with self._lock:
            return self._d.get(key)

    def __len__(self):
        return len(self._d)
