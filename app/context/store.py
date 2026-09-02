"""User context store — SQLite per user_id, fact with provenance."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

DB_PATH = Path(settings.database_path)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS user_context (
        user_id TEXT, fact TEXT, value TEXT, confidence REAL, source TEXT,
        created_at TEXT, updated_at TEXT, confirmed INTEGER, expiry TEXT,
        PRIMARY KEY (user_id, fact)
    )""",
    """CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, decision TEXT,
        forecast TEXT, actual TEXT, feedback TEXT, timestamp TEXT
    )""",
)

_initialized = False


def _connect() -> sqlite3.Connection:
    """Schema is created on first use rather than at import, so importing the module
    never writes to disk.

    `with conn` only commits; it does not close. Callers wrap this in closing().
    """
    global _initialized
    conn = sqlite3.connect(DB_PATH)
    if not _initialized:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.commit()
        _initialized = True
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(expiry: str | None) -> bool:
    """Compare as instants, not strings: '+05:30' and '+00:00' do not sort correctly."""
    if not expiry:
        return False
    try:
        parsed = datetime.fromisoformat(expiry)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc)


def upsert_fact(user_id: str, fact: str, value: Any, confidence: float = 0.9, source: str = "user",
                confirmed: bool = True, expiry: str | None = None) -> None:
    # No conflict resolution: a repeat fact overwrites unconditionally, regardless of
    # the stored confidence.
    now = _now()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_context "
            "(user_id,fact,value,confidence,source,created_at,updated_at,confirmed,expiry) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, fact, str(value), confidence, source, now, now, int(confirmed), expiry),
        )


def get_context(user_id: str) -> dict[str, Any]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT fact,value,confidence,source,confirmed,expiry FROM user_context WHERE user_id=?",
            (user_id,),
        ).fetchall()
    return {fact: {"value": value, "confidence": confidence, "source": source, "confirmed": bool(confirmed)}
            for fact, value, confidence, source, confirmed, expiry in rows if not _expired(expiry)}


def add_feedback(user_id: str, decision: str, forecast: str, actual: str, feedback: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO feedback (user_id,decision,forecast,actual,feedback,timestamp) VALUES (?,?,?,?,?,?)",
            (user_id, decision, forecast, actual, feedback, _now()),
        )
