"""Bounded TTL cache — bounded because this is a long-lived process (an unbounded dict is a slow-fuse memory leak); LRU eviction, and expired entries are swept on write rather than left to accumulate."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings


@dataclass
class CacheEntry:
    value: Any
    retrieved_at: datetime
    expires_at: datetime

    @property
    def stale(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at


class TTLCache:
    def __init__(self, max_entries: int) -> None:
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    async def get(self, key: str, allow_stale: bool = False) -> CacheEntry | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.stale and not allow_stale:
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry

    async def put(self, key: str, value: Any, ttl_seconds: int) -> CacheEntry:
        now = datetime.now(timezone.utc)
        entry = CacheEntry(value=value, retrieved_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
        async with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._evict()
        return entry

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def _evict(self) -> None:
        for key in [key for key, entry in self._entries.items() if entry.stale]:
            del self._entries[key]
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1

    def status(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {"available": True, "entries": len(self._entries), "max_entries": self._max_entries,
                "evictions": self.evictions, "hit_rate": self.hits / total if total else 0.0}


weather_cache = TTLCache(settings.weather_cache_max_entries)
