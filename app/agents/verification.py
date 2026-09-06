"""Independent verification of agent claims: the reviewer never trusts the number attached to a claim — every numeric claim declares how it was derived in ``Claim.extra["derivation"]``, and this module re-runs that derivation over the cited evidence and compares, so a claim citing entirely real evidence can still be rejected if its value doesn't match.
Existence-only citation checking was sufficient while every agent was deterministic Python reading values off the fused panels; it is not sufficient once a language model writes a claim."""
from __future__ import annotations

import math
import re
from collections.abc import Callable
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
    # The wind panel reports km/h whatever the source sent; the same conversion must apply here or every m/s-sourced wind claim false-fails.
    return as_kmh(ev) if unit == "km/h" else ev.value


def _selected(cited: list[CanonicalEvidenceObject], derivation: dict[str, Any]) -> list[CanonicalEvidenceObject]:
    """The subset of cited evidence a derivation actually operated on — the rain panel cites a precipitation_probability record alongside the summed amounts, and summing everything cited would recompute a total the panel never claimed."""
    variable = derivation.get("variable")
    window = derivation.get("accumulation_window_hours", _MISSING)
    selected = [ev for ev in cited if variable is None or ev.variable == variable]
    if window is not _MISSING:
        selected = [ev for ev in selected if ev.accumulation_window_hours == window]
    return selected


def verify_claim(claim, cited: list[CanonicalEvidenceObject]) -> tuple[list[str], list[str]]:
    """Recompute a claim's value from its cited evidence. Returns (errors, warnings) — an error is a value that contradicts its evidence and fails the request; a warning is a claim shape no verifier recognises, recorded but not a rejection reason."""
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


_QUANTITY = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*"
    r"(mm|inches?|%|°\s*f|fahrenheit|°\s*c|celsius|c|km\s*/\s*h|kmph|kph|mph)\b",
    re.IGNORECASE)

# raw regex-matched spelling -> (canonical bucket in `allowed`, conversion to it). Bare "in"/"f" are excluded — they'd false-match prose like "24 in the morning" — so imperial units only match unambiguous spellings.
def _identity(value: float) -> float:
    return value


def _f_to_c(value: float) -> float:
    return (value - 32) * 5 / 9


def _mph_to_kmh(value: float) -> float:
    return value * 1.609344


def _inches_to_mm(value: float) -> float:
    return value * 25.4


_UNIT_ALIASES: dict[str, tuple[str, Callable[[float], float]]] = {
    "mm": ("mm", _identity), "inch": ("mm", _inches_to_mm), "inches": ("mm", _inches_to_mm),
    "%": ("%", _identity),
    "c": ("C", _identity), "°c": ("C", _identity), "° c": ("C", _identity), "celsius": ("C", _identity),
    "°f": ("C", _f_to_c), "° f": ("C", _f_to_c), "fahrenheit": ("C", _f_to_c),
    "km/h": ("km/h", _identity), "km /h": ("km/h", _identity), "km/ h": ("km/h", _identity),
    "km / h": ("km/h", _identity), "kmph": ("km/h", _identity), "kph": ("km/h", _identity),
    "mph": ("km/h", _mph_to_kmh),
}


def _normalise_unit(raw: str) -> tuple[str, Callable[[float], float]] | None:
    return _UNIT_ALIASES.get(re.sub(r"\s+", " ", raw.strip().lower()))


def grounded_values(wio) -> dict[str, set[float]]:
    """Every quantity the deterministic pipeline actually produced, keyed by unit — prose may restate these and nothing else."""
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
    """Quantities in free text that the pipeline never produced. Only numbers carrying a physical unit are checked — bare numerals ("the next 24 hours") are prose, not weather claims, and checking them just produces false rejections; the prompt separately forbids introducing any number."""
    ungrounded: list[str] = []
    allowed = grounded_values(wio)
    for raw_value, raw_unit in _QUANTITY.findall(text or ""):
        normalised = _normalise_unit(raw_unit)
        if normalised is None:
            continue
        unit, convert = normalised
        stated = convert(float(raw_value))
        # "2 mm" for 2.4 is rounding, allowed; "45 mm" is not. Conversion (e.g. mph->km/h) happens before this comparison, so a restated unit still checks against the same grounded value.
        if any(_close(stated, round(value, places))
               for value in allowed[unit] for places in (0, 1, 2)):
            continue
        ungrounded.append(f"{raw_value} {raw_unit.strip()}")
    return ungrounded
