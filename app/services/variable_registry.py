"""
Variable registry — semantic normalization gate.
Maps native field names → canonical variable + statistic + allowed accumulation windows.
If semantics differ, values are NOT comparable (never averaged).
"""
from __future__ import annotations

from collections import defaultdict

# 16+ canonical vars with compatible statistics, units, windows, evidence-class restrictions
DEFAULT_REGISTRY: dict[str, dict] = {
    # precipitation family — NOT interchangeable
    "apcp": {"canonical": "precipitation_amount", "statistic": "accumulation", "unit": "kg m-2", "accumulation_hours": [1,3,6,24], "evidence_class": ["forecast","observation","reanalysis"], "note": "GRIB APCP, check accumulation_window"},
    "tp": {"canonical": "precipitation_amount", "statistic": "accumulation", "unit": "mm", "accumulation_hours": [1,3,6,24], "evidence_class": ["forecast","observation"]},
    "precipitation": {"canonical": "precipitation_amount", "statistic": "accumulation", "unit": "mm", "accumulation_hours": [1,3,6,24]},
    "rainfall": {"canonical": "precipitation_amount", "statistic": "accumulation", "unit": "mm", "accumulation_hours": [1,3,6,24]},
    "rain": {"canonical": "precipitation_amount", "statistic": "accumulation", "unit": "mm", "accumulation_hours": [1,3,6,24]},
    "precipitation_probability": {"canonical": "precipitation_probability", "statistic": "probability", "unit": "%", "evidence_class": ["forecast"]},
    "pop": {"canonical": "precipitation_probability", "statistic": "probability", "unit": "%"},
    "precip_prob": {"canonical": "precipitation_probability", "statistic": "probability", "unit": "%"},
    "rain_rate": {"canonical": "precipitation_rate", "statistic": "instant", "unit": "mm/h", "evidence_class": ["observation","radar"]},
    "prate": {"canonical": "precipitation_rate", "statistic": "instant", "unit": "mm/h"},
    "precipitation_rate": {"canonical": "precipitation_rate", "statistic": "instant", "unit": "mm/h"},
    # temperature
    "t2m": {"canonical": "temperature_2m", "statistic": "instant", "unit": "K", "evidence_class": ["forecast","observation","reanalysis"]},
    "2t": {"canonical": "temperature_2m", "statistic": "instant", "unit": "K"},
    "temperature": {"canonical": "temperature_2m", "statistic": "instant", "unit": "C"},
    "temperature_2m": {"canonical": "temperature_2m", "statistic": "instant", "unit": "C"},
    "tmax": {"canonical": "temperature_max", "statistic": "max", "unit": "C"},
    "tmin": {"canonical": "temperature_min", "statistic": "min", "unit": "C"},
    # wind
    "wind_speed": {"canonical": "wind_speed", "statistic": "instant", "unit": "m/s", "evidence_class": ["forecast","observation"]},
    "wind_gust": {"canonical": "wind_gust", "statistic": "instant", "unit": "m/s"},
    "windgust": {"canonical": "wind_gust", "statistic": "instant", "unit": "m/s"},
    "u10": {"canonical": "wind_speed", "statistic": "instant", "unit": "m/s"},
    "v10": {"canonical": "wind_speed", "statistic": "instant", "unit": "m/s"},
    # humidity/pressure/visibility/cloud
    "humidity": {"canonical": "humidity", "statistic": "instant", "unit": "%"},
    "relative_humidity": {"canonical": "humidity", "statistic": "instant", "unit": "%"},
    "rh": {"canonical": "humidity", "statistic": "instant", "unit": "%"},
    "pressure": {"canonical": "pressure_msl", "statistic": "instant", "unit": "hPa"},
    "pressure_msl": {"canonical": "pressure_msl", "statistic": "instant", "unit": "hPa"},
    "msl": {"canonical": "pressure_msl", "statistic": "instant", "unit": "hPa"},
    "visibility": {"canonical": "visibility", "statistic": "instant", "unit": "km"},
    "cloud_cover": {"canonical": "cloud_cover", "statistic": "instant", "unit": "%"},
    "cloudcover": {"canonical": "cloud_cover", "statistic": "instant", "unit": "%"},
    # warnings — categorical, evidence_class warning only
    "heavy rainfall": {"canonical": "heavy_rain_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "heavy_rain_warning": {"canonical": "heavy_rain_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "thunderstorm": {"canonical": "thunderstorm_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "thunderstorm_warning": {"canonical": "thunderstorm_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "cyclone": {"canonical": "cyclone_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "cyclone_warning": {"canonical": "cyclone_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "heat wave": {"canonical": "heat_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "heat_warning": {"canonical": "heat_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "flood": {"canonical": "flood_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "flood_warning": {"canonical": "flood_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "marine": {"canonical": "marine_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    "marine_warning": {"canonical": "marine_warning", "statistic": "categorical", "unit": None, "evidence_class": ["warning"]},
    # Declared in CanonicalVariable but previously absent here, so the gate rejected every
    # record carrying them as an "unknown canonical variable" — silently discarding all of
    # IMD's nowcast output, from the highest-authority source in the table.
    "thunderstorm_probability": {"canonical": "thunderstorm_probability", "statistic": "probability", "unit": "%", "evidence_class": ["forecast", "nowcast"]},
    "thunderstorm_category": {"canonical": "thunderstorm_probability", "statistic": "categorical", "unit": None, "evidence_class": ["forecast", "nowcast", "warning"]},
    "wind_direction": {"canonical": "wind_direction", "statistic": "instant", "unit": "deg", "evidence_class": ["forecast", "observation", "reanalysis"]},
    "wdir": {"canonical": "wind_direction", "statistic": "instant", "unit": "deg"},
    # A summary row standing in for many ensemble members, not a measurement.
    "rainfall_distribution": {"canonical": "rainfall_distribution", "statistic": "instant", "unit": "members", "evidence_class": ["forecast"]},
}

_BY_CANONICAL: dict[str, list[dict]] = defaultdict(list)
for _entry in DEFAULT_REGISTRY.values():
    _BY_CANONICAL[_entry["canonical"]].append(_entry)


def validate_semantics(variable: str, statistic: str, unit: str | None,
                       evidence_class: str, accumulation_hours: float | None) -> tuple[bool, str]:
    """Validate a CEO against canonical semantics rather than trusting a decoder or an LLM."""
    if variable == "other":
        # A deliberate escape hatch in CanonicalVariable. Accepted without a semantic
        # guarantee, because silently discarding evidence a decoder declined to type is
        # worse than admitting it unvalidated and letting the ranker weigh it.
        return True, "unclassified variable accepted without semantic guarantee"
    entries = _BY_CANONICAL.get(variable)
    if not entries:
        return False, f"unknown canonical variable {variable}"
    allowed_statistics = {entry["statistic"] for entry in entries}
    if statistic not in allowed_statistics:
        return False, f"{variable} does not support statistic {statistic}"
    restrictions = [entry["evidence_class"] for entry in entries if entry.get("evidence_class")]
    if restrictions and not any(evidence_class in allowed for allowed in restrictions):
        return False, f"{variable} is not valid for evidence class {evidence_class}"
    if statistic == "accumulation":
        if not accumulation_hours or accumulation_hours <= 0:
            return False, "accumulation requires a positive accumulation window"
        allowed_windows: set[float] = {window for entry in entries for window in entry.get("accumulation_hours", [])}
        if allowed_windows and accumulation_hours not in allowed_windows:
            return False, f"unsupported accumulation window {accumulation_hours}h"
    if statistic == "probability" and unit not in {None, "%", "1", "probability"}:
        return False, f"invalid probability unit {unit}"
    return True, "valid"
