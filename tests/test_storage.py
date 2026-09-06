"""Storage contracts: the seam that makes Redis/Postgres a config change, not a rewrite."""
import asyncio
import dataclasses
import importlib
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.storage.base import ContextLimitExceeded, ConversationLog, MemoryStore, SessionStore
from app.storage.memory import InMemorySessionStore
from app.storage.sqlite import SqliteConversationLog, SqliteMemoryStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A MemoryStore/ConversationLog pair on a throwaway database file."""
    monkeypatch.setattr("app.context.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.context.store._initialized", False)
    return SqliteMemoryStore(), SqliteConversationLog()


def test_implementations_satisfy_their_protocols():
    """The annotations are checked statically by mypy; this checks the surface at runtime,
    so a Protocol gaining a method cannot silently leave an implementation behind."""
    session: SessionStore = InMemorySessionStore(8, 60)
    memory: MemoryStore = SqliteMemoryStore()
    log: ConversationLog = SqliteConversationLog()
    for implementation, protocol in ((session, SessionStore), (memory, MemoryStore),
                                     (log, ConversationLog)):
        required = [name for name in vars(protocol) if not name.startswith("_")]
        assert required, f"{protocol.__name__} declares no methods"
        for name in required:
            assert callable(getattr(implementation, name, None)), \
                f"{type(implementation).__name__} is missing {name}()"


def test_session_round_trip_and_drop():
    store = InMemorySessionStore(max_entries=8, ttl_seconds=60)

    async def run():
        assert await store.get("missing") is None
        await store.put("s1", {"location": "Indore", "turns": 2})
        assert await store.get("s1") == {"location": "Indore", "turns": 2}
        await store.drop("s1")
        assert await store.get("s1") is None
        await store.drop("s1")  # dropping twice is not an error

    asyncio.run(run())


def test_session_expires_on_ttl():
    store = InMemorySessionStore(max_entries=8, ttl_seconds=0)

    async def run():
        await store.put("s1", {"a": 1})
        assert await store.get("s1") is None

    asyncio.run(run())


def test_session_store_is_lru_bounded():
    store = InMemorySessionStore(max_entries=2, ttl_seconds=60)

    async def run():
        for key in ("a", "b", "c"):
            await store.put(key, {"k": key})
        assert await store.get("a") is None      # evicted, oldest first
        assert await store.get("c") == {"k": "c"}
        assert store.status()["entries"] == 2

    asyncio.run(run())


def test_memory_store_round_trip(store):
    memory, _ = store
    memory.upsert_fact("u1", "risk_tolerance", "low")
    assert memory.get_context("u1")["risk_tolerance"]["value"] == "low"
    memory.upsert_fact("u1", "risk_tolerance", "high")
    assert memory.get_context("u1")["risk_tolerance"]["value"] == "high"  # overwrites
    assert memory.get_context("nobody") == {}


def test_memory_store_still_filters_expired_facts_by_instant(store):
    """Guards the `_expired` fix: '+05:30' and '+00:00' do not sort as strings."""
    memory, _ = store
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    memory.upsert_fact("u1", "stale", "x", expiry=past)
    memory.upsert_fact("u1", "fresh", "y", expiry=future)
    memory.upsert_fact("u1", "forever", "z")
    assert set(memory.get_context("u1")) == {"fresh", "forever"}


def test_2_7_overwriting_an_existing_fact_never_counts_against_the_per_user_cap(store, monkeypatch):
    memory, _ = store
    monkeypatch.setattr("app.context.store.settings", dataclasses.replace(settings, context_max_facts_per_user=1))
    memory.upsert_fact("u1", "risk_tolerance", "low")
    memory.upsert_fact("u1", "risk_tolerance", "high")  # same fact name: overwrite, not growth
    assert memory.get_context("u1")["risk_tolerance"]["value"] == "high"


def test_2_7_a_new_fact_past_the_per_user_cap_is_rejected(store, monkeypatch):
    memory, _ = store
    monkeypatch.setattr("app.context.store.settings", dataclasses.replace(settings, context_max_facts_per_user=1))
    memory.upsert_fact("u1", "risk_tolerance", "low")
    with pytest.raises(ContextLimitExceeded):
        memory.upsert_fact("u1", "another_fact", "value")
    assert set(memory.get_context("u1")) == {"risk_tolerance"}


def test_2_7_an_oversized_value_is_rejected(store, monkeypatch):
    memory, _ = store
    monkeypatch.setattr("app.context.store.settings", dataclasses.replace(settings, context_value_max_chars=8))
    with pytest.raises(ContextLimitExceeded):
        memory.upsert_fact("u1", "essay", "x" * 9)
    assert memory.get_context("u1") == {}


def test_feedback_is_accepted(store):
    memory, _ = store
    memory.add_feedback("u1", "spray", "2.4mm", "dry", "was fine")


def test_conversation_log_preserves_spoken_order_and_limit(store):
    _, log = store
    for index in range(5):
        log.append("s1", {"role": "user", "text": f"turn {index}"})
    assert [t["text"] for t in log.recent("s1", 10)] == [f"turn {i}" for i in range(5)]
    # limit takes the LATEST turns, returned oldest-first
    assert [t["text"] for t in log.recent("s1", 2)] == ["turn 3", "turn 4"]
    assert log.recent("other-session", 10) == []


def test_conversation_log_payload_shape_is_opaque(store):
    """A turn can gain fields — a transcript, for one — without a migration."""
    _, log = store
    log.append("s1", {"role": "user", "text": "hi", "transcript": {"engine": "x"}, "n": 1})
    assert log.recent("s1", 1)[0]["transcript"] == {"engine": "x"}


@pytest.mark.parametrize("field,value,expected", [
    ("session_backend", "redis", "SESSION_BACKEND"),
    ("db_backend", "postgres", "DB_BACKEND"),
])
def test_unimplemented_backend_fails_loudly_at_import(monkeypatch, field, value, expected):
    import app.storage
    monkeypatch.setattr("app.config.settings", dataclasses.replace(settings, **{field: value}))
    with pytest.raises(ValueError) as excinfo:
        importlib.reload(app.storage)
    assert expected in str(excinfo.value) and value in str(excinfo.value)
    monkeypatch.undo()
    importlib.reload(app.storage)


def test_storage_layer_never_imports_a_weather_type():
    """Layering asserted, not merely intended: storage must not learn what a forecast is."""
    forbidden = re.compile(r"^\s*from\s+app\.(schemas|services\.(wio_builder|ranker|retrieval))",
                           re.MULTILINE)
    for path in pathlib.Path("app/storage").glob("*.py"):
        assert not forbidden.search(path.read_text()), f"{path} imports a weather type"
