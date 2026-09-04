"""Independent verification of agent claims.

The reviewer must never trust the number attached to a claim. Every numeric claim declares
how it was derived in ``Claim.extra["derivation"]``; this module re-runs that derivation
over the evidence the claim actually cites and compares the result. A claim can therefore
cite entirely real evidence and still be rejected, because the value hanging off that
citation is not what the evidence says.

Existence-only citation checking was sufficient while every agent was deterministic Python
reading values straight off the fused panels. It is not sufficient once a language model
writes a claim.
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.config import settings
from app.schemas.ceo import CanonicalEvidenceObject
from app.services.units import MS_TO_KMH, as_kmh

_MISSING = object()


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected,
                        rel_tol=settings.reviewer_value_rel_tol,
                        abs_tol=settings.reviewer_value_abs_tol)


def _numeric(ev: CanonicalEvidenceObject, unit: str | None) -> float | None:
    if ev.value is None:
        return None
    # The wind panel reports km/h whatever the source sent; the same conversion has to be
    # applied here or every m/s-sourced wind claim false-fails.
    return as_kmh(ev) if unit == "km/h" else ev.value


def _selected(cited: list[CanonicalEvidenceObject], derivation: dict[str, Any]) -> list[CanonicalEvidenceObject]:
    """The subset of cited evidence a derivation actually operated on.

    The rain panel cites a precipitation_probability record alongside the amounts it summed.
    Summing everything cited would recompute a total the panel never claimed.
    """
    variable = derivation.get("variable")
    window = derivation.get("accumulation_window_hours", _MISSING)
    selected = [ev for ev in cited if variable is None or ev.variable == variable]
    if window is not _MISSING:
        selected = [ev for ev in selected if ev.accumulation_window_hours == window]
    return selected


def verify_claim(claim, cited: list[CanonicalEvidenceObject]) -> tuple[list[str], list[str]]:
    """Recompute a claim's value from its cited evidence.

    Returns (errors, warnings). An error is a value that contradicts its evidence and fails
    the request. A warning is a claim shape no verifier recognises — recorded, but not a
    reason to reject a response that may be perfectly correct.
    """
    derivation = (claim.extra or {}).get("derivation")
    if not isinstance(derivation, dict):
        return [], [f"claim {claim.claim} declares no derivation; its value was not verified"]

    op = derivation.get("op")
    if op == "none":
        return [], []

    declared_unit = derivation.get("unit")
    if declared_unit and claim.unit and claim.unit != declared_unit:
        return [f"claim {claim.claim} states unit {claim.unit!r} but was derived in {declared_unit!r}"], []

    if op == "identity":
        field = derivation.get("field", "value")
        if len(cited) != 1:
            return [f"claim {claim.claim} declares an identity derivation but cites {len(cited)} records"], []
        expected = getattr(cited[0], field, None)
        if field == "value":
            if claim.value is None or expected is None or not _close(float(claim.value), float(expected)):
                return [f"claim {claim.claim} states {claim.value!r} but its evidence reports {expected!r}"], []
            return [], []
        if claim.value != expected:
            return [f"claim {claim.claim} states {claim.value!r} but its evidence reports {expected!r}"], []
        return [], []

    if op not in {"sum", "max", "min"}:
        return [], [f"claim {claim.claim} declares unrecognised derivation {op!r}; its value was not verified"]

    values = [value for value in (_numeric(ev, declared_unit) for ev in _selected(cited, derivation))
              if value is not None]
    if not values:
        return [f"claim {claim.claim} cites no evidence matching its own derivation"], []

    expected = sum(values) if op == "sum" else (max(values) if op == "max" else min(values))
    if claim.value is None or not _close(float(claim.value), float(expected)):
        return [f"claim {claim.claim} states {claim.value!r} but {op} of its cited evidence is {expected!r}"], []
    return [], []


_QUANTITY = re.compile(r"(-?\d+(?:\.\d+)?)\s*(mm|%|°\s*c|celsius|c|km\s*/\s*h|kmph|kph)\b", re.IGNORECASE)

_UNIT_ALIASES = {"mm": "mm", "%": "%", "c": "C", "°c": "C", "° c": "C", "celsius": "C",
                 "km/h": "km/h", "km /h": "km/h", "km/ h": "km/h", "km / h": "km/h",
                 "kmph": "km/h", "kph": "km/h"}


def _normalise_unit(raw: str) -> str | None:
    return _UNIT_ALIASES.get(re.sub(r"\s+", " ", raw.strip().lower()))


def grounded_values(wio) -> dict[str, set[float]]:
    """Every quantity the deterministic pipeline actually produced, keyed by unit.

    Prose may restate these and nothing else.
    """
    allowed: dict[str, set[float]] = {"mm": set(), "%": set(), "C": set(), "km/h": set()}

    rain = wio.weather.rain or {}
    for key in ("value_mm", "peak_hourly_mm"):
        if rain.get(key) is not None:
            allowed["mm"].add(float(rain[key]))
    for value in rain.get("member_values", []) or []:
        allowed["mm"].add(float(value))
    if rain.get("probability") is not None:
        allowed["%"].add(float(rain["probability"]) * 100)

    temperature = wio.weather.temperature or {}
    for key in ("min", "max"):
        if temperature.get(key) is not None:
            allowed["C"].add(float(temperature[key]))

    wind = wio.weather.wind or {}
    if wind.get("value_kmh") is not None:
        allowed["km/h"].add(float(wind["value_kmh"]))

    for item in wio.evidence:
        if item.value is None:
            continue
        unit = (item.unit or "").lower()
        if unit in {"mm", "mm/h"}:
            allowed["mm"].add(float(item.value))
        elif unit in {"c", "°c", "celsius"}:
            allowed["C"].add(float(item.value))
        elif unit in {"km/h", "kmh"}:
            allowed["km/h"].add(float(item.value))
        elif unit == "m/s":
            allowed["km/h"].add(float(item.value) * MS_TO_KMH)
        elif unit in {"%", "percent", "probability"}:
            value = float(item.value)
            allowed["%"].add(value * 100 if value <= 1 else value)
    return allowed


def check_prose_grounding(text: str, wio) -> list[str]:
    """Quantities in free text that the pipeline never produced.

    Only numbers carrying a physical unit are checked. Bare numerals ("the next 24 hours",
    "three sources") are prose, not weather claims, and treating them as claims produces
    nothing but false rejections. The prompt separately forbids introducing any number.
    """
    ungrounded: list[str] = []
    allowed = grounded_values(wio)
    for raw_value, raw_unit in _QUANTITY.findall(text or ""):
        unit = _normalise_unit(raw_unit)
        if unit is None:
            continue
        stated = float(raw_value)
        # An LLM writing "2 mm" for 2.4 is rounding, which is allowed; "45 mm" is not.
        if any(_close(stated, round(value, places))
               for value in allowed[unit] for places in (0, 1, 2)):
            continue
        ungrounded.append(f"{raw_value} {unit}")
    return ungrounded
