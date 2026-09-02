"""MET Norway locationforecast payload -> CEOs.

The Norwegian Meteorological Institute runs its own model chain, so this is the one
source in the registry that is genuinely independent of Open-Meteo. Without it,
"sources agree" compares one vendor against itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

# (payload key, canonical variable, unit, statistic)
_INSTANT_FIELDS = (
    ("air_temperature", "temperature_2m", "C", "instant"),
    ("wind_speed", "wind_speed", "m/s", "instant"),
    ("relative_humidity", "humidity", "%", "instant"),
    ("air_pressure_at_sea_level", "pressure_msl", "hPa", "instant"),
    ("cloud_area_fraction", "cloud_cover", "%", "instant"),
)

# Later steps in the series are 6-hourly, so the 1-hour block is absent there. The
# accumulation window is recorded per record; it is never assumed.
_PRECIPITATION_BLOCKS = (("next_1_hours", 1), ("next_6_hours", 6))


def _parse_time(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decode_met_norway(payload: dict[str, Any], lat: float, lon: float) -> list[CanonicalEvidenceObject]:
    properties = payload.get("properties") or {}
    issued = _parse_time((properties.get("meta") or {}).get("updated_at", "")) or datetime.now(timezone.utc)
    geometry = Geometry(type="Point", coordinates=[lon, lat], reference=f"{lat:.2f},{lon:.2f}")
    out: list[CanonicalEvidenceObject] = []

    for step in properties.get("timeseries") or []:
        valid = _parse_time(step.get("time", ""))
        if valid is None:
            continue
        data = step.get("data") or {}
        details = ((data.get("instant") or {}).get("details")) or {}
        for key, variable, unit, statistic in _INSTANT_FIELDS:
            value = details.get(key)
            if value is None:
                continue
            out.append(CanonicalEvidenceObject(
                source="MET_NORWAY", evidence_class="forecast", variable=variable,
                value=float(value), unit=unit, statistic=statistic, geometry=geometry,
                issued_at=issued, valid_from=valid, valid_to=valid,
                model_name="MET Norway locationforecast",
                provenance=Provenance(original_source="MET Norway", original_field=key, original_unit=unit,
                                      transformations=["fetched MET Norway locationforecast"]),
            ))

        for block, window_hours in _PRECIPITATION_BLOCKS:
            amount = ((data.get(block) or {}).get("details") or {}).get("precipitation_amount")
            if amount is None:
                continue
            out.append(CanonicalEvidenceObject(
                source="MET_NORWAY", evidence_class="forecast", variable="precipitation_amount",
                value=float(amount), unit="mm", statistic="accumulation", geometry=geometry,
                issued_at=issued, valid_from=valid, valid_to=valid,
                accumulation_window_hours=window_hours,
                model_name="MET Norway locationforecast",
                provenance=Provenance(original_source="MET Norway", original_field=f"{block}.precipitation_amount",
                                      original_unit="mm",
                                      transformations=["fetched MET Norway locationforecast",
                                                       f"{window_hours}h precipitation accumulation"]),
            ))
            break  # the finest available window only; never emit 1h and 6h for one step
    return out
