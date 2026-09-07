from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from app.config import settings
from app.schemas.ceo import CanonicalEvidenceObject
from app.schemas.wio import (
    EvidenceSummary,
    WeatherIntelligenceObject,
    WIOAgreement,
    WIOQuery,
    WIOWarning,
    WIOWeather,
)
from app.services.ranker import corroborated, detect_disagreements, rank
from app.services.spatial_match import area_names_query, covers_query
from app.services.units import as_kmh

_SEVERITY_ORDER = {"green": 0, "yellow": 1, "orange": 2, "red": 3}


def _best_source(scored, variable: str) -> str | None:
    """The highest-ranked source reporting a variable. Aggregation then uses only that
    source's records, so a total is never assembled from two different models."""
    for _, ev in scored:
        if ev.variable == variable and ev.ensemble_member is None and ev.value is not None:
            return ev.source
    return None


def _as_probability(ev: CanonicalEvidenceObject) -> float:
    """Sources report probability either as a 0-1 field or as a percentage value."""
    if ev.probability is not None:
        return ev.probability
    return (ev.value or 0.0) / 100


def _series(scored, variable: str, source) -> list[CanonicalEvidenceObject]:
    return [ev for _, ev in scored
            if ev.variable == variable and ev.source == source
            and ev.ensemble_member is None and ev.value is not None]


def _rain_panel(scored) -> tuple[dict | None, str]:
    source = _best_source(scored, "precipitation_amount")
    if source is None:
        return None, ""

    amounts = _series(scored, "precipitation_amount", source)
    # Accumulations only sum within one window length (mixing 1h/6h records double-counts overlapping time); prefer the finest window available.
    by_window = defaultdict(list)
    for ev in amounts:
        by_window[ev.accumulation_window_hours].append(ev)
    window_hours = min(by_window, key=lambda hours: (hours is None, hours))
    contributing = by_window[window_hours]
    hourly = [ev.value for ev in contributing if ev.value is not None]
    total = sum(hourly)

    probabilities = _series(scored, "precipitation_probability", source) or \
        _series(scored, "precipitation_probability", _best_source(scored, "precipitation_probability"))
    peak = None
    if probabilities:
        peak_ev = max(probabilities, key=_as_probability)
        peak = _as_probability(peak_ev)

    starts = [ev.valid_from for ev in contributing if ev.valid_from]
    ends = [ev.valid_to for ev in contributing if ev.valid_to]
    panel = {
        "value_mm": round(total, 2),
        "unit": "mm",
        "variable": "precipitation_amount",
        "accumulation_hours": window_hours,
        "aggregation": "sum over query window",
        "hours_counted": len(contributing),
        "peak_hourly_mm": round(max(hourly), 2) if hourly else None,
        "probability": round(peak, 2) if peak is not None else None,
        "source": source,
        "valid_from": min(starts).isoformat() if starts else None,
        "valid_to": max(ends).isoformat() if ends else None,
        "evidence_ids": [ev.evidence_id for ev in contributing]
                        + [ev.evidence_id for ev in probabilities[:1]],
    }

    members = defaultdict(list)
    for _, ev in scored:
        if ev.variable == "precipitation_amount" and ev.ensemble_member is not None and ev.value is not None:
            members[ev.valid_from].append(ev.value)
    if members:
        # One timestamp's spread across members is a distribution; pooling every timestamp would smear it into a shape no single moment ever had.
        wettest = max(members.values(), key=lambda values: sum(values))
        panel["member_values"] = wettest
        panel["member_count"] = len(wettest)

    if peak is None:
        summary = f"Forecast precipitation {total:.1f} mm over the window ({source})."
    elif peak >= settings.rain_likely_probability:
        summary = f"Rain likely ({peak:.0%} peak probability, {total:.1f} mm expected over the window)."
    elif peak >= settings.rain_possible_probability:
        summary = f"Rain possible ({peak:.0%} peak probability, {total:.1f} mm)."
    else:
        summary = f"Rain unlikely ({peak:.0%} peak probability, {total:.1f} mm)."
    return panel, summary


