"""Concurrent, isolated source retrieval: one dead source can never fail a request — every failure is caught per source and reported as data in `retrieval_status`, never raised."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.adapters.registry import REGISTRY
from app.config import settings
from app.orchestrator.retrieval_planner import RetrievalPlan
from app.schemas.ceo import CanonicalEvidenceObject
from app.services.cache import weather_cache

logger = logging.getLogger(__name__)

# Reanalysis of a past date is immutable but a live forecast isn't, so each gets its own TTL.
_CACHE_TTL_BY_SOURCE = {
    "ERA5": settings.historical_cache_ttl_seconds,
    "NASA_POWER": settings.historical_cache_ttl_seconds,
    "CAP": settings.warning_cache_ttl_seconds,
    "IMD": settings.warning_cache_ttl_seconds,
}


class CircuitBreaker:
    """Stops re-dialling a reliably-failing source, so it doesn't cost a full timeout per request."""

    def __init__(self, threshold: int, reset_seconds: float) -> None:
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, source: str) -> bool:
        opened = self._opened_at.get(source)
        if opened is None:
            return False
        if time.monotonic() - opened >= self._reset_seconds:
            # Half-open: allow one trial request through to see if the source recovered.
            del self._opened_at[source]
            self._failures[source] = self._threshold - 1
            return False
        return True

    def record_success(self, source: str) -> None:
        self._failures.pop(source, None)
        self._opened_at.pop(source, None)

    def record_failure(self, source: str) -> None:
        count = self._failures.get(source, 0) + 1
        self._failures[source] = count
        if count >= self._threshold:
            self._opened_at[source] = time.monotonic()
            logger.warning("retrieval.circuit_opened", extra={"source": source, "failures": count})

    def reset(self) -> None:
        self._failures.clear()
        self._opened_at.clear()


breaker = CircuitBreaker(settings.circuit_breaker_threshold, settings.circuit_breaker_reset_seconds)


def _is_retryable(exc: BaseException) -> bool:
    """A timeout or 5xx may succeed on retry; a 4xx or missing credential won't and only wastes budget."""
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


def _safe_error_reason(exc: Exception) -> str:
    """Fixed, user-safe reason for retrieval_status — never exc's own text, which can leak URLs/hostnames; full detail stays in the (non-user-facing) log line at the call site."""
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "source timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return "source rate-limited" if exc.response.status_code == 429 else "source returned an error response"
    if isinstance(exc, httpx.TransportError):
        return "source unreachable"
    return "source request failed"


def _cache_key(source: str, kwargs: dict[str, Any]) -> str:
    precision = settings.cache_key_precision
    payload = {"source": source,
               **{key: (round(value, precision) if key in {"lat", "lon"} else value)
                  for key, value in sorted(kwargs.items())}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _source_kwargs(source: str, lat: float, lon: float, valid_from, valid_to) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"lat": lat, "lon": lon}
    if source in {"OPEN_METEO", "GEFS", "MET_NORWAY"}:
        today = datetime.now(valid_to.tzinfo).date()
        kwargs["forecast_days"] = min(16, max(1, (valid_to.date() - today).days + 2))
    elif source == "ERA5":
        kwargs.update(start_date=valid_from.date().isoformat(), end_date=valid_to.date().isoformat())
    elif source == "NASA_POWER":
        kwargs.update(start=valid_from.strftime("%Y%m%d"), end=valid_to.strftime("%Y%m%d"))
    elif source == "GFS":
        kwargs["valid_from"] = valid_from.isoformat()
    return kwargs


def _wanted(items: list[CanonicalEvidenceObject], plan: RetrievalPlan) -> list[CanonicalEvidenceObject]:
    """Keep only what the plan asked for; warnings are governed by need_warnings, so never filtered out here."""
    if not plan.variables:
        return items
    wanted = set(plan.variables)
    return [item for item in items
            if item.variable.value in wanted or item.evidence_class.value == "warning"]


async def _fetch_with_retry(adapter: Any, kwargs: dict[str, Any]) -> Any:
    attempts = max(1, settings.source_retries + 1)
    for attempt in range(attempts):
        try:
            return await asyncio.wait_for(adapter.fetch(**kwargs), timeout=settings.source_timeout_seconds)
        except Exception as exc:
            if attempt == attempts - 1 or not _is_retryable(exc):
                raise
            await asyncio.sleep(settings.source_retry_backoff_seconds * (2 ** attempt))
    raise RuntimeError("unreachable")


async def _one(source: str, lat: float, lon: float, plan: RetrievalPlan, valid_from, valid_to):
    adapter = REGISTRY.get(source)
    if adapter is None:
        return source, [], "source is not configured", False
    if breaker.is_open(source):
        return source, [], "circuit open after repeated failures", False

    kwargs = _source_kwargs(source, lat, lon, valid_from, valid_to)
    cache_key = _cache_key(source, kwargs)
    cached = await weather_cache.get(cache_key)
    if cached is not None:
        return source, _wanted(cached.value, plan), None, True

    try:
        raw = await _fetch_with_retry(adapter, kwargs)
        items = adapter.normalize(raw, **kwargs)
        retrieved = datetime.now(timezone.utc)
        for item in items:
            item.retrieval_timestamp = item.retrieval_timestamp or retrieved
        ttl = _CACHE_TTL_BY_SOURCE.get(source, settings.forecast_cache_ttl_seconds)
        await weather_cache.put(cache_key, items, ttl)
        breaker.record_success(source)
        return source, _wanted(items, plan), None, False
    except Exception as exc:
        breaker.record_failure(source)
        logger.warning("retrieval.source_failed",
                       extra={"source": source, "error": type(exc).__name__, "detail": str(exc)[:200]})
        return source, [], _safe_error_reason(exc), False


async def retrieve(plan: RetrievalPlan, *, lat: float, lon: float, valid_from, valid_to) -> tuple[list[CanonicalEvidenceObject], dict[str, Any]]:
    results = await asyncio.gather(
        *[_one(source, lat, lon, plan, valid_from, valid_to) for source in plan.sources]
    )
    evidence: list[CanonicalEvidenceObject] = []
    status: dict[str, Any] = {"sources": {}, "partial": False}
    for source, items, error, cached in results:
        evidence.extend(items)
        status["sources"][source] = {"status": "ok" if not error else "unavailable",
                                     "count": len(items), "error": error, "cached": cached}
        if error is not None:
            status["partial"] = True
    return evidence, status
