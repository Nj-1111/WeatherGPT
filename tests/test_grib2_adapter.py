import asyncio
from datetime import datetime, timezone

from app.adapters.grib2_adapter import (
    Grib2Adapter,
    _build_request,
    _candidate_cycles,
    _lead_hours,
    _snap_lead_hours,
)


def test_candidate_cycles_picks_published_cycle_and_falls_back():
    now = datetime(2026, 9, 2, 12, 4, tzinfo=timezone.utc)
    cycles = _candidate_cycles(now, max_fallbacks=2)
    assert cycles == [
        datetime(2026, 9, 2, 6, tzinfo=timezone.utc),
        datetime(2026, 9, 2, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 18, tzinfo=timezone.utc),
    ]


def test_lead_hours_clamped_to_valid_range():
    run_dt = datetime(2026, 9, 2, 6, tzinfo=timezone.utc)
    assert _lead_hours(run_dt, datetime(2026, 9, 2, 18, tzinfo=timezone.utc)) == 12
    assert _lead_hours(run_dt, datetime(2026, 9, 1, 0, tzinfo=timezone.utc)) == 0
    assert _lead_hours(run_dt, datetime(2026, 9, 20, 0, tzinfo=timezone.utc)) == 384


def test_snap_lead_hours_hourly_then_3hourly():
    assert _snap_lead_hours(12) == 12
    assert _snap_lead_hours(120) == 120
    assert _snap_lead_hours(121) == 120
    assert _snap_lead_hours(122) == 123
    assert _snap_lead_hours(400) == 384


def test_build_request_shape():
    run_dt = datetime(2026, 9, 2, 6, tzinfo=timezone.utc)
    url, params = _build_request(run_dt, 12, 21.14, 79.08)
    assert url == "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
    assert params["file"] == "gfs.t06z.pgrb2.0p25.f012"
    assert params["dir"] == "/gfs.20260902/06/atmos"
    assert params["var_TMP"] == "on" and params["lev_2_m_above_ground"] == "on"
    assert params["var_APCP"] == "on" and params["lev_surface"] == "on"
    assert params["leftlon"] < 79.08 < params["rightlon"]
    assert params["bottomlat"] < 21.14 < params["toplat"]


def test_fetch_reports_missing_dependency_without_network_call():
    """When eccodes/cfgrib/xarray aren't installed, fetch() must fail fast — before
    any HTTP call — rather than hitting NOMADS for a file it couldn't decode anyway."""
    adapter = Grib2Adapter()
    try:
        import cfgrib  # noqa: F401
    except ImportError:
        pass
    else:
        import pytest
        pytest.skip("cfgrib is installed in this environment; this test targets the not-installed path")
    try:
        asyncio.run(adapter.fetch(lat=21.14, lon=79.08))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "eccodes" in str(exc) or "cfgrib" in str(exc)


def test_health_check_reports_missing_dependency():
    adapter = Grib2Adapter()
    try:
        import cfgrib  # noqa: F401
    except ImportError:
        pass
    else:
        import pytest
        pytest.skip("cfgrib is installed in this environment; this test targets the not-installed path")
    result = asyncio.run(adapter.health_check())
    assert result["available"] is False
    assert "eccodes" in result["reason"] or "cfgrib" in result["reason"]
