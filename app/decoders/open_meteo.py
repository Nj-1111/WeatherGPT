"""Open-Meteo Ensemble → CEOs. No key. Uses Open-Meteo Ensemble API."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Dict, Any
import httpx
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

def decode_open_meteo(payload: Dict[str, Any], lat: float, lon: float) -> List[CanonicalEvidenceObject]:
    out: List[CanonicalEvidenceObject] = []
    geom = Geometry(type="GridCell", coordinates=[lon, lat], reference=f"{lat:.2f},{lon:.2f}")
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    precip = hourly.get("precipitation") or []
    precip_probability = hourly.get("precipitation_probability") or []
    temp = hourly.get("temperature_2m") or []
    wind = hourly.get("wind_speed_10m") or []
    issued = datetime.now(timezone.utc)

    # No artificial cap here: forecast_days (set by the caller from the query window)
    # already bounds what Open-Meteo returns. A fixed 48h slice used to sit here and
    # could silently drop the requested window entirely — "tomorrow afternoon" IST
    # lands past hour 48 depending on what time of day, UTC, the request is made.
    for i, t in enumerate(times):
        try:
            valid = datetime.fromisoformat(t.replace("Z","+00:00"))
            if valid.tzinfo is None:
                valid = valid.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if i < len(precip) and precip[i] is not None:
            # Open-Meteo precipitation is accumulation per hour (mm)
            out.append(CanonicalEvidenceObject(
                source="OPEN_METEO", evidence_class="forecast", variable="precipitation_amount",
                value=float(precip[i]), unit="mm", statistic="accumulation",
                geometry=geom, spatial_resolution="0.25°", issued_at=issued,
                valid_from=valid, valid_to=valid, accumulation_window_hours=1,
                provenance=Provenance(original_source="OPEN_METEO", original_unit="mm", transformations=["fetched Open-Meteo", "hourly precipitation"]))
            )
        if i < len(precip_probability) and precip_probability[i] is not None:
            probability = float(precip_probability[i]) / 100.0
            out.append(CanonicalEvidenceObject(
                source="OPEN_METEO", evidence_class="forecast", variable="precipitation_probability",
                value=probability, probability=probability, unit="probability", statistic="probability",
                geometry=geom, spatial_resolution="0.25°", issued_at=issued, valid_from=valid, valid_to=valid,
                provenance=Provenance(original_source="OPEN_METEO", original_unit="%", transformations=["fetched Open-Meteo", "hourly precipitation probability"]))
            )
        if i < len(temp) and temp[i] is not None:
            out.append(CanonicalEvidenceObject(
                source="OPEN_METEO", evidence_class="forecast", variable="temperature_2m",
                value=float(temp[i]), unit="C", statistic="instant",
                geometry=geom, issued_at=issued, valid_from=valid, valid_to=valid,
                provenance=Provenance(original_source="OPEN_METEO", original_unit="C", transformations=["fetched Open-Meteo"]))
            )
        if i < len(wind) and wind[i] is not None:
            out.append(CanonicalEvidenceObject(
                source="OPEN_METEO", evidence_class="forecast", variable="wind_speed",
                value=float(wind[i]), unit="km/h", statistic="instant",
                geometry=geom, issued_at=issued, valid_from=valid, valid_to=valid,
                provenance=Provenance(original_source="OPEN_METEO", original_unit="km/h", transformations=["fetched Open-Meteo"]))
            )
    return out

async def fetch_open_meteo(lat: float, lon: float, hourly: str = "temperature_2m,precipitation,precipitation_probability,wind_speed_10m", forecast_days: int = 3) -> Dict[str, Any]:
    params = {"latitude": lat, "longitude": lon, "hourly": hourly, "forecast_days": forecast_days, "timezone": "UTC"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(OPEN_METEO_FORECAST, params=params)
        r.raise_for_status()
        return r.json()
