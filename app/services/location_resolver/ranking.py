"""Candidate scoring and ambiguity detection.

India ranks as a hard tier, not a bias: when at least one Indian candidate exists, every
Indian candidate outranks every non-Indian one, regardless of population. Filtering by
country was tried and rejected (resolves "Springfield" to an obscure Tamil Nadu hamlet
instead of the US city) — country only affects tier placement, never candidate presence.
"""
from __future__ import annotations

import math

from app.services.location_resolver.providers.base import LocationCandidate

CAPITAL_BONUS = 1.0  # admin/country capitals over same-name villages
EXACT_NAME_BONUS = 0.5

_CAPITAL_CODES = ("PPLC", "PPLA")


def is_india_candidate(candidate: LocationCandidate) -> bool:
    return (candidate.country_code or "").upper() == "IN"


def score_candidate(candidate: LocationCandidate, query: str) -> float:
    """Higher is better within a tier. log10(population) keeps the scale comparable to the
    bonuses. Country is not scored here — is_india_candidate() places the hard tier in
    rank()/select() instead, so an India candidate never loses to a bigger foreign city."""
    population = candidate.population or 0
    score = math.log10(population + 1)
    if (candidate.feature_code or "").upper().startswith(_CAPITAL_CODES):
        score += CAPITAL_BONUS
    head = query.split(",")[0].strip().casefold()
    if head and candidate.name.strip().casefold() == head:
        score += EXACT_NAME_BONUS
    return score


def rank(candidates: list[LocationCandidate], query: str) -> list[tuple[float, LocationCandidate]]:
    scored = [(score_candidate(candidate, query), candidate) for candidate in candidates]
    scored.sort(key=lambda pair: (is_india_candidate(pair[1]), pair[0]), reverse=True)
    return scored


def _is_plausible_single(candidate: LocationCandidate, query: str) -> bool:
    """A lone candidate only auto-wins with some signal it's a real known place — not just
    the one string a fuzzy-text search happened to return for a typo or "near me"."""
    if candidate.population is not None:
        return True
    if (candidate.feature_code or "").upper().startswith(_CAPITAL_CODES):
        return True
    head = query.split(",")[0].strip().casefold()
    return bool(head) and candidate.name.strip().casefold() == head


def select(
    candidates: list[LocationCandidate], query: str, dominance_margin: float
) -> tuple[LocationCandidate | None, bool, list[tuple[float, LocationCandidate]]]:
    """Return (winner, is_dominant, ranked).

    `is_dominant` is True when the top candidate beats the runner-up *within its own tier*
    by at least `dominance_margin`, or when it is the only plausible candidate in that tier
    (see `_is_plausible_single`). A non-dominant result is reported as ambiguous rather
    than silently resolved to a coin-flip winner — this now also covers a single
    low-quality fuzzy match, not just a close multi-candidate tie.
    """
    if not candidates:
        return None, False, []
    ranked = rank(candidates, query)
    if len(ranked) == 1:
        return ranked[0][1], _is_plausible_single(ranked[0][1], query), ranked
    winner_is_india = is_india_candidate(ranked[0][1])
    same_tier = [pair for pair in ranked if is_india_candidate(pair[1]) == winner_is_india]
    if len(same_tier) == 1:
        return ranked[0][1], _is_plausible_single(same_tier[0][1], query), ranked
    dominant = (same_tier[0][0] - same_tier[1][0]) >= dominance_margin
    return ranked[0][1], dominant, ranked


def confidence_for(score_gap: float, dominance_margin: float) -> float:
    """Map the winning margin onto the existing 0-1 confidence field."""
    if dominance_margin <= 0:
        return 0.9
    return round(min(0.95, 0.6 + 0.35 * min(1.0, score_gap / (dominance_margin * 3))), 3)
