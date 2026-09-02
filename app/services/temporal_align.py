from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.schemas.ceo import CanonicalEvidenceObject


def to_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def overlaps(ev: CanonicalEvidenceObject, q_from: datetime, q_to: datetime) -> bool:
    if ev.valid_from is None or ev.valid_to is None:
        # fallback to issued/observed
        t = ev.issued_at or ev.observed_at
        if t is None:
            return True  # keep if no time at all
        t = to_utc(t)
        return q_from <= t <= q_to
    return not (to_utc(ev.valid_to) < q_from or to_utc(ev.valid_from) > q_to)

def filter_by_window(evs: list[CanonicalEvidenceObject], q_from: datetime, q_to: datetime) -> list[CanonicalEvidenceObject]:
    return [e for e in evs if overlaps(e, q_from, q_to)]

def staleness_hours(ev: CanonicalEvidenceObject, now: datetime) -> float:
    t = ev.issued_at or ev.model_initialization_time or ev.observed_at or ev.ingested_at
    if t is None:
        return settings.rank_staleness_ceiling_hours
    return (to_utc(now) - to_utc(t)).total_seconds() / 3600
