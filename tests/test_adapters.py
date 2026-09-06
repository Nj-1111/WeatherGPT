
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


def test_cap_alert_links_accepts_query_string_style_urls(monkeypatch):
    """NDMA's feed links to FetchXMLFile?identifier=... (real, valid CAP XML — verified
    live), not *.xml like IMD's. A suffix-based filter silently dropped every one."""
    import dataclasses

    from app.adapters.cap_adapter import CapAdapter
    from app.config import settings
    monkeypatch.setattr("app.adapters.cap_adapter.settings",
                        dataclasses.replace(settings, cap_feed_url="https://sachet.ndma.gov.in/feed.xml"))
    index = b"""<rss><channel>
        <item><link>https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=123</link></item>
        <item><link>not-a-url</link></item>
    </channel></rss>"""
    links = CapAdapter._alert_links(index)
    assert links == ["https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=123"]


def test_2_10_cap_alert_links_rejects_links_off_the_feeds_own_host(monkeypatch):
    """§2.10: a compromised/malicious feed must not be able to redirect this adapter into
    fetching an arbitrary host (e.g. internal network / cloud metadata addresses)."""
    import dataclasses

    from app.adapters.cap_adapter import CapAdapter
    from app.config import settings
    monkeypatch.setattr("app.adapters.cap_adapter.settings",
                        dataclasses.replace(settings, cap_feed_url="https://sachet.ndma.gov.in/feed.xml"))
    index = b"""<rss><channel>
        <item><link>https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=123</link></item>
        <item><link>http://169.254.169.254/latest/meta-data/</link></item>
        <item><link>https://attacker.example.com/alert.xml</link></item>
    </channel></rss>"""
    links = CapAdapter._alert_links(index)
    assert links == ["https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=123"]


def test_cap_alert_links_empty_for_malformed_index():
    from app.adapters.cap_adapter import CapAdapter
    assert CapAdapter._alert_links(b"not xml at all") == []


def test_2_10_cap_decoder_rejects_xml_with_a_dtd():
    """§2.10: entity-expansion (billion-laughs) payloads are rejected outright rather
    than parsed — defusedxml refuses any DOCTYPE, not just ones that expand."""
    import pytest
    from defusedxml.common import DefusedXmlException

    from app.decoders.cap_decoder import decode_cap_xml
    payload = b"""<?xml version="1.0"?>
        <!DOCTYPE alert [<!ENTITY lol "lol">]>
        <alert><info><event>&lol;</event></info></alert>"""
    with pytest.raises(DefusedXmlException):
        decode_cap_xml(payload)


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
