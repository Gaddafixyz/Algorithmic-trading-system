"""Simple in-process TTL cache."""

from __future__ import annotations

import time
from typing import Any, Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    """In-memory key-value cache with per-entry TTL."""

    def __init__(self, default_ttl: float = 60.0, max_size: int = 1000) -> None:
        self._ttl = default_ttl
        self._max = max_size
        self._store: dict[str, tuple[V, float]] = {}

    def get(self, key: str) -> V | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: V, ttl: float | None = None) -> None:
        if len(self._store) >= self._max:
            # Evict oldest entry
            oldest = min(self._store.items(), key=lambda x: x[1][1])
            del self._store[oldest[0]]
        self._store[key] = (value, time.monotonic() + (ttl or self._ttl))

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
