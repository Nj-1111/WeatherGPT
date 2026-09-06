"""Shared constants used across app/ modules."""
from __future__ import annotations

from datetime import timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Fixture only to prove an endpoint answers at all — not data; a region-restricted source can still report healthy here, see /health caveats.
HEALTH_PROBE_LAT = 21.14
HEALTH_PROBE_LON = 79.08
HEALTH_PROBE_DATE = "20240101"

# Nagpur (above) is inland, no use for a marine API; this is Bay of Bengal just east of Chennai, over water.
MARINE_HEALTH_PROBE_LAT = 13.0
MARINE_HEALTH_PROBE_LON = 80.3
