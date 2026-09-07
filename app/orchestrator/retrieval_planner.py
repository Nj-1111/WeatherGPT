"""Deterministic retrieval planning; language models never select data sources."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

EvidenceClassName = Literal["forecast", "warning", "observation", "reanalysis", "radar", "satellite"]


class RetrievalPlan(BaseModel):
    variables: list[str]
    evidence_classes: list[EvidenceClassName]
    sources: list[str]
    need_history: bool = False
    need_warnings: bool = False
    need_ensemble: bool = False
    decision_context: str | None = None
    reasons: list[str] = Field(default_factory=list)


# Requesting a temperature means any of its statistics is useful: sources report instantaneous, daily-max and daily-min under different canonical names.
_VARIABLE_FAMILIES = {
    "precipitation": ["precipitation_amount", "precipitation_probability"],
    "temperature": ["temperature_2m", "temperature_max", "temperature_min"],
    "wind": ["wind_speed", "wind_gust"],
    "marine": ["wave_height", "wave_direction", "wave_period",
              "ocean_current_velocity", "ocean_current_direction", "sea_surface_temperature"],
    "humidity": ["humidity"],
    "pressure": ["pressure_msl"],
    "cloud_cover": ["cloud_cover"],
    "visibility": ["visibility"],
}

# Closed capability vocabulary the guardrail LLM selects from (app.schemas.query.GuardrailDecision
# .capabilities) — replaces keyword-only retrieval triggering with genuine understanding while
# keeping data-fetching itself fully deterministic: the LLM picks a name from this fixed list,
# never a source or a value. DATA_CAPABILITIES must map to a real fetch/computation; GUIDANCE_FLAGS
# only ever toggle explanation-prompt behavior and carry no data of their own. `heat_stress` is
# derived (temperature+humidity+wind already fetched), not a separate raw fetch. Capabilities not
# yet wired here (uv_index, astronomy_basic, historical_weather, seasonal_climate,
# agriculture_weather, air_quality) are deliberately not in this v1 vocabulary — adding one later
# is an additive entry, not a version bump (see GuardrailDecision.capabilities_version).
DATA_CAPABILITIES: tuple[str, ...] = (
    "temperature", "precipitation", "wind", "marine", "extreme_events",
    "humidity", "pressure", "cloud_cover", "visibility", "heat_stress",
)
GUIDANCE_FLAGS: tuple[str, ...] = ("travel_safety_guidance",)
ALL_CAPABILITIES: tuple[str, ...] = DATA_CAPABILITIES + GUIDANCE_FLAGS

# capability -> variable family key, for capabilities that map directly to one existing family.
# marine/extreme_events/heat_stress are handled separately below since they aren't 1:1.
_CAPABILITY_VARIABLE_FAMILY: dict[str, str] = {
    "temperature": "temperature", "precipitation": "precipitation", "wind": "wind",
    "humidity": "humidity", "pressure": "pressure", "cloud_cover": "cloud_cover",
    "visibility": "visibility",
}

_DECISION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "spray": ("spray", "spraying", "pesticide", "chhidak", "छिड़क"),
    "irrigate": ("irrigate", "irrigation", "water crop", "sichai", "सिंचाई"),
    "harvest": ("harvest", "harvesting", "cut crop", "katai", "कटाई"),
    "marine": ("fish", "fishing", "marine", "boat", "sail", "sea"),
    "travel": ("travel", "travelling", "drive", "driving", "route", "commute", "journey"),
}

_RAIN_WORDS = ("rain", "rainfall", "baarish", "barish", "बरसात", "precipitation", "precip", "shower", "showers")
_TEMPERATURE_WORDS = ("temperature", "temp", "hot", "cold", "heat", "mausam", "मौसम")
_WIND_WORDS = ("wind", "windy", "gust", "gusts", "hawa", "हवा")
_WARNING_WORDS = ("warning", "warnings", "alert", "alerts", "cyclone", "storm", "flood", "flooding", "heatwave")
_MARINE_WORDS = ("wave", "waves", "swell", "current", "currents", "tide", "tides", "surf")
_UNCERTAINTY_WORDS = ("probability", "chance", "chances", "likely", "uncertain", "uncertainty")
_HISTORY_WORDS = ("usual", "usually", "history", "historical", "climate", "normal", "average", "typically")


def has_word(text: str, words: tuple[str, ...]) -> bool:
    """Whole-word matching — substring matching made 'go' match Goa/mango and 'sea' match season, silently turning weather questions into travel decisions."""
    return any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) for word in words)


def build_retrieval_plan(question: str, horizon: str, decision_type: str | None = None,
                         capabilities: list[str] | None = None) -> RetrievalPlan:
    text = question.casefold()
    decision = decision_type
    if not decision:
        for domain, keywords in _DECISION_KEYWORDS.items():
            if has_word(text, keywords):
                decision = domain
                break
    # Capabilities are the guardrail LLM's broader understanding (covers phrasing the
    # keyword sets below miss entirely, e.g. "is it safe near the coast" for marine, or
    # "fog on the road" for visibility) — OR'd alongside the keyword triggers, never
    # replacing them, so the deterministic keyword path is unaffected when the LLM is down
    # or capabilities is empty.
    caps = set(capabilities or [])

    variables: list[str] = []
    if has_word(text, _RAIN_WORDS) or decision or "precipitation" in caps:
        variables.extend(_VARIABLE_FAMILIES["precipitation"])
    if has_word(text, _TEMPERATURE_WORDS) or "temperature" in caps or "heat_stress" in caps:
        variables.extend(_VARIABLE_FAMILIES["temperature"])
    if (has_word(text, _WIND_WORDS) or decision in {"spray", "marine", "travel"}
            or "wind" in caps or "heat_stress" in caps):
        variables.extend(_VARIABLE_FAMILIES["wind"])
    need_marine = has_word(text, _MARINE_WORDS) or decision == "marine" or "marine" in caps
    if need_marine:
        variables.extend(_VARIABLE_FAMILIES["marine"])
    if "humidity" in caps or "heat_stress" in caps:
        variables.extend(_VARIABLE_FAMILIES["humidity"])
    if "pressure" in caps:
        variables.extend(_VARIABLE_FAMILIES["pressure"])
    if "cloud_cover" in caps:
        variables.extend(_VARIABLE_FAMILIES["cloud_cover"])
    if "visibility" in caps:
        variables.extend(_VARIABLE_FAMILIES["visibility"])
    if not variables:
        variables = _VARIABLE_FAMILIES["temperature"] + _VARIABLE_FAMILIES["precipitation"]
    variables = list(dict.fromkeys(variables))

    # CAP is always retrieved: official warnings are safety information — gating on the user saying "warning" hid a live thunderstorm alert from "weather in X".
    classes: list[EvidenceClassName] = ["forecast", "warning"]
    sources = ["OPEN_METEO", "MET_NORWAY", "CAP"]
    reasons = ["forecast requested", "official warnings always checked"]

    need_warnings = has_word(text, _WARNING_WORDS) or decision is not None or "extreme_events" in caps
    if need_warnings:
        sources.append("IMD")
        reasons.append("warning-specific sources requested")

    if need_marine:
        sources.extend(["OPEN_METEO_MARINE", "STORMGLASS"])
        reasons.append("marine conditions requested")

    need_ensemble = decision is not None or has_word(text, _UNCERTAINTY_WORDS)
    if need_ensemble:
        sources.append("GEFS")
        reasons.append("uncertainty needed for decision/probability")

    need_history = horizon == "climate" or has_word(text, _HISTORY_WORDS)
    if need_history:
        classes.append("reanalysis")
        sources.extend(["ERA5", "NASA_POWER"])
        reasons.append("historical context requested")

    return RetrievalPlan(
        variables=variables, evidence_classes=classes, sources=list(dict.fromkeys(sources)),
        need_history=need_history, need_warnings=need_warnings, need_ensemble=need_ensemble,
        decision_context=decision, reasons=reasons,
    )
