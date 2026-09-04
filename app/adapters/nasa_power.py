"""NASA POWER Adapter — historical independent observations."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.adapters.base import WeatherSourceAdapter
from app.adapters.http import get_client
from app.config import settings
from app.constants import HEALTH_PROBE_DATE, HEALTH_PROBE_LAT, HEALTH_PROBE_LON
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

POWER_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"

class NasaPowerAdapter(WeatherSourceAdapter):
    source_name = "NASA_POWER"

    async def fetch(self, lat: float, lon: float, start: str, end: str, **kwargs) -> dict[str, Any]:
        params: dict[str, Any] = {"parameters": "T2M,PRECTOTCORR", "community": "AG", "longitude": lon, "latitude": lat, "start": start, "end": end, "format": "JSON"}
        response = await get_client().get(POWER_URL, params=params)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw: dict[str, Any], lat: float, lon: float, **kwargs) -> list[CanonicalEvidenceObject]:
        props = raw.get("properties", {}).get("parameter", {})
        t2m = props.get("T2M", {})
        precip = props.get("PRECTOTCORR", {})
        out: list[CanonicalEvidenceObject] = []
        geom = Geometry(type="Point", coordinates=[lon, lat])
        for k, v in t2m.items():
            try:
                # POWER hourly keys like 2024010100
                dt = datetime.strptime(k, "%Y%m%d%H")
                dt = dt.replace(tzinfo=timezone.utc)
                out.append(CanonicalEvidenceObject(source="NASA_POWER", evidence_class="observation", variable="temperature_2m", value=float(v), unit="C", statistic="instant", geometry=geom, observed_at=dt, valid_from=dt, valid_to=dt, provenance=Provenance(original_source="NASA POWER", original_unit="C", transformations=["fetched POWER"])))
            except (TypeError, ValueError):
                continue
        for k, v in precip.items():
            try:
                dt = datetime.strptime(k, "%Y%m%d%H")
                dt = dt.replace(tzinfo=timezone.utc)
                out.append(CanonicalEvidenceObject(source="NASA_POWER", evidence_class="observation", variable="precipitation_amount", value=float(v), unit="mm", statistic="accumulation", geometry=geom, valid_from=dt, valid_to=dt, accumulation_window_hours=1, provenance=Provenance(original_source="NASA POWER", original_unit="mm", transformations=["fetched POWER"])))
            except (TypeError, ValueError):
                continue
        return out

    async def health_check(self) -> dict[str, Any]:
        start=time.time()
        try:
            response = await get_client().get(
                POWER_URL,
                params={"parameters": "T2M", "community": "AG", "longitude": HEALTH_PROBE_LON,
                        "latitude": HEALTH_PROBE_LAT, "start": HEALTH_PROBE_DATE,
                        "end": HEALTH_PROBE_DATE, "format": "JSON"},
                timeout=settings.source_timeout_seconds)
            response.raise_for_status()
            return {"available": True, "latency_ms": int((time.time()-start)*1000), "reason": "ok"}
        except Exception as e:
            return {"available": False, "latency_ms": int((time.time()-start)*1000), "reason": str(e)}
