"""GFS GRIB2 decoder — selection by param/level/lead + nearest-grid-cell spatial sampling, called by grib2_adapter.py with a downloaded file path; needs eccodes/cfgrib/xarray (requirements-full.txt) or the adapter reports itself unavailable.

Standalone: python -m app.decoders.grib2_placeholder --grib <file> --lat <lat> --lon <lon>
Install: pip install -r requirements-full.txt  (or: conda install -c conda-forge eccodes cfgrib)
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

logger = logging.getLogger(__name__)


def decode_grib2_file(path: str, lat: float, lon: float) -> list[CanonicalEvidenceObject]:
    try:
        import cfgrib  # noqa: F401
        import xarray as xr
    except Exception as e:
        raise RuntimeError(f"GRIB2 decoder needs cfgrib+eccodes. Install requirements-full.txt. Detail: {e}") from e

    out: list[CanonicalEvidenceObject] = []
    try:
        ds = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"typeOfLevel": "heightAboveGround", "shortName": "2t"})
        point = ds.sel(latitude=lat, longitude=lon if lon<=180 else lon-360, method="nearest")
        val = float(point["t2m"].values) - 273.15  # K → C
        out.append(CanonicalEvidenceObject(
            source="GFS", evidence_class="forecast", variable="temperature_2m", value=val, unit="C", statistic="instant",
            geometry=Geometry(type="GridCell", coordinates=[lon, lat]),
            model_name="GFS", spatial_resolution="0.25°",
            provenance=Provenance(original_source="GFS GRIB2", original_unit="K", transformations=["decoded GRIB2","selected 2t surface","spatially sampled","K→C"], raw_record_id=Path(path).name)
        ))
    except Exception as exc:
        logger.warning("grib2.t2m_decode_skipped", extra={"error": str(exc)})

    try:
        ds2 = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"typeOfLevel": "surface", "shortName": "tp"})
        point = ds2.sel(latitude=lat, longitude=lon if lon<=180 else lon-360, method="nearest")
        val = float(point["tp"].values)  # kg m-2 ≈ mm
        out.append(CanonicalEvidenceObject(
            source="GFS", evidence_class="forecast", variable="precipitation_amount", value=val, unit="mm", statistic="accumulation",
            geometry=Geometry(type="GridCell", coordinates=[lon, lat]),
            accumulation_window_hours=6, model_name="GFS", spatial_resolution="0.25°",
            provenance=Provenance(original_source="GFS GRIB2", original_unit="kg m-2", transformations=["decoded GRIB2","selected tp","spatially sampled","kg m-2→mm"], raw_record_id=Path(path).name)
        ))
    except Exception as exc:
        logger.warning("grib2.tp_decode_skipped", extra={"error": str(exc)})

    try:
        ds3 = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"typeOfLevel": "heightAboveGround", "shortName": "10u"})
        ds4 = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"typeOfLevel": "heightAboveGround", "shortName": "10v"})
        point_u = ds3.sel(latitude=lat, longitude=lon if lon<=180 else lon-360, method="nearest")
        point_v = ds4.sel(latitude=lat, longitude=lon if lon<=180 else lon-360, method="nearest")
        u, v = float(point_u["u10"].values), float(point_v["v10"].values)
        speed = math.hypot(u, v)
        # Meteorological convention (direction the wind blows FROM, 0=N/90=E/180=S/270=W),
        # not the mathematical vector angle — matches how every other source in this repo
        # reports wind direction.
        direction = math.degrees(math.atan2(-u, -v)) % 360
        out.append(CanonicalEvidenceObject(
            source="GFS", evidence_class="forecast", variable="wind_speed", value=speed, unit="m/s", statistic="instant",
            geometry=Geometry(type="GridCell", coordinates=[lon, lat]),
            model_name="GFS", spatial_resolution="0.25°",
            provenance=Provenance(original_source="GFS GRIB2", original_unit="m/s", transformations=["decoded GRIB2","selected 10u/10v","spatially sampled","combined to speed"], raw_record_id=Path(path).name)
        ))
        out.append(CanonicalEvidenceObject(
            source="GFS", evidence_class="forecast", variable="wind_direction", value=direction, unit="degrees", statistic="instant",
            geometry=Geometry(type="GridCell", coordinates=[lon, lat]),
            model_name="GFS", spatial_resolution="0.25°",
            provenance=Provenance(original_source="GFS GRIB2", original_unit="m/s", transformations=["decoded GRIB2","selected 10u/10v","spatially sampled","combined to direction"], raw_record_id=Path(path).name)
        ))
    except Exception as exc:
        logger.warning("grib2.wind_decode_skipped", extra={"error": str(exc)})

    try:
        ds5 = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"typeOfLevel": "heightAboveGround", "shortName": "2r"})
        point = ds5.sel(latitude=lat, longitude=lon if lon<=180 else lon-360, method="nearest")
        val = float(point["r2"].values)  # already %
        out.append(CanonicalEvidenceObject(
            source="GFS", evidence_class="forecast", variable="humidity", value=val, unit="%", statistic="instant",
            geometry=Geometry(type="GridCell", coordinates=[lon, lat]),
            model_name="GFS", spatial_resolution="0.25°",
            provenance=Provenance(original_source="GFS GRIB2", original_unit="%", transformations=["decoded GRIB2","selected 2r surface","spatially sampled"], raw_record_id=Path(path).name)
        ))
    except Exception as exc:
        logger.warning("grib2.rh_decode_skipped", extra={"error": str(exc)})

    try:
        ds6 = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"typeOfLevel": "meanSea", "shortName": "prmsl"})
        point = ds6.sel(latitude=lat, longitude=lon if lon<=180 else lon-360, method="nearest")
        val = float(point["prmsl"].values) / 100.0  # Pa → hPa
        out.append(CanonicalEvidenceObject(
            source="GFS", evidence_class="forecast", variable="pressure_msl", value=val, unit="hPa", statistic="instant",
            geometry=Geometry(type="GridCell", coordinates=[lon, lat]),
            model_name="GFS", spatial_resolution="0.25°",
            provenance=Provenance(original_source="GFS GRIB2", original_unit="Pa", transformations=["decoded GRIB2","selected prmsl meanSea","spatially sampled","Pa→hPa"], raw_record_id=Path(path).name)
        ))
    except Exception as exc:
        logger.warning("grib2.prmsl_decode_skipped", extra={"error": str(exc)})

    return out

if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--grib", required=True)
    ap.add_argument("--lat", type=float, default=21.14)
    ap.add_argument("--lon", type=float, default=79.08)
    args = ap.parse_args()
    ceos = decode_grib2_file(args.grib, args.lat, args.lon)
    print(json.dumps([c.model_dump(mode="json") for c in ceos], indent=2))
