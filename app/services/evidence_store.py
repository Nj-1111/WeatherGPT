"""Process-local evidence index backing GET /evidence/{id} — bounded and TTL-expiring since this grows ~96 entries per day-ahead query, and entries outlive a request only long enough for a user to follow a citation, not forever."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.schemas.ceo import CanonicalEvidenceObject


class EvidenceStore:
    """Deliberately keyed by server-generated IDs only."""

    def __init__(self, max_entries: int, ttl_seconds: int) -> None:
        self._items: OrderedDict[str, tuple[datetime, CanonicalEvidenceObject]] = OrderedDict()
        self._max_entries = max_entries
        self._ttl = timedelta(seconds=ttl_seconds)

    def add_many(self, items: list[CanonicalEvidenceObject]) -> None:
        expires_at = datetime.now(timezone.utc) + self._ttl
        for item in items:
            self._items[item.evidence_id] = (expires_at, item)
            self._items.move_to_end(item.evidence_id)
        self._evict()

    def get(self, evidence_id: str) -> CanonicalEvidenceObject | None:
        record = self._items.get(evidence_id)
        if record is None:
            return None
        expires_at, item = record
        if datetime.now(timezone.utc) > expires_at:
            del self._items[evidence_id]
            return None
        return item

    def _evict(self) -> None:
        now = datetime.now(timezone.utc)
        for key in [key for key, (expires_at, _) in self._items.items() if now > expires_at]:
            del self._items[key]
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)

    def status(self) -> dict[str, int]:
        return {"entries": len(self._items), "max_entries": self._max_entries}


evidence_store = EvidenceStore(settings.evidence_store_max_entries, settings.evidence_store_ttl_seconds)
