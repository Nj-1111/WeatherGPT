"""Storage factory — the one place a backend is chosen.

`SESSION_BACKEND` and `DB_BACKEND` exist so promotion is a config change. Only the
in-process values are implemented today, and an unsupported value raises here at import
rather than falling back silently: a switch that quietly ignores `redis` is worse than no
switch, because it fails in production looking like it worked.
"""
from __future__ import annotations

from app.config import settings
from app.storage.base import ConversationLog, MemoryStore, SessionStore
from app.storage.memory import build_session_store
from app.storage.sqlite import SqliteConversationLog, SqliteMemoryStore

__all__ = ["ConversationLog", "MemoryStore", "SessionStore",
           "conversation_log", "memory_store", "session_store"]

_SUPPORTED_SESSION_BACKENDS = {"memory"}
_SUPPORTED_DB_BACKENDS = {"sqlite"}


def _check(name: str, value: str, supported: set[str], planned: str) -> None:
    if value not in supported:
        raise ValueError(
            f"{name}={value!r} is not implemented. Supported: {sorted(supported)}. "
            f"{planned} is a planned promotion, not yet wired."
        )


_check("SESSION_BACKEND", settings.session_backend, _SUPPORTED_SESSION_BACKENDS, "redis")
_check("DB_BACKEND", settings.db_backend, _SUPPORTED_DB_BACKENDS, "postgres")

session_store: SessionStore = build_session_store()
memory_store: MemoryStore = SqliteMemoryStore()
conversation_log: ConversationLog = SqliteConversationLog()
