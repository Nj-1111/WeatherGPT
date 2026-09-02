"""Bounds and expiry on the process-local stores — the difference between a
long-running service and one that leaks until it is killed."""
import asyncio
from datetime import datetime, timedelta, timezone

from app.schemas.ceo import CanonicalEvidenceObject, Provenance
from app.services.cache import TTLCache
from app.services.evidence_store import EvidenceStore
from app.services.location_resolver.cache import LocationCache


def _ceo() -> CanonicalEvidenceObject:
    return CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast", variable="temperature_2m",
                                   value=25.0, unit="C", statistic="instant",
                                   provenance=Provenance(original_source="test"))


def _expire(cache: TTLCache, key: str) -> None:
    cache._entries[key].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_cache_evicts_least_recently_used_beyond_bound():
    async def scenario():
        cache = TTLCache(max_entries=3)
        for index in range(3):
            await cache.put(f"k{index}", index, 60)
        await cache.get("k0")  # k1 becomes the least recently used
        await cache.put("k3", 3, 60)
        assert len(cache._entries) == 3
        assert await cache.get("k1") is None
        assert (await cache.get("k0")).value == 0
        assert cache.evictions == 1
    asyncio.run(scenario())


def test_expired_entry_is_removed_not_merely_reported_as_a_miss():
    async def scenario():
        cache = TTLCache(max_entries=10)
        await cache.put("k", "v", ttl_seconds=60)
        _expire(cache, "k")
        assert await cache.get("k") is None
        assert "k" not in cache._entries
    asyncio.run(scenario())


def test_stale_entry_is_still_readable_when_explicitly_allowed():
    async def scenario():
        cache = TTLCache(max_entries=10)
        await cache.put("k", "v", ttl_seconds=60)
        _expire(cache, "k")
        entry = await cache.get("k", allow_stale=True)
        assert entry is not None and entry.value == "v" and entry.stale
    asyncio.run(scenario())


def test_put_sweeps_expired_entries():
    async def scenario():
        cache = TTLCache(max_entries=10)
        await cache.put("old", "v", ttl_seconds=60)
        _expire(cache, "old")
        await cache.put("new", "v", ttl_seconds=60)
        assert "old" not in cache._entries
    asyncio.run(scenario())


def test_location_cache_returns_the_value_not_the_entry():
    async def scenario():
        cache = LocationCache(max_entries=2)
        await cache.put("patna", {"lat": 25.6}, 60)
        assert await cache.get("patna") == {"lat": 25.6}
        assert await cache.get("absent") is None
    asyncio.run(scenario())


def test_evidence_store_is_bounded():
    store = EvidenceStore(max_entries=5, ttl_seconds=60)
    items = [_ceo() for _ in range(8)]
    store.add_many(items)
    assert store.status()["entries"] == 5
    assert store.get(items[0].evidence_id) is None, "oldest evidence should have been evicted"
    assert store.get(items[-1].evidence_id) is not None


def test_evidence_store_expires_entries():
    store = EvidenceStore(max_entries=100, ttl_seconds=0)
    item = _ceo()
    store.add_many([item])
    assert store.get(item.evidence_id) is None