def _temperature_panel(scored) -> dict | None:
    for variable in ("temperature_2m", "temperature_max"):
        source = _best_source(scored, variable)
        if source is None:
            continue
        series = _series(scored, variable, source)
        values = [ev.value for ev in series if ev.value is not None]
        return {"min": round(min(values), 1), "max": round(max(values), 1),
                "unit": series[0].unit or "C", "source": source, "variable": variable,
                "aggregation": "range over query window",
                "evidence_ids": [ev.evidence_id for ev in series]}
    return None


def _range_panel(scored, variable: str, unit_default: str) -> dict | None:
    """Generic min-max-range panel for a single-canonical-name field (humidity, pressure,
    cloud cover) — same shape as _temperature_panel without needing to try multiple names."""
    source = _best_source(scored, variable)
    if source is None:
        return None
    series = _series(scored, variable, source)
    values = [ev.value for ev in series if ev.value is not None]
    return {"min": round(min(values), 1), "max": round(max(values), 1),
            "unit": series[0].unit or unit_default, "source": source, "variable": variable,
            "aggregation": "range over query window",
            "evidence_ids": [ev.evidence_id for ev in series]}


def _visibility_panel(scored) -> dict | None:
    """Minimum (worst-case) visibility over the window — the hazard-relevant reading for
    driving/travel, not a range or peak."""
    source = _best_source(scored, "visibility")
    if source is None:
        return None
    series = _series(scored, "visibility", source)
    worst = min(series, key=lambda ev: ev.value if ev.value is not None else float("inf"))
    return {"value_km": round((worst.value or 0.0) / 1000, 2), "variable": "visibility",
            "from_unit": worst.unit, "unit": "km",
            "aggregation": "minimum over query window", "source": source,
            "evidence_ids": [worst.evidence_id]}


def _heat_stress_panel(temperature: dict | None, humidity: dict | None, wind: dict | None) -> dict | None:
    """Derived from already-fused panels, not raw evidence: heat index when hot+humid,
    wind chill when cold+windy, absent otherwise (neither is a hazard-relevant reading).
    NWS formulas (Fahrenheit-native), converted at the boundary since this system reports
    Celsius everywhere else. If a window spans both a hot and a cold extreme, heat is
    checked first — a known simplification, not expected to matter for same-day windows."""
    if temperature is None:
        return None
    if temperature["max"] >= 27 and humidity is not None:
        temp_c, rh = temperature["max"], humidity["max"]
        tf = temp_c * 9 / 5 + 32
        if tf >= 80:
            hi_f = (-42.379 + 2.04901523 * tf + 10.14333127 * rh - 0.22475541 * tf * rh
                    - 0.00683783 * tf * tf - 0.05481717 * rh * rh + 0.00122874 * tf * tf * rh
                    + 0.00085282 * tf * rh * rh - 0.00000199 * tf * tf * rh * rh)
        else:
            hi_f = 0.5 * (tf + 61.0 + ((tf - 68.0) * 1.2) + (rh * 0.094))
        return {"index": "heat_index", "value_c": round((hi_f - 32) * 5 / 9, 1), "unit": "C",
                "basis": {"temperature_c": temp_c, "humidity_pct": rh},
                "evidence_ids": list(temperature["evidence_ids"]) + list(humidity["evidence_ids"])}
    if temperature["min"] <= 10 and wind is not None:
        temp_c, wind_kmh = temperature["min"], wind["value_kmh"]
        v_mph = wind_kmh * 0.621371
        if v_mph >= 3:
            tf = temp_c * 9 / 5 + 32
            wc_f = 35.74 + 0.6215 * tf - 35.75 * (v_mph ** 0.16) + 0.4275 * tf * (v_mph ** 0.16)
            return {"index": "wind_chill", "value_c": round((wc_f - 32) * 5 / 9, 1), "unit": "C",
                    "basis": {"temperature_c": temp_c, "wind_kmh": wind_kmh},
                    "evidence_ids": list(temperature["evidence_ids"]) + list(wind["evidence_ids"])}
    return None


def _wind_panel(scored) -> dict | None:
    source = _best_source(scored, "wind_speed")
    if source is None:
        return None
    series = _series(scored, "wind_speed", source)
    peak = max(series, key=as_kmh)
    return {"value_kmh": round(as_kmh(peak), 1), "variable": "wind_speed",
            "from_unit": peak.unit, "unit": "km/h",
            "aggregation": "maximum over query window", "source": source,
            "evidence_ids": [peak.evidence_id]}


