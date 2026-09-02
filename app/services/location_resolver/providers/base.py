"""Provider contract — the rest of the app never knows which geocoder answered."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LocationCandidate:
    """One possible interpretation of a location query, normalized across providers."""
    name: str
    lat: float
    lon: float
    country_code: str | None = None
    country: str | None = None
    admin1: str | None = None  # state / first-level division
    admin2: str | None = None  # district / second-level division
    population: int | None = None
    feature_code: str | None = None  # GeoNames-style class, e.g. PPLA (admin capital)
    postcode: str | None = None
    timezone: str | None = None  # IANA name; only Open-Meteo supplies one
    provider: str = "unknown"

    def summary(self) -> dict[str, object]:
        """Client-safe candidate description — never exposes provider internals or URLs."""
        return {
            "name": self.name,
            "state": self.admin1,
            "district": self.admin2,
            "country": self.country or self.country_code,
            "lat": self.lat,
            "lon": self.lon,
        }


class LocationProvider(Protocol):
    name: str

    async def search(self, query: str) -> list[LocationCandidate]:
        """Return candidates for a normalized query. Must return [] rather than raise
        on 'no match'; may raise on transport failure (the resolver isolates it)."""
        ...
