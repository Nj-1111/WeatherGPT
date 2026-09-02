"""Candidate scoring and ambiguity detection.

India is preferred, but as a *bias*, not a filter: filtering by country would resolve
"Springfield" to an obscure Tamil Nadu hamlet instead of the US city. Scoring keeps
Indian places winning whenever they are plausible, without breaking global queries.
"""
from __future__ import annotations

import math

from app.services.location_resolver.providers.base import LocationCandidate

INDIA_BONUS = 2.0  # worth ~100x population — India is the priority market
CAPITAL_BONUS = 1.0  # admin/country capitals over same-name villages
EXACT_NAME_BONUS = 0.5

_CAPITAL_CODES = ("PPLC", "PPLA")


def score_candidate(candidate: LocationCandidate, query: str) -> float:
    """Higher is better. log10(population) keeps the scale comparable to the bonuses."""
    population = candidate.population or 0
    score = math.log10(population + 1)
    if (candidate.country_code or "").upper() == "IN":
        score += INDIA_BONUS
    if (candidate.feature_code or "").upper().startswith(_CAPITAL_CODES):
        score += CAPITAL_BONUS
    head = query.split(",")[0].strip().casefold()
    if head and candidate.name.strip().casefold() == head:
        score += EXACT_NAME_BONUS
    return score


def rank(candidates: list[LocationCandidate], query: str) -> list[tuple[float, LocationCandidate]]:
    scored = [(score_candidate(candidate, query), candidate) for candidate in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def select(
    candidates: list[LocationCandidate], query: str, dominance_margin: float
) -> tuple[LocationCandidate | None, bool, list[tuple[float, LocationCandidate]]]:
    """Return (winner, is_dominant, ranked).

    `is_dominant` is True when the top candidate beats the runner-up by at least
    `dominance_margin`, or when it is the only candidate. A non-dominant result is
    reported as ambiguous rather than silently resolved to a coin-flip winner.
    """
    if not candidates:
        return None, False, []
    ranked = rank(candidates, query)
    if len(ranked) == 1:
        return ranked[0][1], True, ranked
    dominant = (ranked[0][0] - ranked[1][0]) >= dominance_margin
    return ranked[0][1], dominant, ranked


def confidence_for(score_gap: float, dominance_margin: float) -> float:
    """Map the winning margin onto the existing 0-1 confidence field."""
    if dominance_margin <= 0:
        return 0.9
    return round(min(0.95, 0.6 + 0.35 * min(1.0, score_gap / (dominance_margin * 3))), 3)
