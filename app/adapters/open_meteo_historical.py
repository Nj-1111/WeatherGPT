"""Open-Meteo Historical / ERA5 Adapter — real, key-free, reanalysis."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.adapters.base import WeatherSourceAdapter
from app.adapters.http import get_client
from app.config import settings
from app.constants import HEALTH_PROBE_LAT, HEALTH_PROBE_LON
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

URL_ERA5 = "https://archive-api.open-meteo.com/v1/era5"

class OpenMeteoHistoricalAdapter(WeatherSourceAdapter):
    source_name = "ERA5"

    async def fetch(self, lat: float, lon: float, start_date: str, end_date: str, **kwargs) -> dict[str, Any]:
        params: dict[str, Any] = {"latitude": lat, "longitude": lon, "start_date": start_date, "end_date": end_date, "hourly": "temperature_2m,precipitation,wind_speed_10m", "timezone": "UTC"}
        response = await get_client().get(URL_ERA5, params=params)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw: dict[str, Any], lat: float, lon: float, **kwargs) -> list[CanonicalEvidenceObject]:
        hourly = raw.get("hourly", {})
        times = hourly.get("time", [])
        t2m = hourly.get("temperature_2m", [])
        precip = hourly.get("precipitation", [])
        out: list[CanonicalEvidenceObject] = []
        for i, t in enumerate(times):
            try:
                valid = datetime.fromisoformat(t.replace("Z","+00:00"))
                if valid.tzinfo is None:
                    valid = valid.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            geom = Geometry(type="GridCell", coordinates=[lon, lat])
            if i < len(t2m) and t2m[i] is not None:
                out.append(CanonicalEvidenceObject(source="ERA5", evidence_class="reanalysis", variable="temperature_2m", value=float(t2m[i]), unit="C", statistic="instant", geometry=geom, valid_from=valid, valid_to=valid, provenance=Provenance(original_source="ERA5", original_unit="C", transformations=["fetched ERA5 reanalysis"])))
            if i < len(precip) and precip[i] is not None:
                out.append(CanonicalEvidenceObject(source="ERA5", evidence_class="reanalysis", variable="precipitation_amount", value=float(precip[i]), unit="mm", statistic="accumulation", geometry=geom, valid_from=valid, valid_to=valid, accumulation_window_hours=1, provenance=Provenance(original_source="ERA5", original_unit="mm", transformations=["fetched ERA5 reanalysis"])))
        return out

    async def health_check(self) -> dict[str, Any]:
        start=time.time()
        try:
            response = await get_client().get(
                URL_ERA5,
                params={"latitude": HEALTH_PROBE_LAT, "longitude": HEALTH_PROBE_LON,
                        "start_date": "2024-01-01", "end_date": "2024-01-02",
                        "hourly": "temperature_2m", "timezone": "UTC"},
                timeout=settings.source_timeout_seconds)
            response.raise_for_status()
            return {"available":True,"latency_ms":int((time.time()-start)*1000),"reason":"ok"}
        except Exception as e:
            return {"available":False,"latency_ms":int((time.time()-start)*1000),"reason":str(e)}
