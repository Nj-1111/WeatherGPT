"""Output contract for the guardrail decision funnel (see services/query_guardrail.py).

Fields are intentionally minimal — this is what the guardrail produces, not a place to
accumulate downstream state.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

ExtractionSource = Literal["llm", "deterministic_fallback", "confirmed"]


class GuardrailAction(str, Enum):
    """What the guardrail decided to do with a query — the real dispatch key. Every value
    maps to a distinct code path in the request handler: ACCEPT_LOCATION_ONLY calls only
    the location resolver; ACCEPT_WEATHER_FULL runs the full pipeline; the other four
    return a rendered message immediately with no further calls at all.
    """
    ACCEPT_LOCATION_ONLY = "accept_location_only"
    ACCEPT_WEATHER_FULL = "accept_weather_full"
    REJECT_OFF_TOPIC = "reject_off_topic"
    CLARIFY = "clarify"
    VERIFY = "verify"
    # Recognized as a real question, but about a hazard this system has no data for
    # (earthquake, tsunami, wildfire, ...) — distinct from REJECT_OFF_TOPIC, which means
    # the question isn't a weather/disaster/location question at all. Answering with
    # generic weather data here would be wrong, not just unhelpful.
    UNSUPPORTED_TOPIC = "unsupported_topic"


class ClarifyReason(str, Enum):
    GARBLED_INPUT = "garbled_input"
    NO_LOCATION = "no_location"


class GuardrailDecision(BaseModel):
    """Output of services/query_guardrail.py. A strict decision-tree classification,
    not a graded judgment — see that module's system prompt for the exact rules the LLM
    (or the deterministic fallback) applies, in order, to reach one action."""
    original_text: str
    action: GuardrailAction
    location: str | None = None
    time: str | None = None
    verify_candidate: str | None = None
    clarify_reason: ClarifyReason | None = None
    unsupported_topic: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    extraction_source: ExtractionSource = "deterministic_fallback"
    # ISO 639-1 code, a pure function of original_text (LLM report or script detection) —
    # never of a caller-supplied override. Plain str, not an enum: an unrecognized code
    # must degrade to English at render time, not fail validation.
    detected_lang: str = "en"


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
