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
from app.schemas.query import GuardrailDecision, ResolvedContext
from app.storage.memory import InMemorySessionStore

_store = InMemorySessionStore(settings.follow_up_context_max_entries, settings.follow_up_context_ttl_seconds)
_verify_store = InMemorySessionStore(settings.verify_pending_max_entries, settings.verify_pending_ttl_seconds)
_disambiguation_store = InMemorySessionStore(settings.verify_pending_max_entries, settings.verify_pending_ttl_seconds)


async def evaluate_follow_up(session_id: str, decision: GuardrailDecision) -> ResolvedContext | None:
    """None means "resolve normally": a new explicit location was named, or nothing (or
    nothing unexpired) is stored for this session."""
    if decision.location:
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


async def store_pending_verification(session_id: str, original_text: str, candidate: str) -> None:
    await _verify_store.put(session_id, {"original_text": original_text, "candidate": candidate})


async def consume_pending_verification(session_id: str) -> tuple[str, str] | None:
    """Reads and clears in one call — a pending verification only ever applies to the
    single next turn, confirmed or not."""
    payload = await _verify_store.get(session_id)
    if payload is None:
        return None
    await _verify_store.drop(session_id)
    return payload["original_text"], payload["candidate"]


async def store_pending_disambiguation(session_id: str, original_text: str, candidates: list[dict]) -> None:
    await _disambiguation_store.put(session_id, {"original_text": original_text, "candidates": candidates})


async def consume_pending_disambiguation(session_id: str) -> tuple[str, list[dict]] | None:
    """Reads and clears in one call — a pending disambiguation only ever applies to the
    single next turn, matched or not."""
    payload = await _disambiguation_store.get(session_id)
    if payload is None:
        return None
    await _disambiguation_store.drop(session_id)
    return payload["original_text"], payload["candidates"]


def status() -> dict:
    return _store.status()
