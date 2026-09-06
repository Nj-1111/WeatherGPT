"""Output contract for the guardrail decision funnel (services/query_guardrail.py) — intentionally minimal, what the guardrail produces, not a place to accumulate downstream state."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

ExtractionSource = Literal["llm", "deterministic_fallback", "confirmed"]


class GuardrailAction(str, Enum):
    """What the guardrail decided to do — the real dispatch key. ACCEPT_LOCATION_ONLY calls only the location resolver; ACCEPT_WEATHER_FULL runs the full pipeline; the other four return a rendered message immediately with no further calls."""
    ACCEPT_LOCATION_ONLY = "accept_location_only"
    ACCEPT_WEATHER_FULL = "accept_weather_full"
    REJECT_OFF_TOPIC = "reject_off_topic"
    CLARIFY = "clarify"
    VERIFY = "verify"
    # A real question about a hazard this system has no data for (earthquake, tsunami, wildfire, ...) — distinct from REJECT_OFF_TOPIC (not a weather/disaster/location question at all); answering with generic weather data here would be wrong, not just unhelpful.
    UNSUPPORTED_TOPIC = "unsupported_topic"


class ClarifyReason(str, Enum):
    GARBLED_INPUT = "garbled_input"
    NO_LOCATION = "no_location"


class Persona(str, Enum):
    """Who the query is for beyond generic weather-QA. Only MARINE is implemented today (retrieval, RADE, response formatting) — the rest are reserved so a later persona is an additive change, not a schema migration."""
    NONE = "none"
    MARINE = "marine"
    FARMER = "farmer"
    TRAVELLER = "traveller"
    MOUNTAINEER = "mountaineer"
    RESEARCHER = "researcher"


class GuardrailDecision(BaseModel):
    """Output of services/query_guardrail.py — a strict decision-tree classification, not a graded judgment (see that module's system prompt for the exact rules applied)."""
    # max_length values mirror schemas/api.py's user-facing fields (LocationInput.raw=256, QueryRequestV1.question=4096); these are LLM-produced, not user-typed, but were the one inconsistency an attacker could lean on for an oversized query/cache key (§2.2).
    original_text: str = Field(max_length=4096)
    action: GuardrailAction
    location: str | None = Field(default=None, max_length=256)
    time: str | None = Field(default=None, max_length=128)
    verify_candidate: str | None = Field(default=None, max_length=256)
    clarify_reason: ClarifyReason | None = None
    unsupported_topic: str | None = Field(default=None, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    extraction_source: ExtractionSource = "deterministic_fallback"
    persona: Persona = Persona.NONE
    # ISO 639-1 code, a pure function of original_text (never a caller override); plain str not enum, so an unrecognized code degrades to English at render time instead of failing validation.
    detected_lang: str = "en"


class ResolvedContext(BaseModel):
    """Last successful query's resolved location + time window, stored per session_id so a location-less follow-up can skip geocoding/time parsing rather than fail with LOCATION_REQUIRED."""
    resolved_lat: float
    resolved_lon: float
    resolved_location_name: str
    timezone: str | None = None
    valid_from: datetime
    valid_to: datetime
    horizon: str
    time_confidence: float = Field(ge=0.0, le=1.0, default=0.9)
