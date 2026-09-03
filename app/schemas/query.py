"""Output contract for the LLM-based query extraction funnel (see services/query_extractor.py).

Fields are intentionally minimal — this is what the extractor produces, not a place to
accumulate downstream state.
"""
from __future__ import annotations

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
