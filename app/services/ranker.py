from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from app.config import settings
from app.schemas.ceo import CanonicalEvidenceObject
from app.services.spatial_match import UNKNOWN_DISTANCE_KM, distance_to_query
from app.services.temporal_align import staleness_hours, to_utc

AUTHORITY = {
    "CAP": 1.0,
    "IMD": 0.95,
    "RADAR": 0.85,
    "INSAT": 0.8,
    "MET_NORWAY": 0.75,
    "WRF": 0.75,
    "GEFS": 0.70,
    "GFS": 0.7,
    "OPEN_METEO": 0.7,
    "NASA_POWER": 0.65,
    "ERA5": 0.5,
    "OTHER": 0.5,
}


def _temporal_score(ev: CanonicalEvidenceObject, window: tuple[datetime, datetime] | None) -> float:
    """Proximity of the evidence's valid time to the middle of the requested window.

    Every hourly record from one source is otherwise identical on all other terms, which
    left ordering to be decided by whichever was decoded first.
    """
    if window is None or ev.valid_from is None:
        return 1.0
    start, end = to_utc(window[0]), to_utc(window[1])
    span = (end - start).total_seconds()
    if span <= 0:
        return 1.0
    centre = start + (end - start) / 2
    offset = abs((to_utc(ev.valid_from) - centre).total_seconds())
    return max(0.0, 1.0 - offset / span)


def score_evidence(ev: CanonicalEvidenceObject, q_lat: float, q_lon: float, now: datetime,
                   window: tuple[datetime, datetime] | None = None) -> float:
    authority = AUTHORITY.get(ev.source, 0.5)
    if ev.evidence_class == "warning":
        authority = min(1.0, authority + settings.rank_warning_authority_bonus)

    staleness = min(staleness_hours(ev, now), settings.rank_staleness_ceiling_hours)
    freshness = 1 - staleness / settings.rank_staleness_ceiling_hours

    distance = distance_to_query(ev, q_lat, q_lon)
    if distance == UNKNOWN_DISTANCE_KM:
        spatial = settings.rank_unknown_location_penalty
    else:
        spatial = 1 / (1 + distance / settings.rank_spatial_half_decay_km)

    quality = settings.rank_bad_quality_factor if (ev.quality_flag and "bad" in ev.quality_flag.lower()) else 1.0

    return (settings.rank_weight_authority * authority
            + settings.rank_weight_freshness * freshness
            + settings.rank_weight_spatial * spatial
            + settings.rank_weight_quality * quality
            + settings.rank_weight_temporal * _temporal_score(ev, window))


def rank(evs: list[CanonicalEvidenceObject], q_lat: float, q_lon: float,
         now: datetime | None = None, window: tuple[datetime, datetime] | None = None):
    now = now or datetime.now(timezone.utc)
    scored = [(score_evidence(e, q_lat, q_lon, now, window), e) for e in evs]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def group_comparable(evs: list[CanonicalEvidenceObject]) -> dict[tuple, list[CanonicalEvidenceObject]]:
    """Bucket evidence that is genuinely like-for-like.

    The accumulation window is part of the key: a 24-hour rainfall total and a 1-hour one
    differ by arithmetic, not by disagreement, and comparing them is the exact confusion
    the CEO schema exists to prevent. Ensemble members are excluded for the same reason:
    they are one vendor's uncertainty distribution, not independent reports, and letting
    them into a bucket made that spread read as sources disagreeing.
    """
    buckets: dict[tuple, list[CanonicalEvidenceObject]] = defaultdict(list)
    for ev in evs:
        if ev.value is None or ev.valid_from is None or ev.ensemble_member is not None:
            continue
        buckets[(ev.variable.value, ev.accumulation_window_hours, to_utc(ev.valid_from))].append(ev)
    return buckets


def corroborated(evs: list[CanonicalEvidenceObject]) -> bool:
    """True when at least two distinct sources report the same thing for the same moment.

    Counting evidence objects instead of sources let one vendor's temperature and rainfall
    read as two sources agreeing.
    """
    return any(len({ev.source for ev in bucket}) >= 2 for bucket in group_comparable(evs).values())


_THRESHOLDS = {"precipitation_amount": settings.disagreement_threshold_mm,
               "temperature_2m": settings.disagreement_threshold_c}


def detect_disagreements(scored) -> list[str]:
    evs = [ev for _, ev in scored]
    notes = []
    for (variable, _window, valid_from), bucket in sorted(group_comparable(evs).items(), key=lambda item: str(item[0])):
        threshold = _THRESHOLDS.get(variable)
        if threshold is None or len({ev.source for ev in bucket}) < 2:
            continue
        values = [ev.value for ev in bucket if ev.value is not None]
        spread = max(values) - min(values)
        if spread > threshold:
            sources = ",".join(sorted({ev.source.value for ev in bucket}))
            notes.append(f"{variable} spread {min(values):.1f}-{max(values):.1f} at "
                         f"{valid_from:%Y-%m-%d %H:%M}Z across {sources} (threshold {threshold})")
    return notes
