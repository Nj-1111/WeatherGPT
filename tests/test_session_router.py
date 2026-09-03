"""Conversational follow-up short-circuit: reuse a session's last resolved location/time
window when a follow-up names no new location, and never when it does."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.schemas.location import ResolvedLocation
from app.schemas.query import NormalizedQuery
from app.services import session_router
from app.storage.memory import InMemorySessionStore

MUMBAI = ResolvedLocation(raw="Mumbai", lat=19.076, lon=72.8777, timezone="Asia/Kolkata",
                          normalized_name="Mumbai")


def _query(location: str | None, text: str = "weather") -> NormalizedQuery:
    return NormalizedQuery(original_text=text, normalized_location=location,
                           is_weather_related=True, confidence_score=0.9)


def _window():
    now = datetime.now(timezone.utc)
    return now, now + timedelta(hours=6)


def test_a_first_query_has_no_context_yet(monkeypatch):
    monkeypatch.setattr(session_router, "_store", InMemorySessionStore(8, 300))
    result = asyncio.run(session_router.evaluate_follow_up("s1", _query("Mumbai", "weather in Mumbai")))
    assert result is None


def test_b_location_less_follow_up_reuses_stored_context(monkeypatch):
    monkeypatch.setattr(session_router, "_store", InMemorySessionStore(8, 300))
    valid_from, valid_to = _window()
    asyncio.run(session_router.store_context("s1", MUMBAI, valid_from, valid_to, "short", 0.9))

    result = asyncio.run(session_router.evaluate_follow_up("s1", _query(None, "how about the wind?")))
    assert result is not None
    assert result.resolved_location_name == "Mumbai"
    assert result.resolved_lat == MUMBAI.lat and result.resolved_lon == MUMBAI.lon
    assert result.horizon == "short"


def test_c_explicit_new_location_bypasses_cache(monkeypatch):
    monkeypatch.setattr(session_router, "_store", InMemorySessionStore(8, 300))
    valid_from, valid_to = _window()
    asyncio.run(session_router.store_context("s1", MUMBAI, valid_from, valid_to, "short", 0.9))

    result = asyncio.run(session_router.evaluate_follow_up("s1", _query("Delhi", "what about Delhi?")))
    assert result is None


def test_d_expired_context_is_not_reused(monkeypatch):
    # A negative TTL means every entry is already stale the instant it's written — the
    # deterministic equivalent of "6 minutes have passed" against a 5-minute TTL, without
    # sleeping in the test or mocking wall-clock time.
    monkeypatch.setattr(session_router, "_store", InMemorySessionStore(8, -1))
    valid_from, valid_to = _window()
    asyncio.run(session_router.store_context("s1", MUMBAI, valid_from, valid_to, "short", 0.9))

    result = asyncio.run(session_router.evaluate_follow_up("s1", _query(None, "will it stop?")))
    assert result is None


def test_different_session_ids_do_not_share_context(monkeypatch):
    monkeypatch.setattr(session_router, "_store", InMemorySessionStore(8, 300))
    valid_from, valid_to = _window()
    asyncio.run(session_router.store_context("s1", MUMBAI, valid_from, valid_to, "short", 0.9))

    result = asyncio.run(session_router.evaluate_follow_up("s2", _query(None, "how about the wind?")))
    assert result is None
