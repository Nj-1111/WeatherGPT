import math

import pytest

from app.decoders.grib2_placeholder import decode_grib2_file


def test_decode_reports_missing_dependency_without_a_real_file():
    try:
        import cfgrib  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("cfgrib is installed in this environment; this test targets the not-installed path")
    with pytest.raises(RuntimeError, match="eccodes|cfgrib"):
        decode_grib2_file("/nonexistent.grib2", 21.14, 79.08)


@pytest.mark.parametrize("u,v,expected_direction", [
    (0.0, -5.0, 0.0),      # blowing from the north (wind vector points south)
    (-5.0, 0.0, 90.0),     # blowing from the east
    (0.0, 5.0, 180.0),     # blowing from the south
    (5.0, 0.0, 270.0),     # blowing from the west
])
def test_wind_direction_meteorological_convention(u, v, expected_direction):
    """Direction the wind blows FROM, not the vector's mathematical angle — same formula
    used inline in grib2_placeholder.py's wind decode block."""
    direction = math.degrees(math.atan2(-u, -v)) % 360
    assert direction == pytest.approx(expected_direction, abs=1e-6)
