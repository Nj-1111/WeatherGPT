
from app.adapters.registry import REGISTRY


def test_registry_has_all():
    assert "OPEN_METEO" in REGISTRY
    assert "IMD" in REGISTRY
    assert "GFS" in REGISTRY
    assert "OPEN_METEO_MARINE" in REGISTRY


def test_open_meteo_marine_normalize_maps_every_field():
    from app.adapters.open_meteo_marine import OpenMeteoMarineAdapter
    payload = {"hourly": {
        "time": ["2026-09-04T00:00", "2026-09-04T01:00"],
        "wave_height": [0.8, 0.78],
        "wave_direction": [149, 148],
        "wave_period": [8.35, 8.60],
        "ocean_current_velocity": [0.6, 0.6],
        "ocean_current_direction": [18, 18],
        "sea_surface_temperature": [30.0, 30.0],
    }}
    ceos = OpenMeteoMarineAdapter().normalize(payload, lat=13.0, lon=80.3)
    assert len(ceos) == 12  # 6 fields x 2 hours
    variables = {ceo.variable.value for ceo in ceos}
    assert variables == {"wave_height", "wave_direction", "wave_period",
                         "ocean_current_velocity", "ocean_current_direction", "sea_surface_temperature"}
    velocity = next(c for c in ceos if c.variable == "ocean_current_velocity")
    assert velocity.unit == "km/h" and velocity.source == "OPEN_METEO_MARINE"


def test_open_meteo_marine_normalize_skips_null_readings():
    from app.adapters.open_meteo_marine import OpenMeteoMarineAdapter
    payload = {"hourly": {"time": ["2026-09-04T00:00"], "wave_height": [None],
                          "wave_direction": [149], "wave_period": [], "ocean_current_velocity": [0.6],
                          "ocean_current_direction": [18], "sea_surface_temperature": [30.0]}}
    ceos = OpenMeteoMarineAdapter().normalize(payload, lat=13.0, lon=80.3)
    variables = {ceo.variable.value for ceo in ceos}
    assert "wave_height" not in variables  # null value skipped
    assert "wave_period" not in variables  # missing index skipped
    assert "wave_direction" in variables


def test_open_meteo_marine_normalize_handles_malformed_payload():
    from app.adapters.open_meteo_marine import OpenMeteoMarineAdapter
    assert OpenMeteoMarineAdapter().normalize({}, lat=13.0, lon=80.3) == []
    assert OpenMeteoMarineAdapter().normalize({"hourly": {"time": ["not-a-timestamp"]}}, lat=13.0, lon=80.3) == []

def test_grib2_unavailable():
    import asyncio
    adapter = REGISTRY["GFS"]
    result = asyncio.run(adapter.health_check())
    assert not result["available"]
    assert "eccodes" in result["reason"] or "isolated" in result["reason"]

def test_imd_without_key():
    import asyncio
    import os
    os.environ.pop("IMD_API_KEY", None)
    adapter = REGISTRY["IMD"]
    result = asyncio.run(adapter.health_check())
    assert not result["available"]
    assert "IMD_API_KEY" in result["reason"]


def test_stormglass_without_key(monkeypatch):
    import asyncio
    import dataclasses

    from app.config import settings
    monkeypatch.setattr("app.adapters.stormglass_adapter.settings",
                        dataclasses.replace(settings, stormglass_api_key=""))
    result = asyncio.run(REGISTRY["STORMGLASS"].health_check())
    assert not result["available"]
    assert "STORMGLASS_API_KEY" in result["reason"]


def test_stormglass_normalize_prefers_sg_model():
    from app.adapters.stormglass_adapter import StormglassAdapter
    payload = {"hours": [{"time": "2026-09-04T00:00:00+00:00",
                          "waveHeight": {"sg": 0.8, "noaa": 0.75},
                          "currentSpeed": {"noaa": 0.3}}]}
    ceos = StormglassAdapter().normalize(payload, lat=13.0, lon=80.3)
    wave = next(c for c in ceos if c.variable == "wave_height")
    assert wave.value == 0.8  # "sg" preferred over "noaa"
    current = next(c for c in ceos if c.variable == "ocean_current_velocity")
    assert current.value == 0.3  # falls back to the one model present when "sg" absent


def test_stormglass_normalize_handles_malformed_payload():
    from app.adapters.stormglass_adapter import StormglassAdapter
    assert StormglassAdapter().normalize({}, lat=13.0, lon=80.3) == []
    assert StormglassAdapter().normalize({"hours": "not-a-list"}, lat=13.0, lon=80.3) == []
    assert StormglassAdapter().normalize(
        {"hours": [{"time": "garbage", "waveHeight": {"sg": 1.0}}]}, lat=13.0, lon=80.3) == []
