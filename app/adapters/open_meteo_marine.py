"""Open-Meteo Marine Weather Adapter — real, key-free.

Primary marine source (app/orchestrator/retrieval_planner.py's "marine" family): wave
height/direction/period, ocean current velocity/direction, sea surface temperature.
Verified live field names/units against the real API before writing this — Open-Meteo
returns ocean_current_velocity in km/h, not m/s, which app/services/variable_registry.py
declares accordingly.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.adapters.base import WeatherSourceAdapter
from app.adapters.http import get_client
from app.config import settings
from app.constants import MARINE_HEALTH_PROBE_LAT, MARINE_HEALTH_PROBE_LON
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
_HOURLY_FIELDS = "wave_height,wave_direction,wave_period,ocean_current_velocity,ocean_current_direction,sea_surface_temperature"

# (payload key, canonical variable, unit)
_FIELD_MAP = (
    ("wave_height", "wave_height", "m"),
    ("wave_direction", "wave_direction", "deg"),
    ("wave_period", "wave_period", "s"),
    ("ocean_current_velocity", "ocean_current_velocity", "km/h"),
    ("ocean_current_direction", "ocean_current_direction", "deg"),
    ("sea_surface_temperature", "sea_surface_temperature", "C"),
)


class OpenMeteoMarineAdapter(WeatherSourceAdapter):
    source_name = "OPEN_METEO_MARINE"

    async def fetch(self, lat: float, lon: float, **kwargs) -> dict[str, Any]:
        forecast_days = kwargs.get("forecast_days", 3)
        params: dict[str, Any] = {"latitude": lat, "longitude": lon, "hourly": _HOURLY_FIELDS,
                                  "forecast_days": forecast_days, "timezone": "UTC"}
        response = await get_client().get(MARINE_URL, params=params)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw: dict[str, Any], lat: float, lon: float, **kwargs) -> list[CanonicalEvidenceObject]:
        hourly = raw.get("hourly") or {}
        times = hourly.get("time") or []
        series = {field: (hourly.get(field) or []) for field, _, _ in _FIELD_MAP}
        geom = Geometry(type="GridCell", coordinates=[lon, lat], reference=f"{lat:.2f},{lon:.2f}")
        issued = datetime.now(timezone.utc)

        out: list[CanonicalEvidenceObject] = []
        for i, t in enumerate(times):
            try:
                valid = datetime.fromisoformat(t.replace("Z", "+00:00"))
                if valid.tzinfo is None:
                    valid = valid.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue
            for field, variable, unit in _FIELD_MAP:
                values = series[field]
                if i >= len(values) or values[i] is None:
                    continue
                out.append(CanonicalEvidenceObject(
                    source="OPEN_METEO_MARINE", evidence_class="forecast", variable=variable,
                    value=float(values[i]), unit=unit, statistic="instant",
                    geometry=geom, issued_at=issued, valid_from=valid, valid_to=valid,
                    provenance=Provenance(original_source="OPEN_METEO_MARINE", original_unit=unit,
                                          transformations=["fetched Open-Meteo Marine"])))
        return out

    async def health_check(self) -> dict[str, Any]:
        start = time.time()
        try:
            response = await get_client().get(
                MARINE_URL,
                params={"latitude": MARINE_HEALTH_PROBE_LAT, "longitude": MARINE_HEALTH_PROBE_LON,
                        "hourly": "wave_height", "forecast_days": 1, "timezone": "UTC"},
                timeout=settings.source_timeout_seconds)
            response.raise_for_status()
            return {"available": True, "latency_ms": int((time.time() - start) * 1000), "reason": "ok"}
        except Exception as e:
            return {"available": False, "latency_ms": int((time.time() - start) * 1000), "reason": str(e)}