# Hazard-relevant fields report the peak over the window (matches _wind_panel); direction/period/temperature fields report the most recent reading instead, since maxing those doesn't mean anything.
_MARINE_PEAK_FIELDS = (("wave_height", "wave_height_m"), ("ocean_current_velocity", "current_velocity_kmh"))
_MARINE_LATEST_FIELDS = (("wave_direction", "wave_direction_deg"), ("wave_period", "wave_period_s"),
                         ("ocean_current_direction", "current_direction_deg"),
                         ("sea_surface_temperature", "sea_surface_temp_c"))


def _marine_panel(scored) -> dict | None:
    panel: dict[str, object] = {}
    evidence_ids: list[str] = []
    sources: set[str] = set()

    for variable, key in _MARINE_PEAK_FIELDS:
        source = _best_source(scored, variable)
        if source is None:
            continue
        series = _series(scored, variable, source)
        peak = max(series, key=lambda ev: ev.value or 0.0)
        panel[key] = round(peak.value or 0.0, 2)
        evidence_ids.append(peak.evidence_id)
        sources.add(source)

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for variable, key in _MARINE_LATEST_FIELDS:
        source = _best_source(scored, variable)
        if source is None:
            continue
        series = _series(scored, variable, source)
        latest = max(series, key=lambda ev: ev.valid_from or epoch)
        panel[key] = round(latest.value or 0.0, 2)
        evidence_ids.append(latest.evidence_id)
        sources.add(source)

    if not panel:
        return None
    panel["source"] = "+".join(sorted(sources))
    panel["evidence_ids"] = evidence_ids
    return panel


def _fallback_summary(weather: WIOWeather) -> str:
    """weather.summary is written by _rain_panel alone, so a rain-less question left it empty and every consumer read that as "no evidence" despite a populated temperature/wind/marine panel; fixed once here for every consumer."""
    parts: list[str] = []
    if weather.temperature:
        t = weather.temperature
        parts.append(f"Temperature ranging {t['min']}-{t['max']}{t['unit']}")
    if weather.wind:
        parts.append(f"wind up to {weather.wind['value_kmh']} km/h")
    if weather.marine and "wave_height_m" in weather.marine:
        parts.append(f"wave height up to {weather.marine['wave_height_m']} m")
    return "; ".join(parts) + "." if parts else ""


def place_names(resolved_location: dict) -> tuple[list[str], list[str]]:
    """(district/city names, state names) a warning's area text might use; split because a bare state name is weaker evidence — see area_names_query."""
    state = resolved_location.get("state")
    local = [resolved_location.get("district"), resolved_location.get("normalized_name"),
             resolved_location.get("raw")]
    local.extend(name for name in (resolved_location.get("administrative_hierarchy") or [])
                 if name != state)
    return ([name for name in local if isinstance(name, str) and name.strip()],
            [state] if isinstance(state, str) and state.strip() else [])


def covers(ev, q_lat: float, q_lon: float, names: tuple[list[str], list[str]]) -> bool:
    """Whether a warning applies here: polygon first, else area-description fallback — the feed is national, so defaulting an untestable warning to "included" once showed every alert in India to every user and maxed RADE's risk aversion for all of them."""
    by_polygon = covers_query(ev, q_lat, q_lon)
    if by_polygon is not None:
        return by_polygon
    return area_names_query(ev, *names)


def filter_covered_warnings(ceos: list[CanonicalEvidenceObject],
                            resolved_location: dict) -> list[CanonicalEvidenceObject]:
    """Drop non-covering warning-class CEOs (everything else passes through); applied once in main.py upstream of both build_wio and run_all_agents, so one filter fixes both."""
    q_lat, q_lon = resolved_location["lat"], resolved_location["lon"]
    names = place_names(resolved_location)
    return [e for e in ceos if e.evidence_class != "warning" or covers(e, q_lat, q_lon, names)]


