"""Unit conversions shared across fusion and verification — one definition on purpose, since the reviewer recomputes panel values from cited evidence and a mismatch would reject correct claims."""
from __future__ import annotations

from app.schemas.ceo import CanonicalEvidenceObject

MS_TO_KMH = 3.6


def as_kmh(ev: CanonicalEvidenceObject) -> float:
    """Wind panels report km/h whatever the source sent; MET Norway sends m/s."""
    value = ev.value or 0.0
    return value * MS_TO_KMH if ev.unit == "m/s" else value
