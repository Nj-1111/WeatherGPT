"""GFS GRIB2 Adapter — fetches a filtered 0.25deg GRIB2 subset from NOAA NOMADS.

Decoding needs eccodes+cfgrib+xarray (requirements-full.txt); if they're not
installed, this adapter reports itself unavailable rather than failing the
whole request (matches every other adapter's isolation contract).

NOAA publishes each GFS cycle (00/06/12/18Z) with a real-world latency —
data for a given cycle typically isn't on NOMADS until ~4-5h after the
cycle's nominal time. `_candidate_cycles` picks the newest cycle that's
plausibly published, then falls back to progressively older cycles on
fetch failure (a not-yet-published cycle 404s rather than hanging).
"""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.adapters.base import WeatherSourceAdapter

NOMADS_BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
NOMADS_HEALTH_URL = "https://nomads.ncep.noaa.gov/"
USER_AGENT = "WeatherGPT/1.0 (weather-intelligence backend; contact via repo issues)"

CYCLE_HOURS = (0, 6, 12, 18)
PUBLISH_LATENCY_HOURS = 5.0
MAX_CYCLE_FALLBACKS = 2
SUBREGION_BOX_DEG = 0.3
FETCH_TIMEOUT_SECONDS = 8.0


def _candidate_cycles(now: datetime, max_fallbacks: int = MAX_CYCLE_FALLBACKS) -> list[datetime]:
    """Newest-first list of GFS cycle run-times plausibly published by `now`."""
    candidate = now - timedelta(hours=PUBLISH_LATENCY_HOURS)
    cycle_hour = max(h for h in CYCLE_HOURS if h <= candidate.hour)
    run_dt = candidate.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    out = []
    for _ in range(max_fallbacks + 1):
        out.append(run_dt)
        run_dt = run_dt - timedelta(hours=6)
    return out


def _lead_hours(run_dt: datetime, valid_from: datetime) -> int:
    hours = round((valid_from - run_dt).total_seconds() / 3600)
    return max(0, min(384, hours))


def _snap_lead_hours(lead_hours: int) -> int:
    """GFS 0.25deg is hourly through f120, then 3-hourly through f384."""
    if lead_hours <= 120:
        return lead_hours
    return min(384, 120 + round((lead_hours - 120) / 3) * 3)


def _build_request(run_dt: datetime, lead_hours: int, lat: float, lon: float) -> tuple[str, dict]:
    file_name = f"gfs.t{run_dt.hour:02d}z.pgrb2.0p25.f{lead_hours:03d}"
    dir_path = f"/gfs.{run_dt.strftime('%Y%m%d')}/{run_dt.hour:02d}/atmos"
    params = {
        "file": file_name,
        "dir": dir_path,
        "var_TMP": "on",
        "lev_2_m_above_ground": "on",
        "var_APCP": "on",
        "lev_surface": "on",
        "subregion": "",
        "leftlon": round(lon - SUBREGION_BOX_DEG, 2),
        "rightlon": round(lon + SUBREGION_BOX_DEG, 2),
        "toplat": round(lat + SUBREGION_BOX_DEG, 2),
        "bottomlat": round(lat - SUBREGION_BOX_DEG, 2),
    }
    return NOMADS_BASE, params


class Grib2Adapter(WeatherSourceAdapter):
    source_name = "GFS"
    supported_evidence_classes = ["forecast"]
    supported_variables = ["temperature_2m", "precipitation_amount"]

    async def fetch(self, lat: float, lon: float, **kwargs) -> Any:
        try:
            import cfgrib  # noqa: F401
        except Exception as exc:
            raise RuntimeError(f"GRIB2 decoding requires eccodes+cfgrib+xarray (install requirements-full.txt): {exc}") from exc

        valid_from_raw = kwargs.get("valid_from")
        valid_from = datetime.fromisoformat(valid_from_raw) if valid_from_raw else datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)

        last_error: Exception | None = None
        for run_dt in _candidate_cycles(now):
            lead_hours = _snap_lead_hours(_lead_hours(run_dt, valid_from))
            url, params = _build_request(run_dt, lead_hours, lat, lon)
            try:
                async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}) as client:
                    r = await client.get(url, params=params)
                    r.raise_for_status()
                    if len(r.content) < 200:
                        raise RuntimeError(f"NOMADS returned an empty subset for cycle {run_dt.isoformat()} f{lead_hours:03d} — likely not yet published")
                fd, path = tempfile.mkstemp(suffix=".grib2")
                with os.fdopen(fd, "wb") as f:
                    f.write(r.content)
                return path
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(f"GFS GRIB2 fetch failed for all candidate cycles: {last_error}")

    def normalize(self, raw: Any, lat: float, lon: float, **kwargs) -> list:
        from app.decoders.grib2_placeholder import decode_grib2_file
        path = raw
        try:
            return decode_grib2_file(path, lat, lon)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    async def health_check(self) -> dict[str, Any]:
        start = time.time()
        try:
            import cfgrib  # noqa: F401
        except Exception as exc:
            return {"available": False, "latency_ms": 0, "reason": f"missing eccodes/cfgrib/xarray (install requirements-full.txt): {exc}"}
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as client:
                r = await client.head(NOMADS_HEALTH_URL)
                r.raise_for_status()
            return {"available": True, "latency_ms": int((time.time() - start) * 1000), "reason": "ok"}
        except Exception as exc:
            return {"available": False, "latency_ms": int((time.time() - start) * 1000), "reason": f"NOMADS unreachable: {exc}"}
