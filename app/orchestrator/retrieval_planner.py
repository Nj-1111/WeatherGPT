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


# Requesting a temperature means any of its statistics is useful: sources report
# instantaneous, daily-max and daily-min under different canonical names.
_VARIABLE_FAMILIES = {
    "precipitation": ["precipitation_amount", "precipitation_probability"],
    "temperature": ["temperature_2m", "temperature_max", "temperature_min"],
    "wind": ["wind_speed", "wind_gust"],
    "marine": ["wave_height", "wave_direction", "wave_period",
              "ocean_current_velocity", "ocean_current_direction", "sea_surface_temperature"],
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
    """Whole-word matching. Substring matching made 'go' match Goa and mango, and
    'sea' match season, silently turning weather questions into travel decisions."""
    return any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) for word in words)


def build_retrieval_plan(question: str, horizon: str, decision_type: str | None = None) -> RetrievalPlan:
    text = question.casefold()
    decision = decision_type
    if not decision:
        for domain, keywords in _DECISION_KEYWORDS.items():
            if has_word(text, keywords):
                decision = domain
                break

    variables: list[str] = []
    if has_word(text, _RAIN_WORDS) or decision:
        variables.extend(_VARIABLE_FAMILIES["precipitation"])
    if has_word(text, _TEMPERATURE_WORDS):
        variables.extend(_VARIABLE_FAMILIES["temperature"])
    if has_word(text, _WIND_WORDS) or decision in {"spray", "marine", "travel"}:
        variables.extend(_VARIABLE_FAMILIES["wind"])
    need_marine = has_word(text, _MARINE_WORDS) or decision == "marine"
    if need_marine:
        variables.extend(_VARIABLE_FAMILIES["marine"])
    if not variables:
        variables = _VARIABLE_FAMILIES["temperature"] + _VARIABLE_FAMILIES["precipitation"]
    variables = list(dict.fromkeys(variables))

    # CAP is always retrieved: an official warning is safety information, and gating it on
    # the user happening to say "warning" hid a live thunderstorm alert from "weather in X".
    classes: list[EvidenceClassName] = ["forecast", "warning"]
    sources = ["OPEN_METEO", "MET_NORWAY", "CAP"]
    reasons = ["forecast requested", "official warnings always checked"]

    need_warnings = has_word(text, _WARNING_WORDS) or decision is not None
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
