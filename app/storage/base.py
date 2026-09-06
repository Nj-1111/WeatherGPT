"""Storage contracts — three narrow Protocols (not base classes, since TTLCache predates this layer) so the backing store can be promoted (memory to Redis, SQLite to Postgres) without a caller changing. Interfaces speak JSON-serialisable dicts, not domain objects, and never import a weather type."""
from __future__ import annotations

from typing import Any, Protocol


class ContextLimitExceeded(Exception):
    """Raised by MemoryStore.upsert_fact past a per-user guard (fact count or value size); lives here, not a concrete backend module, so callers catch one type regardless of implementation."""

    def __init__(self, user_id: str, message: str) -> None:
        self.user_id = user_id
        super().__init__(message)


class SessionStore(Protocol):
    """Hot, expiring conversation state — lossy by design, a dropped session costs a re-fetch, never a wrong answer."""

    async def get(self, session_id: str) -> dict[str, Any] | None: ...

    async def put(self, session_id: str, payload: dict[str, Any]) -> None: ...

    async def drop(self, session_id: str) -> None: ...

    def status(self) -> dict[str, Any]: ...


class MemoryStore(Protocol):
    """Durable user facts — the shape deliberately matches context/store.py's existing signatures; an abstraction isn't a licence to redesign a working store."""

    def upsert_fact(self, user_id: str, fact: str, value: Any, confidence: float = 0.9,
                    source: str = "user", confirmed: bool = True,
                    expiry: str | None = None) -> None: ...

    def get_context(self, user_id: str) -> dict[str, Any]: ...

    def add_feedback(self, user_id: str, decision: str, forecast: str, actual: str,
                     feedback: str) -> None: ...


class ConversationLog(Protocol):
    """Append-only turns, never mutated, so it stays correct under concurrent writers and can replay a session the hot store has already expired."""

    def append(self, session_id: str, turn: dict[str, Any]) -> None: ...

    def recent(self, session_id: str, limit: int) -> list[dict[str, Any]]: ...
