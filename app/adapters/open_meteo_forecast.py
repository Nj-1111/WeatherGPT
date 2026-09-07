"""Open-Meteo Forecast Adapter — real, key-free."""
from __future__ import annotations

import time
from typing import Any

from app.adapters.base import WeatherSourceAdapter
from app.adapters.http import get_client
from app.config import settings
from app.constants import HEALTH_PROBE_LAT, HEALTH_PROBE_LON
from app.decoders.open_meteo import decode_open_meteo
from app.schemas.ceo import CanonicalEvidenceObject

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

class OpenMeteoForecastAdapter(WeatherSourceAdapter):
    source_name = "OPEN_METEO"

    async def fetch(self, lat: float, lon: float, **kwargs) -> dict[str, Any]:
        hourly = kwargs.get("hourly", "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,relative_humidity_2m,pressure_msl,cloud_cover,visibility")
        forecast_days = kwargs.get("forecast_days", 3)
        params: dict[str, Any] = {"latitude": lat, "longitude": lon, "hourly": hourly, "forecast_days": forecast_days, "timezone": "UTC"}
        response = await get_client().get(FORECAST_URL, params=params)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw: dict[str, Any], lat: float, lon: float, **kwargs) -> list[CanonicalEvidenceObject]:
        return decode_open_meteo(raw, lat, lon)

    async def health_check(self) -> dict[str, Any]:
        start = time.time()
        try:
            response = await get_client().get(
                FORECAST_URL,
                params={"latitude": HEALTH_PROBE_LAT, "longitude": HEALTH_PROBE_LON, "hourly": "temperature_2m", "forecast_days": 1},
                timeout=settings.source_timeout_seconds)
            response.raise_for_status()
            return {"available": True, "latency_ms": int((time.time()-start)*1000), "reason": "ok"}
        except Exception as e:
            return {"available": False, "latency_ms": int((time.time()-start)*1000), "reason": str(e)}
