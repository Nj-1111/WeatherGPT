"""Unit conversions shared across fusion and verification.

One definition on purpose: the reviewer recomputes panel values from the evidence they
cite, so if fusion and verification ever converted differently the gate would start
rejecting correct claims.
"""
from __future__ import annotations

from app.schemas.ceo import CanonicalEvidenceObject

MS_TO_KMH = 3.6


def as_kmh(ev: CanonicalEvidenceObject) -> float:
    """Wind panels report km/h whatever the source sent; MET Norway sends m/s."""
    value = ev.value or 0.0
    return value * MS_TO_KMH if ev.unit == "m/s" else value
