"""StormGlass Adapter — fallback marine source, requires STORMGLASS_API_KEY.

Chosen as the marine fallback because it's a real, independent wave/current-capable API
with a usable free tier, mirroring how MET Norway was added as the first weather source
genuinely independent of Open-Meteo. Same missing-key convention as ImdAdapter: fetch()
raises rather than returning empty, and retrieval.py's existing per-source isolation
handles that — no new "unconfigured" plumbing needed here.

Built to StormGlass's documented API shape (each parameter keyed by a further dict of
per-model readings, "sg" being their blended consensus model). Unlike
open_meteo_marine.py, this has NOT been live-verified against a real key — StormGlass
requires paid signup, unlike Open-Meteo's keyless API. Smoke-test against a real key
before depending on this in production.
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

POINT_URL = "https://api.stormglass.io/v2/weather/point"
_PARAMS = "waveHeight,wavePeriod,waveDirection,currentSpeed,currentDirection,waterTemperature"

# (payload key, canonical variable, unit)
_FIELD_MAP = (
    ("waveHeight", "wave_height", "m"),
    ("waveDirection", "wave_direction", "deg"),
    ("wavePeriod", "wave_period", "s"),
    ("currentSpeed", "ocean_current_velocity", "m/s"),
    ("currentDirection", "ocean_current_direction", "deg"),
    ("waterTemperature", "sea_surface_temperature", "C"),
)
# StormGlass blends several underlying models per field; "sg" is their own consensus
# read. Fall back to whatever model is present when "sg" itself is missing.
_PREFERRED_MODEL = "sg"


def _reading(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    if _PREFERRED_MODEL in value and isinstance(value[_PREFERRED_MODEL], (int, float)):
        return float(value[_PREFERRED_MODEL])
    for candidate in value.values():
        if isinstance(candidate, (int, float)):
            return float(candidate)
    return None


class StormglassAdapter(WeatherSourceAdapter):
    source_name = "STORMGLASS"

    async def fetch(self, lat: float, lon: float, **kwargs) -> dict[str, Any]:
        if not settings.stormglass_api_key:
            raise RuntimeError("STORMGLASS_API_KEY missing — StormGlass source unavailable")
        params: dict[str, Any] = {"lat": lat, "lng": lon, "params": _PARAMS}
        response = await get_client().get(POINT_URL, params=params,
                                          headers={"Authorization": settings.stormglass_api_key})
        response.raise_for_status()
        return response.json()

    def normalize(self, raw: dict[str, Any], lat: float, lon: float, **kwargs) -> list[CanonicalEvidenceObject]:
        hours = raw.get("hours")
        if not isinstance(hours, list):
            return []
        geom = Geometry(type="GridCell", coordinates=[lon, lat], reference=f"{lat:.2f},{lon:.2f}")
        issued = datetime.now(timezone.utc)

        out: list[CanonicalEvidenceObject] = []
        for hour in hours:
            if not isinstance(hour, dict):
                continue
            try:
                valid = datetime.fromisoformat(str(hour.get("time")).replace("Z", "+00:00"))
                if valid.tzinfo is None:
                    valid = valid.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue
            for field, variable, unit in _FIELD_MAP:
                value = _reading(hour.get(field))
                if value is None:
                    continue
                out.append(CanonicalEvidenceObject(
                    source="STORMGLASS", evidence_class="forecast", variable=variable,
                    value=value, unit=unit, statistic="instant",
                    geometry=geom, issued_at=issued, valid_from=valid, valid_to=valid,
                    provenance=Provenance(original_source="STORMGLASS", original_unit=unit,
                                          transformations=["fetched StormGlass", f"model={_PREFERRED_MODEL}"])))
        return out

    async def health_check(self) -> dict[str, Any]:
        if not settings.stormglass_api_key:
            return {"available": False, "latency_ms": 0, "reason": "STORMGLASS_API_KEY not configured"}
        start = time.time()
        try:
            await self.fetch(lat=MARINE_HEALTH_PROBE_LAT, lon=MARINE_HEALTH_PROBE_LON)
            return {"available": True, "latency_ms": int((time.time() - start) * 1000), "reason": "ok"}
        except Exception as e:
            return {"available": False, "latency_ms": int((time.time() - start) * 1000), "reason": str(e)}
