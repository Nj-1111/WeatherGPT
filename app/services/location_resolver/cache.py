"""Location-only TTL cache — a separate instance from weather_cache (not implementation) since location facts are stable for weeks while forecasts expire in minutes; swappable for Redis later without touching the resolver."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.cache import TTLCache


class LocationCache(TTLCache):
    """Returns the stored value directly; callers here have no use for freshness metadata."""

    async def get(self, key: str, allow_stale: bool = False) -> Any | None:
        entry = await super().get(key, allow_stale)
        return entry.value if entry is not None else None


location_cache = LocationCache(settings.location_cache_max_entries)
