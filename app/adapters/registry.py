"""Adapter registry — centralizes health and source selection."""
from __future__ import annotations

import asyncio

from app.adapters.cap_adapter import CapAdapter
from app.adapters.grib2_adapter import Grib2Adapter
from app.adapters.imd_adapter import ImdAdapter
from app.adapters.met_norway import MetNorwayAdapter
from app.adapters.nasa_power import NasaPowerAdapter
from app.adapters.open_meteo_ensemble import OpenMeteoEnsembleAdapter
from app.adapters.open_meteo_forecast import OpenMeteoForecastAdapter
from app.adapters.open_meteo_historical import OpenMeteoHistoricalAdapter
from app.config import settings
from app.services.cache import TTLCache

_health_cache = TTLCache(max_entries=1)

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
    """Probe every source concurrently, cached for health_cache_ttl_seconds.

    Sequentially this cost the sum of eight upstream round trips (~11.5s measured), on an
    endpoint deliberately exempt from auth and rate limiting (infra must be able to probe
    it) — so any caller could repeat this fan-out for free, at this server's expense and
    the upstream free APIs'. The cache is what actually bounds that, not the exemption
    list. `return_exceptions` keeps each adapter isolated exactly as the loop did.
    """
    cached = await _health_cache.get("health")
    if cached is not None:
        return cached.value
    names = list(REGISTRY)
    outcomes = await asyncio.gather(*(adapter.health_check() for adapter in REGISTRY.values()),
                                    return_exceptions=True)
    result = {name: ({"available": False, "reason": str(outcome)}
                     if isinstance(outcome, BaseException) else outcome)
              for name, outcome in zip(names, outcomes, strict=True)}
    await _health_cache.put("health", result, settings.health_cache_ttl_seconds)
    return result
