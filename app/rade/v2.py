"""RADE v2: deterministic, context-aware expected utility and downside-risk policy."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings


class Scenario(BaseModel):
    name: str
    probability: float = Field(ge=0, le=1)
    precipitation_mm: float
    wind_kmh: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class DecisionAlternative(BaseModel):
    action: str
    score: float
    expected_utility: float
    downside_risk: float


class DecisionResult(BaseModel):
    recommended_action: str
    alternatives: list[DecisionAlternative] = Field(default_factory=list)
    expected_utility: float
    risk: float
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    rejected_actions: list[str] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Score gap between the top two ranked actions — domain-general "how close was this
    # call" signal. None when there was only one alternative to rank at all.
    top_margin: float | None = None


POLICIES: dict[str, dict[str, dict[str, float]]] = {
    "spray": {
        "spray": {"dry": 20, "wet": -35, "wind_penalty": -25},
        "delay": {"dry": -5, "wet": 12}, "reschedule": {"dry": -8, "wet": 14},
    },
    "irrigate": {
        "irrigate": {"dry": 16, "wet": -14}, "delay": {"dry": -7, "wet": 12}, "partial_irrigation": {"dry": 8, "wet": 2},
    },
    "harvest": {
        "harvest_now": {"dry": 20, "wet": -30}, "delay": {"dry": -8, "wet": 10}, "partial_harvest": {"dry": 8, "wet": -2},
    },
    "marine": {
        "go": {"dry": 16, "wet": -45, "wind_penalty": -30}, "delay": {"dry": -4, "wet": 12}, "avoid": {"dry": -10, "wet": 18},
    },
    "travel": {
        "go": {"dry": 12, "wet": -22, "wind_penalty": -14}, "delay": {"dry": -5, "wet": 10}, "alternate_route": {"dry": 5, "wet": 8},
    },
}


# Optional context fields a domain knows how to use when the call is borderline and the
# field is unknown — declared here, not hardcoded per-domain in the guardrail/main.py, so a
# new domain's follow-up needs a dict entry, not a new code path. Every domain without an
# entry is a no-op: no field is ever asked about, no CLARIFYING_FIELDS-driven escalation runs.
CLARIFYING_FIELDS: dict[str, dict[str, dict]] = {
    "marine": {
        "crew_size": {"kind": "solo_or_count", "solo_words": ("alone", "solo", "single", "myself")},
        "boat_size": {"kind": "enum", "values": {"small": ("small", "dinghy", "kayak", "canoe"),
                                                  "large": ("large", "big", "trawler")}},
    },
}


def _domain(context: str) -> str | None:
    """Only the decision context is matched — including the user-context dict let a stored fact containing "crop" flip the domain of an unrelated question."""
    text = context.casefold()
    for domain, keywords in {"spray": ("spray", "pesticide"), "irrigate": ("irrigat", "sichai"), "harvest": ("harvest", "crop"), "marine": ("fish", "marine", "boat"), "travel": ("travel", "route", "drive")}.items():
        if any(keyword in text for keyword in keywords):
            return domain
    return None


def generate_scenarios(wio) -> tuple[list[Scenario], list[str]]:
    rain = wio.weather.rain or {}
    evidence_ids = [item.evidence_id for item in wio.evidence if item.variable in {"precipitation_amount", "precipitation_probability", "wind_speed"}]
    members = rain.get("member_values") if isinstance(rain, dict) else None
    wind = (wio.weather.wind or {}).get("value_kmh") if wio.weather.wind else None
    if members:
        # Member provenance remains in the scenario evidence IDs; values are empirically binned.
        bins = [(0, 2, "0-2mm"), (2, 10, "2-10mm"), (10, 25, "10-25mm"), (25, 50, "25-50mm"), (50, float("inf"), ">50mm")]
        scenarios = [Scenario(name=name, probability=sum(low <= value < high for value in members) / len(members), precipitation_mm=(low if high == float("inf") else (low + high) / 2), wind_kmh=wind, evidence_ids=evidence_ids) for low, high, name in bins]
        return [scenario for scenario in scenarios if scenario.probability], ["Scenarios are derived from member-level ensemble values."]
    probability = rain.get("probability") if isinstance(rain, dict) else None
    amount = rain.get("value_mm") if isinstance(rain, dict) else None
    if probability is None or amount is None:
        return [], ["No precipitation distribution is available; RADE will not assert a weather-dependent recommendation."]
    probability = max(0.0, min(1.0, float(probability)))
    return [Scenario(name="dry", probability=1 - probability, precipitation_mm=0, wind_kmh=wind, evidence_ids=evidence_ids), Scenario(name="measurable_rain", probability=probability, precipitation_mm=float(amount), wind_kmh=wind, evidence_ids=evidence_ids)], ["Scenarios use the provider precipitation probability and amount; no ensemble distribution was available."]


def _utility(action: str, scenario: Scenario, table: dict[str, dict[str, float]]) -> float:
    values = table[action]
    wet = scenario.precipitation_mm >= 0.5
    result = values["wet" if wet else "dry"]
    if scenario.wind_kmh is not None and scenario.wind_kmh > 25:
        result += values.get("wind_penalty", 0)
    return result


def _marine_warning_active(wio) -> bool:
    """wio.official_warning is a single collapsed slot that a marine alert can correctly lose to a higher-severity non-marine one, so RADE's marine domain reads its own signal straight off wio.evidence instead, which keeps every surviving CEO's variable regardless of which won that slot."""
    return any(item.variable == "marine_warning" for item in wio.evidence)


_CAUTION_PENALTY = 13
_AVOID_PENALTY = 35


def _marine_hazard_penalty(wio) -> tuple[float, list[str]]:
    """Marine safety is dominated by sea state and official warnings, not the rain/wind table every other domain shares — returns a utility penalty (applied only to "go", never making "stay in" look better) plus the assumption strings explaining why.
    Magnitudes are calibrated against POLICIES["marine"] (go=16/-45, delay=-4/12, avoid=-10/18): the caution penalty (13) still lets "go" win at default risk_lambda (0.6) but not the crew/boat-escalated one (0.85), a deliberately borderline case; the avoid penalty (35) always loses to "delay" regardless of risk tolerance, since a genuinely dangerous sea state is never a profile-dependent call."""
    marine = wio.weather.marine or {}
    wind = (wio.weather.wind or {}).get("value_kmh")
    penalty = 0.0
    notes: list[str] = []

    wave = marine.get("wave_height_m")
    if wave is not None:
        if wave >= settings.rade_marine_wave_avoid_m:
            penalty -= _AVOID_PENALTY
            notes.append(f"wave height {wave}m is at or above the avoid threshold ({settings.rade_marine_wave_avoid_m}m)")
        elif wave >= settings.rade_marine_wave_caution_m:
            penalty -= _CAUTION_PENALTY
            notes.append(f"wave height {wave}m is at or above the caution threshold ({settings.rade_marine_wave_caution_m}m)")

    current = marine.get("current_velocity_kmh")
    if current is not None and current >= settings.rade_marine_current_caution_kmh:
        penalty -= _CAUTION_PENALTY
        notes.append(f"current {current} km/h is at or above the caution threshold ({settings.rade_marine_current_caution_kmh} km/h)")

    if wind is not None:
        if wind >= settings.rade_marine_wind_avoid_kmh:
            penalty -= _AVOID_PENALTY
            notes.append(f"wind {wind} km/h is at or above the avoid threshold ({settings.rade_marine_wind_avoid_kmh} km/h)")
        elif wind >= settings.rade_marine_wind_caution_kmh:
            penalty -= _CAUTION_PENALTY
            notes.append(f"wind {wind} km/h is at or above the caution threshold ({settings.rade_marine_wind_caution_kmh} km/h)")

    if _marine_warning_active(wio):
        penalty -= _AVOID_PENALTY
        notes.append("a marine-flavored official warning is active")

    return penalty, notes


def decide(wio, user_context: dict[str, Any], decision_context: str = "") -> DecisionResult:
    domain = _domain(decision_context)
    scenarios, assumptions = generate_scenarios(wio)
    evidence_ids = list(dict.fromkeys(eid for scenario in scenarios for eid in scenario.evidence_ids))
    if domain is None:
        return DecisionResult(recommended_action="defer_decision", expected_utility=0, risk=1, confidence=0,
                              rationale="The question does not match a supported decision domain.",
                              evidence_ids=evidence_ids, assumptions=assumptions, scenarios=scenarios)
    if not scenarios:
        return DecisionResult(recommended_action="defer_decision", expected_utility=0, risk=1, confidence=0,
                              rationale="Weather evidence is insufficient for a risk-aware recommendation.", evidence_ids=evidence_ids,
                              assumptions=assumptions, rejected_actions=list(POLICIES[domain]), scenarios=[])
    risk_tolerance = str(user_context.get("risk_tolerance", "medium")).casefold()
    risk_lambda = {"low": 1.0, "medium": 0.6, "high": 0.25}.get(risk_tolerance, 0.6)
    if wio.official_warning.active and wio.official_warning.severity in {"orange", "red"}:
        risk_lambda = max(risk_lambda, 1.0)
        assumptions.append("Risk aversion increased because an official high-severity warning is active.")
    if domain == "marine" and str(user_context.get("boat_size", "")).casefold() == "small" \
            and isinstance(user_context.get("crew_size"), int) and user_context["crew_size"] > 1:
        # A small boat with >1 person is a downside-risk-tolerance fact (less margin, more people to rescue), not a hazard-magnitude one, so it escalates risk_lambda rather than the hazard penalty below.
        risk_lambda = max(risk_lambda, 0.85)
        assumptions.append("Risk aversion increased for a small boat with more than one person aboard.")
    marine_penalty, marine_notes = _marine_hazard_penalty(wio) if domain == "marine" else (0.0, [])
    assumptions.extend(marine_notes)
    ranked: list[DecisionAlternative] = []
    for action in POLICIES[domain]:
        outcomes = [(scenario.probability, _utility(action, scenario, POLICIES[domain])) for scenario in scenarios]
        expected = sum(probability * value for probability, value in outcomes)
        downside = sum(probability * abs(value) for probability, value in outcomes if value < 0)
        if action == "go" and domain == "marine":
            expected += marine_penalty
            downside += abs(min(marine_penalty, 0.0))
        ranked.append(DecisionAlternative(action=action, expected_utility=expected, downside_risk=downside, score=expected - risk_lambda * downside))
    ranked.sort(key=lambda item: item.score, reverse=True)
    best = ranked[0]
    top_margin = ranked[0].score - ranked[1].score if len(ranked) > 1 else None
    # Kept above WEATHERGPT_BIG_LLM_COMPLEXITY_CONFIDENCE_THRESHOLD (0.6): one source alone is weaker than two agreeing, but not the unresolved case the big tier exists for.
    confidence = {"full_agreement": 0.8, "single_source": 0.65}.get(wio.agreement.status, 0.55)
    return DecisionResult(recommended_action=best.action, alternatives=ranked[1:], expected_utility=best.expected_utility,
                          risk=best.downside_risk, confidence=float(confidence), top_margin=top_margin,
                          rationale=f"{best.action} has the highest risk-adjusted utility for {domain}; expected utility {best.expected_utility:.1f}, downside risk {best.downside_risk:.1f}.",
                          evidence_ids=evidence_ids, assumptions=assumptions, rejected_actions=[item.action for item in ranked[1:]], scenarios=scenarios)
