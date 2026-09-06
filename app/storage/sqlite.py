"""SQLite-backed MemoryStore and ConversationLog — SqliteMemoryStore is a thin delegation to context/store.py (already handles connection lifetime, expiry-as-instants, schema creation) so there's one set of SQL and semantics."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app.context import store


class SqliteMemoryStore:
    def upsert_fact(self, user_id: str, fact: str, value: Any, confidence: float = 0.9,
                    source: str = "user", confirmed: bool = True,
                    expiry: str | None = None) -> None:
        store.upsert_fact(user_id, fact, value, confidence, source, confirmed, expiry)

    def get_context(self, user_id: str) -> dict[str, Any]:
        return store.get_context(user_id)

    def add_feedback(self, user_id: str, decision: str, forecast: str, actual: str,
                     feedback: str) -> None:
        store.add_feedback(user_id, decision, forecast, actual, feedback)


class SqliteConversationLog:
    """Turn payloads are stored as JSON text — the log must not know a turn's shape, so the router can add fields (e.g. a transcript) without a migration here."""

    def append(self, session_id: str, turn: dict[str, Any]) -> None:
        with closing(store._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO conversation_turns (session_id, payload, created_at) VALUES (?,?,?)",
                (session_id, json.dumps(turn, default=str), datetime.now(timezone.utc).isoformat()),
            )

    def recent(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        with closing(store._connect()) as conn:
            rows = conn.execute(
                "SELECT payload FROM conversation_turns WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        # Newest-first from SQL so LIMIT takes the latest turns; reversed so callers read them in the order they were spoken.
        return [json.loads(payload) for (payload,) in reversed(rows)]
