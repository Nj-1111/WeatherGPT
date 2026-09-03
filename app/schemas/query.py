"""Output contract for the LLM-based query extraction funnel (see services/query_extractor.py).

Fields are intentionally minimal — this is what the extractor produces, not a place to
accumulate downstream state.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class QueryIntent(str, Enum):
    CURRENT = "current"
    FORECAST = "forecast"
    ALERTS = "alerts"
    HISTORICAL = "historical"
    UNKNOWN = "unknown"


ExtractionSource = Literal["llm", "deterministic_fallback"]


class NormalizedQuery(BaseModel):
    original_text: str
    normalized_location: str | None = None
    normalized_time: str | None = None
    intent: QueryIntent = QueryIntent.UNKNOWN
    is_weather_related: bool = True
    confidence_score: float = Field(ge=0.0, le=1.0, default=0.0)
    # Observability: which code path produced this read, for cost/latency tracking in
    # production. Defaults to the fallback because that's correct whenever construction
    # short-circuits before the LLM path explicitly marks itself "llm".
    extraction_source: ExtractionSource = "deterministic_fallback"


class ResolvedContext(BaseModel):
    """The last successful query's resolved location + time window, stored per session_id
    (services/session_router.py) so a location-less follow-up can skip geocoding and time
    parsing entirely rather than fail with LOCATION_REQUIRED."""
    resolved_lat: float
    resolved_lon: float
    resolved_location_name: str
    timezone: str | None = None
    valid_from: datetime
    valid_to: datetime
    horizon: str
    time_confidence: float = Field(ge=0.0, le=1.0, default=0.9)
