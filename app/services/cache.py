"""Bounded, process-local TTL cache with an approximate byte budget."""
from __future__ import annotations

import json
from threading import RLock
from cachetools import TTLCache
from app.core.config import settings


def _value_size(value) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="ignore"))
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return 1024


class MemoryCache:
    def __init__(self):
        # cachetools uses the supplied getsizeof value when an entry is inserted.
        self._cache = TTLCache(
            maxsize=settings.cache_max_bytes,
            ttl=settings.cache_ttl_seconds,
            getsizeof=_value_size,
        )
        self._max_items = settings.cache_max_items
        self._lock = RLock()

    def get(self, key: str):
        with self._lock:
            return self._cache.get(key)

    def set(self, key: str, value):
        with self._lock:
            try:
                self._cache[key] = value
            except ValueError:
                # Oversized upstream payloads are returned but not cached.
                return
            while len(self._cache) > self._max_items:
                self._cache.popitem()

    def clear(self):
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict:
        with self._lock:
            return {"items": len(self._cache), "bytes": self._cache.currsize,
                    "max_items": self._max_items, "max_bytes": self._cache.maxsize}


cache = MemoryCache()