def _warning(ceos, q_lat: float, q_lon: float,
             names: tuple[list[str], list[str]]) -> WIOWarning:
    warnings = [e for e in ceos
                if e.evidence_class == "warning" and covers(e, q_lat, q_lon, names)]
    if not warnings:
        return WIOWarning(active=False)
    worst = max(warnings, key=lambda e: _SEVERITY_ORDER.get((e.warning_severity or "yellow").lower(), 1))
    return WIOWarning(
        active=True, authority=worst.source, severity=worst.warning_severity or "yellow",
        event=worst.raw_value or worst.variable, valid_from=worst.valid_from, valid_until=worst.valid_to,
        areas=[worst.geometry.reference] if worst.geometry and worst.geometry.reference else [],
        provenance={"source_record_id": worst.source_record_id,
                    "transformations": worst.provenance.transformations},
    )


def _evidence_summaries(scored) -> list[EvidenceSummary]:
    """Ensemble members are distribution inputs, not individually meaningful claims, so they collapse to one summary row and stay retrievable by ID."""
    summaries = [
        EvidenceSummary(evidence_id=e.evidence_id, source=e.source, evidence_class=e.evidence_class,
                        variable=e.variable, value=e.value, unit=e.unit,
                        valid_from=e.valid_from, valid_to=e.valid_to,
                        provenance={"transformations": e.provenance.transformations,
                                    "original_source": e.provenance.original_source,
                                    "original_unit": e.provenance.original_unit})
        for _, e in scored if e.ensemble_member is None
    ]
    members = [e for _, e in scored if e.ensemble_member is not None]
    if members:
        wet = sum(1 for e in members
                  if e.variable == "precipitation_amount" and (e.value or 0.0) >= settings.measurable_rain_mm)
        precipitation_members = [e for e in members if e.variable == "precipitation_amount"]
        summaries.append(EvidenceSummary(
            evidence_id=members[0].evidence_id, source=members[0].source, evidence_class="forecast",
            variable="rainfall_distribution", value=len(precipitation_members), unit="members",
            valid_from=members[0].valid_from, valid_to=members[-1].valid_to,
            provenance={"original_source": members[0].provenance.original_source,
                        "transformations": [
                            f"{len(precipitation_members)} ensemble member records summarised",
                            f"{wet} at or above {settings.measurable_rain_mm} mm",
                            "individual members retrievable via GET /evidence/{id}"]}))
    return summaries


def build_wio(query_text: str, resolved_location: dict, valid_from, valid_to, horizon: str,
              ceos: list[CanonicalEvidenceObject], lang: str = "en") -> WeatherIntelligenceObject:
    q_lat = resolved_location["lat"]
    q_lon = resolved_location["lon"]
    scored = rank(ceos, q_lat, q_lon, datetime.now(timezone.utc), window=(valid_from, valid_to))

    weather = WIOWeather()
    weather.rain, weather.summary = _rain_panel(scored)
    weather.temperature = _temperature_panel(scored)
    weather.wind = _wind_panel(scored)
    weather.humidity = _range_panel(scored, "humidity", "%")
    weather.pressure = _range_panel(scored, "pressure_msl", "hPa")
    weather.cloud_cover = _range_panel(scored, "cloud_cover", "%")
    weather.visibility = _visibility_panel(scored)
    weather.heat_stress = _heat_stress_panel(weather.temperature, weather.humidity, weather.wind)
    weather.marine = _marine_panel(scored)
    if not weather.summary:
        weather.summary = _fallback_summary(weather)

    disagreements = detect_disagreements(scored)
    if not ceos:
        agreement = WIOAgreement(status="insufficient_evidence", notes="No evidence matched the query window.")
    elif disagreements:
        agreement = WIOAgreement(status="partial_agreement", notes="; ".join(disagreements))
    elif corroborated(ceos):
        agreement = WIOAgreement(status="full_agreement",
                                 notes="Two or more sources agree on the same variable and time within tolerance.")
    else:
        agreement = WIOAgreement(status="single_source",
                                 notes="Only one source reported this variable for this window.")

    query = WIOQuery(raw_text=query_text, resolved_location=resolved_location,
                     valid_from=valid_from, valid_to=valid_to, intent=horizon, lang=lang)
    return WeatherIntelligenceObject(
        query=query, weather=weather,
        official_warning=_warning(ceos, q_lat, q_lon, place_names(resolved_location)),
        agreement=agreement, evidence=_evidence_summaries(scored), disagreements=disagreements)
