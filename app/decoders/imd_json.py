"""IMD JSON decoder → CEOs. Handles city forecast, current, nowcast, rainfall, district warning schemas."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.constants import IST
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

logger = logging.getLogger(__name__)


def _geometry(record: dict[str, Any], reference: str, geometry_type: str = "Point") -> Geometry | None:
    """No coordinate default — a record without a position used to inherit Nagpur's and rank by distance as though local, wrong-location evidence presented as authoritative."""
    lat, lon = record.get("lat"), record.get("lon")
    if lat is None or lon is None:
        logger.warning("imd.record_without_coordinates", extra={"reference": reference})
        return None
    try:
        return Geometry(type=geometry_type, coordinates=[float(lon), float(lat)], reference=reference)
    except (TypeError, ValueError):
        logger.warning("imd.record_with_invalid_coordinates", extra={"reference": reference})
        return None

def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            if fmt.endswith("%z") and s.endswith("Z"):
                s = s.replace("Z", "+0000")
            # handle IST offset like +0530
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception:
        return None

def _ceo_base(source_record_id, evidence_class, variable, value, unit, statistic, geometry,
              observed_at=None, issued_at=None, valid_from=None, valid_to=None,
              accumulation=None, raw_value=None, warning_severity=None, extra=None, source="IMD", model_name=None):
    return CanonicalEvidenceObject(
        source=source, source_record_id=str(source_record_id) if source_record_id else None,
        evidence_class=evidence_class, variable=variable, value=value, raw_value=raw_value, unit=unit,
        statistic=statistic, geometry=geometry,
        observed_at=observed_at, issued_at=issued_at, valid_from=valid_from, valid_to=valid_to,
        accumulation_window_hours=accumulation, warning_severity=warning_severity,
        model_name=model_name,
        provenance=Provenance(original_source=source, original_unit=unit,
                              transformations=["parsed IMD JSON", f"mapped {variable}"],
                              raw_record_id=str(source_record_id) if source_record_id else None),
        extra=extra or {}
    )

def decode_city_forecast(record: dict[str, Any]) -> list[CanonicalEvidenceObject]:
    """Example fields (vary): city, forecast_date, temp_max, temp_min, rainfall, humidity, wind_speed, issued_at."""
    out: list[CanonicalEvidenceObject] = []
    city = record.get("city") or record.get("station") or "unknown"
    geom = _geometry(record, city)
    if geom is None:
        return []
    issued = _parse_time(record.get("issued_at") or record.get("date"))
    valid_from = _parse_time(record.get("forecast_date") or record.get("valid_from"))
    valid_to = (valid_from + timedelta(hours=24)) if valid_from else None
    if "rainfall" in record:
        out.append(_ceo_base(city, "forecast", "precipitation_amount", float(record["rainfall"]), "mm", "accumulation", geom, issued_at=issued, valid_from=valid_from, valid_to=valid_to, accumulation=24, source="IMD"))
    if "temp_max" in record:
        out.append(_ceo_base(city, "forecast", "temperature_max", float(record["temp_max"]), "C", "max", geom, issued_at=issued, valid_from=valid_from, valid_to=valid_to, source="IMD"))
    if "temp_min" in record:
        out.append(_ceo_base(city, "forecast", "temperature_min", float(record["temp_min"]), "C", "min", geom, issued_at=issued, valid_from=valid_from, valid_to=valid_to, source="IMD"))
    if "wind_speed" in record:
        out.append(_ceo_base(city, "forecast", "wind_speed", float(record["wind_speed"]), "km/h", "instant", geom, issued_at=issued, valid_from=valid_from, valid_to=valid_to, source="IMD"))
    return out

def decode_current(record: dict[str, Any]) -> list[CanonicalEvidenceObject]:
    geom = _geometry(record, record.get("station", "unknown"))
    if geom is None:
        return []
    observed = _parse_time(record.get("observed_at") or record.get("time"))
    out = []
    if "temperature" in record:
        out.append(_ceo_base(record.get("station"), "observation", "temperature_2m", float(record["temperature"]), "C", "instant", geom, observed_at=observed, source="IMD"))
    if "rainfall" in record:
        out.append(_ceo_base(record.get("station"), "observation", "precipitation_amount", float(record["rainfall"]), "mm", "accumulation", geom, observed_at=observed, accumulation=24, source="IMD"))
    return out

def decode_warning(record: dict[str, Any]) -> list[CanonicalEvidenceObject]:
    """District warning: hazards as category codes + colour/severity, e.g. {district, issue_time, valid_from, valid_to, hazard, colour, severity, category_code}."""
    district = record.get("district", "unknown")
    geom = Geometry(type="Polygon", coordinates=None, reference=district)
    issued = _parse_time(record.get("issue_time") or record.get("issued_at"))
    valid_from = _parse_time(record.get("valid_from"))
    valid_to = _parse_time(record.get("valid_to"))
    colour = (record.get("colour") or record.get("color") or record.get("severity") or "yellow").lower()
    hazard = record.get("hazard") or record.get("event") or "heavy rainfall"
    return [_ceo_base(record.get("district"), "warning", "heavy_rain_warning", None, None, "categorical", geom, issued_at=issued, valid_from=valid_from, valid_to=valid_to, raw_value=hazard, warning_severity=colour, extra={"category_code": record.get("category_code")}, source="IMD")]

def decode_nowcast(record: dict[str, Any]) -> list[CanonicalEvidenceObject]:
    """Nowcast with category codes."""
    station = record.get("station") or record.get("district") or "unknown"
    geom = _geometry(record, station)
    if geom is None:
        return []
    issued = _parse_time(record.get("issue_time") or record.get("issued_at"))
    valid_from = _parse_time(record.get("valid_from"))
    valid_to = _parse_time(record.get("valid_to"))
    code = record.get("category") or record.get("code") or record.get("condition")
    return [_ceo_base(station, "nowcast", "thunderstorm_probability", None, None, "categorical", geom, issued_at=issued, valid_from=valid_from, valid_to=valid_to, raw_value=str(code), extra={"condition": code}, source="IMD")]

def decode_rainfall(record: dict[str, Any]) -> list[CanonicalEvidenceObject]:
    """Rainfall: actual, normal, departure %, category."""
    district = record.get("district", "unknown")
    geom = Geometry(type="Polygon", coordinates=None, reference=district)
    valid = _parse_time(record.get("date"))
    issued = _parse_time(record.get("issued_at"))
    out = []
    if "actual" in record:
        out.append(_ceo_base(district, "observation", "precipitation_amount", float(record["actual"]), "mm", "accumulation", geom, observed_at=valid, issued_at=issued, accumulation=24, source="IMD"))
    return out

def decode(record: dict[str,Any], product: str = "forecast") -> list[CanonicalEvidenceObject]:
    table = {
        "forecast": decode_city_forecast,
        "city_forecast": decode_city_forecast,
        "current": decode_current,
        "observation": decode_current,
        "warning": decode_warning,
        "district_warning": decode_warning,
        "nowcast": decode_nowcast,
        "rainfall": decode_rainfall,
    }
    fn = table.get(product, decode_city_forecast)
    return fn(record)
