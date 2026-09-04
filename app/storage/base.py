"""Storage contracts.

Three narrow interfaces so the backing store can be promoted — memory to Redis, SQLite to
Postgres — without a caller changing. Protocols rather than base classes: the existing
`TTLCache` predates this layer and must not be forced to inherit anything.

**These interfaces speak JSON-serialisable dicts, not domain objects.** Redis stores
strings and Postgres stores jsonb, so a typed-model interface would need a serialisation
layer bolted on at promotion time anyway. Speaking dicts now is what makes promotion a
config change rather than a rewrite.

Nothing here may import a weather type. Storage never learns what a forecast is; callers
serialise into it and parse back out.
"""
from __future__ import annotations

from typing import Any, Protocol


class SessionStore(Protocol):
    """Hot, expiring conversation state. Lossy by design — a dropped session costs a
    re-fetch, never a wrong answer."""

    async def get(self, session_id: str) -> dict[str, Any] | None: ...

    async def put(self, session_id: str, payload: dict[str, Any]) -> None: ...

    async def drop(self, session_id: str) -> None: ...

    def status(self) -> dict[str, Any]: ...


class MemoryStore(Protocol):
    """Durable user facts. The shape is deliberately the signatures that already exist in
    `context/store.py` — an abstraction is not a licence to redesign a working store."""

    def upsert_fact(self, user_id: str, fact: str, value: Any, confidence: float = 0.9,
                    source: str = "user", confirmed: bool = True,
                    expiry: str | None = None) -> None: ...

    def get_context(self, user_id: str) -> dict[str, Any]: ...

    def add_feedback(self, user_id: str, decision: str, forecast: str, actual: str,
                     feedback: str) -> None: ...


class ConversationLog(Protocol):
    """Append-only turns. Never mutated, so it stays correct under concurrent writers and
    can be replayed to reconstruct a session the hot store has already expired."""

    def append(self, session_id: str, turn: dict[str, Any]) -> None: ...

    def recent(self, session_id: str, limit: int) -> list[dict[str, Any]]: ...
