"""Storage factory — the one place a backend is chosen. SESSION_BACKEND/DB_BACKEND exist so promotion is a config change; only the in-process values are implemented today, and an unsupported value raises loudly at import rather than silently falling back."""
from __future__ import annotations

from app.config import settings
from app.storage.base import ContextLimitExceeded, ConversationLog, MemoryStore, SessionStore
from app.storage.sqlite import SqliteConversationLog, SqliteMemoryStore

__all__ = ["ContextLimitExceeded", "ConversationLog", "MemoryStore", "SessionStore",
           "conversation_log", "memory_store"]

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

memory_store: MemoryStore = SqliteMemoryStore()
conversation_log: ConversationLog = SqliteConversationLog()
