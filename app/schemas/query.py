"""Output contract for the guardrail decision funnel (services/query_guardrail.py) — intentionally minimal, what the guardrail produces, not a place to accumulate downstream state."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, computed_field

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


PairingMode = Literal["locations_x_shared_time", "times_x_shared_location", "full_cross_product"]
CapabilityConfidence = Literal["low", "medium", "high"]


class GuardrailDecision(BaseModel):
    """Output of services/query_guardrail.py — a strict decision-tree classification, not a graded judgment (see that module's system prompt for the exact rules applied).

    Every field added after `detected_lang` degrades independently on invalid/missing input
    (defaults to empty/None/a safe default) rather than discarding the whole decision — this
    call is already the tightest, most failure-prone LLM call in the pipeline (a token-budget
    bug once silently zeroed out every guardrail LLM call for a full session), so no single
    new field may be allowed to take the rest down with it.
    """
    # max_length values mirror schemas/api.py's user-facing fields (LocationInput.raw=256, QueryRequestV1.question=4096); these are LLM-produced, not user-typed, but were the one inconsistency an attacker could lean on for an oversized query/cache key (§2.2).
    original_text: str = Field(max_length=4096)
    action: GuardrailAction
    verify_candidate: str | None = Field(default=None, max_length=256)
    clarify_reason: ClarifyReason | None = None
    unsupported_topic: str | None = Field(default=None, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    extraction_source: ExtractionSource = "deterministic_fallback"
    # ISO 639-1 code, a pure function of original_text (never a caller override); plain str not enum, so an unrecognized code degrades to English at render time instead of failing validation.
    detected_lang: str = "en"
    # Short free-text phrase describing who's asking and what they're deciding, inferred by
    # the same LLM call from the text itself — e.g. "a driver checking road conditions".
    # Same status as location/time/verify_candidate: LLM-classified, same-turn, bounded.
    # Never set by the deterministic fallback, which doesn't guess free text.
    apparent_context: str | None = Field(default=None, max_length=200)

    # Multi-entity extraction (replaces the old singular location/time fields — see the
    # computed properties below for the backward-compatible single-value view). Each list
    # entry mirrors location/verify_candidate's own max_length; capped in length by
    # settings.max_location_time_pairs downstream in main.py, not here (this model doesn't
    # know the config value and shouldn't reach for settings from a schema module).
    locations: list[str] = Field(default_factory=list)
    time_phrases: list[str] = Field(default_factory=list)
    pairing_mode: PairingMode = "locations_x_shared_time"

    # Closed, multi-select capability vocabulary (app.orchestrator.retrieval_planner.ALL_CAPABILITIES)
    # replacing keyword-only retrieval triggering with genuine understanding, without letting
    # the LLM select data sources itself — it only ever picks from this fixed list.
    capabilities: list[str] = Field(default_factory=list)
    capabilities_version: str = "v1"
    # Advisory/logging only in this round — not wired to change fetch behavior.
    confidence_per_capability: dict[str, CapabilityConfidence] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def location(self) -> str | None:
        """Backward-compatible single-value view of `locations` — deprecated, read-only.
        Existing call sites keep reading `.location` unchanged; only construction sites need
        to move to `locations=[...]`."""
        return self.locations[0] if self.locations else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def time(self) -> str | None:
        """Backward-compatible single-value view of `time_phrases` — deprecated, read-only."""
        return self.time_phrases[0] if self.time_phrases else None


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
