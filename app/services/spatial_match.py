"""Distance and containment between evidence geometry and the query point."""
from __future__ import annotations

import math

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
