from __future__ import annotations

from pydantic import BaseModel


class ResolvedLocation(BaseModel):
    raw: str
    lat: float
    lon: float
    district: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str | None = None
    timezone: str | None = None
    confidence: float = 0.8
    source: str = "nominatim_cache"
    normalized_name: str | None = None
    administrative_hierarchy: list[str] = []
    resolution_method: str = "gazetteer"
