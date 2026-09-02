"""Primary geocoder — Open-Meteo Geocoding API.

Chosen as primary because it needs no API key, and because the weather adapters already
depend on Open-Meteo: no new vendor trust or reliability relationship is introduced.
Returns structured admin1/admin2/population/feature_code, which map directly onto
ResolvedLocation's existing state/district/hierarchy fields.

Searches globally and lets ranking.py apply the India bias — passing countryCode=IN here
would resolve "Springfield" to a Tamil Nadu hamlet (verified against the live API).
"""
from __future__ import annotations

from typing import Any

from app.adapters.http import get_client
from app.config import settings
from app.services.location_resolver.providers.base import LocationCandidate

SEARCH_URL = "https://geocoding-api.open-meteo.com/v1/search"


class OpenMeteoGeocoder:
    name = "open_meteo"

    async def search(self, query: str) -> list[LocationCandidate]:
        params: dict[str, Any] = {"name": query, "count": 10, "language": "en", "format": "json"}
        response = await get_client().get(SEARCH_URL, params=params,
                                          timeout=settings.geocoding_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []  # Open-Meteo omits "results" entirely on no match
        return [c for c in (self._to_candidate(item) for item in results) if c is not None]

    def _to_candidate(self, item: object) -> LocationCandidate | None:
        if not isinstance(item, dict):
            return None
        try:
            lat = float(item["latitude"])
            lon = float(item["longitude"])
            name = str(item["name"])
        except (KeyError, TypeError, ValueError):
            return None  # skip malformed rows rather than failing the whole response
        population = item.get("population")
        postcodes = item.get("postcodes")
        return LocationCandidate(
            name=name, lat=lat, lon=lon,
            country_code=item.get("country_code"),
            country=item.get("country"),
            admin1=item.get("admin1"),
            admin2=item.get("admin2"),
            population=int(population) if isinstance(population, (int, float)) else None,
            feature_code=item.get("feature_code"),
            postcode=postcodes[0] if isinstance(postcodes, list) and postcodes else None,
            timezone=item.get("timezone"),
            provider=self.name,
        )
