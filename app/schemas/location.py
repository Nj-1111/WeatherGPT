from __future__ import annotations

from pydantic import BaseModel


class ResolvedLocation(BaseModel):
    raw: str
    lat: float
    lon: float
    district: str | None = None
    state: str | None = None
    block: str | None = None
    pincode: str | None = None
    country: str | None = None
    timezone: str | None = None
    confidence: float = 0.8
    source: str = "nominatim_cache"
    candidates: list[dict] = []
    normalized_name: str | None = None
    administrative_hierarchy: list[str] = []
    resolution_method: str = "gazetteer"
    ambiguity_status: str = "resolved"

# Minimal in-memory gazetteer for demo/offline
GAZETTEER = {
    "nagpur": {"lat": 21.1458, "lon": 79.0882, "district": "Nagpur", "state": "Maharashtra"},
    "mumbai": {"lat": 19.0760, "lon": 72.8777, "district": "Mumbai", "state": "Maharashtra"},
    "delhi": {"lat": 28.6139, "lon": 77.2090, "district": "New Delhi", "state": "Delhi"},
    "kolkata": {"lat": 22.5726, "lon": 88.3639, "district": "Kolkata", "state": "West Bengal"},
    "chennai": {"lat": 13.0827, "lon": 80.2707, "district": "Chennai", "state": "Tamil Nadu"},
    "bengaluru": {"lat": 12.9716, "lon": 77.5946, "district": "Bengaluru Urban", "state": "Karnataka"},
    "pune": {"lat": 18.5204, "lon": 73.8567, "district": "Pune", "state": "Maharashtra"},
    "malegaon": {"lat": 20.5579, "lon": 74.5287, "district": "Nashik", "state": "Maharashtra"},
}
