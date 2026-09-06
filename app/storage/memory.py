"""In-process SessionStore over services/cache.py's TTLCache (already LRU-bounded with expiry swept on write) rather than a second eviction implementation; per-process, so a follow-up on another worker just looks like a new query — same constraint evidence_store carries, removed by promoting to Redis."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.cache import TTLCache


class InMemorySessionStore:
    def __init__(self, max_entries: int, ttl_seconds: int) -> None:
        self._cache = TTLCache(max_entries)
        self._ttl = ttl_seconds

    async def get(self, session_id: str) -> dict[str, Any] | None:
        entry = await self._cache.get(session_id)
        return entry.value if entry is not None else None

    async def put(self, session_id: str, payload: dict[str, Any]) -> None:
        await self._cache.put(session_id, payload, self._ttl)

    async def drop(self, session_id: str) -> None:
        await self._cache.delete(session_id)

    def status(self) -> dict[str, Any]:
        return {**self._cache.status(), "backend": "memory", "ttl_seconds": self._ttl}


def build_session_store() -> InMemorySessionStore:
    return InMemorySessionStore(settings.session_max_entries, settings.session_ttl_seconds)
