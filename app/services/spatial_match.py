"""Distance and containment between evidence geometry and the query point."""
from __future__ import annotations

import math

from app.orchestrator.retrieval_planner import has_word
from app.schemas.ceo import CanonicalEvidenceObject

EARTH_RADIUS_KM = 6371.0
# Returned when evidence carries no usable position. Not zero: zero means "exactly at the
# query point", which would rank unlocated evidence as perfectly local.
UNKNOWN_DISTANCE_KM = float("inf")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _points(coordinates) -> list[tuple[float, float]]:
    """Normalize a coordinate list into [(lon, lat), ...], tolerating a GeoJSON ring."""
    if not isinstance(coordinates, list) or not coordinates:
        return []
    first = coordinates[0]
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, tuple)):
        coordinates = first  # GeoJSON wraps rings in an extra list
    points = []
    for item in coordinates:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                points.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                continue
    return points


def point_in_polygon(lat: float, lon: float, coordinates) -> bool:
    """Ray casting over a [lon, lat] ring. Used to decide whether an official warning
    actually covers the user, rather than assuming every warning is local."""
    points = _points(coordinates)
    if len(points) < 3:
        return False
    inside = False
    for index in range(len(points)):
        lon1, lat1 = points[index]
        lon2, lat2 = points[(index + 1) % len(points)]
        if (lat1 > lat) != (lat2 > lat):
            crossing_lon = lon1 + (lat - lat1) / (lat2 - lat1) * (lon2 - lon1)
            if lon < crossing_lon:
                inside = not inside
    return inside


def covers_query(ev: CanonicalEvidenceObject, q_lat: float, q_lon: float) -> bool | None:
    """True/False when the geometry can be tested, None when it cannot."""
    geometry = ev.geometry
    if geometry is None or not geometry.coordinates:
        return None
    if geometry.type == "Polygon":
        return point_in_polygon(q_lat, q_lon, geometry.coordinates)
    return None


# Shorter than this and a place name matches too much by coincidence. Real Indian states
# and districts clear it ("Goa" is the shortest at 3).
_MIN_PLACE_NAME_CHARS = 3


def area_names_query(ev: CanonicalEvidenceObject, local_names: list[str],
                     state_names: list[str]) -> bool:
    """Whether a warning's free-text area description names the query location.

    The polygon test above is authoritative but usually unavailable: every alert in
    NDMA's national CAP feed (checked live, 31 of 31) ships with an area *description*
    and no polygon at all. That description does carry the district and state
    ("Brahmaputra, Dhubri, Dhubri, Assam"), so it can still answer "is this the user's
    warning" — imprecisely, but far better than assuming every national alert is local.

    A state name only implies coverage when the alert is actually state-wide. IMD
    publishes district-scoped alerts as "<district>, <district> districts of <state>"
    (verified live), where the state is naming where those districts are, not claiming
    the whole state — so a listed-districts alert must name the user's own district.

    Whole-word matching only: substring matching would match "Assam" inside a longer
    unrelated token. Some publishers emit unusable text ("tslg"), which matches nothing
    and is treated as not covering the user.
    """
    reference = (ev.geometry.reference if ev.geometry else None) or ""
    areas = ev.extra.get("areas") if isinstance(ev.extra, dict) else None
    haystack = " ".join([reference, *(areas if isinstance(areas, list) else [])]).casefold()
    if not haystack.strip():
        return False

    def mentions(names: list[str]) -> bool:
        return any(has_word(haystack, (name.casefold(),))
                   for name in names if name and len(name) >= _MIN_PLACE_NAME_CHARS)

    if mentions(local_names):
        return True
    if has_word(haystack, ("district", "districts")):
        return False
    return mentions(state_names)


def distance_to_query(ev: CanonicalEvidenceObject, q_lat: float, q_lon: float) -> float:
    geometry = ev.geometry
    if geometry is None or not geometry.coordinates:
        return UNKNOWN_DISTANCE_KM
    try:
        if geometry.type in {"Point", "GridCell", "RasterCell"}:
            lon, lat = float(geometry.coordinates[0]), float(geometry.coordinates[1])
            return haversine_km(q_lat, q_lon, lat, lon)
        if geometry.type == "Polygon":
            points = _points(geometry.coordinates)
            if not points:
                return UNKNOWN_DISTANCE_KM
            if point_in_polygon(q_lat, q_lon, geometry.coordinates):
                return 0.0
            return min(haversine_km(q_lat, q_lon, lat, lon) for lon, lat in points)
    except (TypeError, ValueError, IndexError):
        return UNKNOWN_DISTANCE_KM
    return UNKNOWN_DISTANCE_KM
