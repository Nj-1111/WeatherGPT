"""Offline seed data — the last resort when every geocoding provider is unreachable.

These are the locations the system supported before geocoding existed. They are no longer
the location database; they exist so an offline/air-gapped demo still resolves its
core cities instead of failing every request. Nothing is fabricated: an unknown place
with no provider reachable still raises LocationNotFoundError.
"""
from __future__ import annotations

from app.schemas.location import GAZETTEER

# Kept here rather than in the resolver's hot path — PIN resolution now goes through
# the India Post provider; these 7 are only the offline fallback.
SEED_PINCODES = {
    "440001": {"lat": 21.1458, "lon": 79.0882, "district": "Nagpur", "state": "Maharashtra"},
    "400001": {"lat": 18.9388, "lon": 72.8347, "district": "Mumbai", "state": "Maharashtra"},
    "110001": {"lat": 28.6139, "lon": 77.2090, "district": "New Delhi", "state": "Delhi"},
    "700001": {"lat": 22.5726, "lon": 88.3639, "district": "Kolkata", "state": "West Bengal"},
    "600001": {"lat": 13.0827, "lon": 80.2707, "district": "Chennai", "state": "Tamil Nadu"},
    "560001": {"lat": 12.9716, "lon": 77.5946, "district": "Bengaluru Urban", "state": "Karnataka"},
    "411001": {"lat": 18.5204, "lon": 73.8567, "district": "Pune", "state": "Maharashtra"},
}


def seed_place(query: str) -> dict | None:
    """Exact-match lookup against the offline gazetteer. No substring matching —
    substring matching is what made 'pune mumbai' ambiguous and 'Indore' unresolvable."""
    key = (query or "").split(",")[0].strip().casefold()
    entry = GAZETTEER.get(key)
    if entry:
        return {**entry, "name": key.title()}
    return None


def seed_pincode(pincode: str) -> dict | None:
    return SEED_PINCODES.get(pincode)
