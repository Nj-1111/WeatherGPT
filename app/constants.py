"""Shared constants used across app/ modules."""
from __future__ import annotations

from datetime import timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Fixture used only to prove an endpoint answers at all. It is not data, and a source
# that is region-restricted can still report healthy here — see /health caveats.
HEALTH_PROBE_LAT = 21.14
HEALTH_PROBE_LON = 79.08
HEALTH_PROBE_DATE = "20240101"
