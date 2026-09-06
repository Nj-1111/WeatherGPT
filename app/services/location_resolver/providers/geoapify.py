"""Primary geocoder — Geoapify, falling back to Open-Meteo/Nominatim when unconfigured or empty (see __init__.py's _GEOCODERS order). Requires GEOAPIFY_API_KEY; search() returns [] rather than raising when unset, so an unconfigured deployment falls through to the free keyless chain exactly as before this provider existed."""
from __future__ import annotations

from typing import Any

from app.adapters.http import get_client
from app.config import settings
from app.services.location_resolver.providers.base import LocationCandidate

SEARCH_URL = "https://api.geoapify.com/v1/geocode/search"

# Geoapify's result_type values mapped onto GeoNames-style codes so ranking.py stays provider-agnostic (same approach nominatim.py takes for OSM's place types).
_CAPITAL_TYPES = {"city", "administrative"}


class GeoapifyGeocoder:
    name = "geoapify"

    async def search(self, query: str) -> list[LocationCandidate]:
        if not settings.geoapify_api_key:
            return []
        params: dict[str, Any] = {"text": query, "apiKey": settings.geoapify_api_key,
                                  "format": "json", "limit": 10}
        response = await get_client().get(SEARCH_URL, params=params,
                                          timeout=settings.geocoding_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [c for c in (self._to_candidate(item) for item in results) if c is not None]

    def _to_candidate(self, item: object) -> LocationCandidate | None:
        if not isinstance(item, dict):
            return None
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        name = item.get("city") or item.get("name")
        if not name:
            formatted = item.get("formatted")
            name = str(formatted).split(",")[0].strip() if formatted else None
        if not name:
            return None
        country_code = item.get("country_code")
        timezone = item.get("timezone")
        return LocationCandidate(
            name=str(name), lat=lat, lon=lon,
            country_code=country_code.upper() if isinstance(country_code, str) else None,
            country=item.get("country"),
            admin1=item.get("state"),
            admin2=item.get("county"),
            population=item.get("population") if isinstance(item.get("population"), (int, float)) else None,
            feature_code="PPLA" if item.get("result_type") in _CAPITAL_TYPES else "PPL",
            postcode=item.get("postcode"),
            timezone=timezone.get("name") if isinstance(timezone, dict) else None,
            provider=self.name,
        )
