"""Fallback geocoder — OpenStreetMap Nominatim, covering what Open-Meteo's populated-places dataset misses (verified live): Indian districts, states, small towns, historical aliases. Used only after the primary yields nothing, keeping volume inside OSM's ~1 req/sec fair-use policy. Data is ODbL-licensed; deployments surfacing it should credit "© OpenStreetMap contributors"."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.adapters.http import get_client
from app.config import settings
from app.services.location_resolver.providers.base import LocationCandidate

SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# OSM place ranks, mapped onto GeoNames-style codes so ranking.py stays provider-agnostic.
_CAPITAL_TYPES = {"city", "administrative"}


class NominatimGeocoder:
    name = "nominatim"

    # OSM's fair-use policy is a hard 1 req/sec, IP-blocked if exceeded, and nothing else enforces it, so the provider paces itself — deliberately a single process-wide lock (a known shared-bottleneck tradeoff, see §2.3 in docs/REPORT.md), not per-client.
    _lock = asyncio.Lock()
    _last_request_at = 0.0

    @classmethod
    async def _throttle(cls) -> None:
        async with cls._lock:
            wait = cls._last_request_at + settings.nominatim_min_interval_seconds - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_request_at = time.monotonic()

    async def search(self, query: str) -> list[LocationCandidate]:
        params: dict[str, Any] = {"q": query, "format": "json", "limit": 5, "addressdetails": 1}
        await self._throttle()
        response = await get_client().get(SEARCH_URL, params=params,
                                          headers={"User-Agent": settings.nominatim_user_agent},
                                          timeout=settings.geocoding_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [c for c in (self._to_candidate(item) for item in payload) if c is not None]

    def _to_candidate(self, item: object) -> LocationCandidate | None:
        if not isinstance(item, dict):
            return None
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        raw_address = item.get("address")
        address = raw_address if isinstance(raw_address, dict) else {}
        name = item.get("name") or address.get("city") or address.get("state") or ""
        if not name:
            display = item.get("display_name")
            name = str(display).split(",")[0].strip() if display else ""
        if not name:
            return None
        country_code = address.get("country_code")
        district = address.get("state_district") or address.get("county")
        # Nominatim has no population; approximate importance via place type so a city outranks a same-named hamlet in ranking.py.
        feature_code = "PPLA" if item.get("type") in _CAPITAL_TYPES else "PPL"
        return LocationCandidate(
            name=str(name), lat=lat, lon=lon,
            country_code=country_code.upper() if isinstance(country_code, str) else None,
            country=address.get("country"),
            admin1=address.get("state"),
            admin2=district,
            population=None,
            feature_code=feature_code,
            postcode=address.get("postcode"),
            provider=self.name,
        )
