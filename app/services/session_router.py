"""Conversational follow-up short-circuit.

A follow-up like "and how soon will that rain stop?" names no location — today that
means extract_location(question) finds nothing and the request 422s with
LOCATION_REQUIRED, even though the location was just resolved one turn ago. This module
lets main.py reuse that resolution (and the time window it was computed for) instead of
either failing or re-running geocoding.

Backed by InMemorySessionStore — the same bounded TTL-cache primitive app/storage/memory.py
already wraps around TTLCache — rather than a bare dict, so this doesn't reintroduce the
unbounded-growth failure mode the 2026-09 hardening pass fixed elsewhere (see CLAUDE.md).
A *separate* instance from app/storage's `session_store` singleton: follow-up context needs
its own short, fixed TTL (a stale location silently answering a new question is worse than
a cache miss), independent of whatever TTL general conversation state carries.
"""
from __future__ import annotations

from datetime import datetime

from app.config import settings
from app.schemas.location import ResolvedLocation
from app.schemas.query import NormalizedQuery, ResolvedContext
from app.storage.memory import InMemorySessionStore

_store = InMemorySessionStore(settings.follow_up_context_max_entries, settings.follow_up_context_ttl_seconds)


async def evaluate_follow_up(session_id: str, normalized_query: NormalizedQuery) -> ResolvedContext | None:
    """None means "resolve normally": a new explicit location was named, or nothing (or
    nothing unexpired) is stored for this session."""
    if normalized_query.normalized_location:
        return None
    payload = await _store.get(session_id)
    if payload is None:
        return None
    return ResolvedContext.model_validate(payload)


async def store_context(session_id: str, location: ResolvedLocation, valid_from: datetime,
                        valid_to: datetime, horizon: str, time_confidence: float) -> None:
    context = ResolvedContext(
        resolved_lat=location.lat,
        resolved_lon=location.lon,
        resolved_location_name=location.normalized_name or location.raw,
        timezone=location.timezone,
        valid_from=valid_from,
        valid_to=valid_to,
        horizon=horizon,
        time_confidence=time_confidence,
    )
    await _store.put(session_id, context.model_dump(mode="json"))


def status() -> dict:
    return _store.status()
