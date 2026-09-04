"""MET Norway adapter — keyless, but requires an identifying User-Agent.

api.met.no returns 403 to generic User-Agents as a matter of policy, so the
identifying string is configuration, not decoration.
"""
from __future__ import annotations

import time
from typing import Any

from app.adapters.base import WeatherSourceAdapter
from app.adapters.http import get_client
from app.config import settings
from app.constants import HEALTH_PROBE_LAT, HEALTH_PROBE_LON
from app.decoders.met_norway import decode_met_norway
from app.schemas.ceo import CanonicalEvidenceObject

FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


class MetNorwayAdapter(WeatherSourceAdapter):
    source_name = "MET_NORWAY"

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": settings.met_norway_user_agent}

    async def fetch(self, lat: float, lon: float, **kwargs) -> dict[str, Any]:
        response = await get_client().get(FORECAST_URL, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                                          headers=self._headers())
        response.raise_for_status()
        return response.json()

    def normalize(self, raw: dict[str, Any], lat: float, lon: float, **kwargs) -> list[CanonicalEvidenceObject]:
        return decode_met_norway(raw, lat, lon)

    async def health_check(self) -> dict[str, Any]:
        started = time.time()
        try:
            response = await get_client().get(
                FORECAST_URL, params={"lat": HEALTH_PROBE_LAT, "lon": HEALTH_PROBE_LON},
                headers=self._headers(), timeout=settings.source_timeout_seconds)
            response.raise_for_status()
            return {"available": True, "latency_ms": int((time.time() - started) * 1000), "reason": "ok"}
        except Exception as exc:
            return {"available": False, "latency_ms": int((time.time() - started) * 1000), "reason": str(exc)}
