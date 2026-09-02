"""Adapter registry — centralizes health and source selection."""
from __future__ import annotations

from app.adapters.cap_adapter import CapAdapter
from app.adapters.grib2_adapter import Grib2Adapter
from app.adapters.imd_adapter import ImdAdapter
from app.adapters.met_norway import MetNorwayAdapter
from app.adapters.nasa_power import NasaPowerAdapter
from app.adapters.open_meteo_ensemble import OpenMeteoEnsembleAdapter
from app.adapters.open_meteo_forecast import OpenMeteoForecastAdapter
from app.adapters.open_meteo_historical import OpenMeteoHistoricalAdapter

REGISTRY = {
    "OPEN_METEO": OpenMeteoForecastAdapter(),
    "ERA5": OpenMeteoHistoricalAdapter(),
    "GEFS": OpenMeteoEnsembleAdapter(),
    "CAP": CapAdapter(),
    "NASA_POWER": NasaPowerAdapter(),
    "IMD": ImdAdapter(),
    "GFS": Grib2Adapter(),
    "MET_NORWAY": MetNorwayAdapter(),
}

async def health_all() -> dict[str, dict]:
    out = {}
    for name, adapter in REGISTRY.items():
        try:
            out[name] = await adapter.health_check()
        except Exception as e:
            out[name] = {"available": False, "reason": str(e)}
    return out
