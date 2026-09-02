"""Location resolution — public surface.

Import path and error contract are unchanged from the previous single-module resolver
(`resolve_location`, `extract_location`, `LocationNotFoundError`, `LocationAmbiguousError`),
so app/main.py needs no import changes. The functions are now async because resolution
performs network geocoding.

Resolution order:
    coordinates -> cache -> PIN code -> normalized place name -> providers -> rank -> ambiguity
"""
from __future__ import annotations

import logging
import time

from app.config import settings
from app.schemas.location import ResolvedLocation
from app.services.location_resolver import detect, normalize, ranking, seed
from app.services.location_resolver.cache import location_cache
from app.services.location_resolver.providers.base import LocationCandidate
from app.services.location_resolver.providers.india_post import IndiaPostProvider
from app.services.location_resolver.providers.nominatim import NominatimGeocoder
from app.services.location_resolver.providers.open_meteo import OpenMeteoGeocoder

logger = logging.getLogger(__name__)

__all__ = ["LocationAmbiguousError", "LocationNotFoundError", "extract_location", "resolve_location"]


class LocationNotFoundError(Exception):
    def __init__(self, raw: str, message: str = "Location not found"):
        self.raw = raw
        super().__init__(f"{message}: {raw}")


class LocationAmbiguousError(Exception):
    def __init__(self, raw: str, candidates: list, message: str = "Location ambiguous"):
        self.raw = raw
        self.candidates = candidates
        super().__init__(f"{message}: {raw} -> {candidates}")


# Ordered fallback chain. Open-Meteo first (keyless, structured, same vendor as the
# weather adapters); Nominatim second because it covers Indian districts, states and
# historical aliases that Open-Meteo's populated-places dataset lacks.
_GEOCODERS = (OpenMeteoGeocoder(), NominatimGeocoder())
_PINCODE_PROVIDER = IndiaPostProvider()


async def resolve_location(raw: str) -> ResolvedLocation:
    if not raw or not raw.strip():
        raise LocationNotFoundError(raw, "Location required but empty")

    started = time.monotonic()

    if detect.looks_like_coordinates(raw):
        coords = detect.parse_coordinates(raw)
        if coords is None:
            raise LocationNotFoundError(raw, "Invalid coordinates")
        logger.info("location.resolved", extra={"method": "coordinates", "cached": False})
        return _from_coordinates(raw, *coords)

    key = normalize.cache_key(raw)
    cached = await location_cache.get(key)
    if cached is not None:
        logger.info("location.cache_hit", extra={"query": key})
        return cached.model_copy(update={"raw": raw})

    pincode = detect.parse_pincode(raw)
    resolved = await (_resolve_pincode(raw, pincode) if pincode else _resolve_place(raw))

    await location_cache.put(key, resolved, settings.location_cache_ttl_seconds)
    logger.info(
        "location.resolved",
        extra={"query": key, "method": resolved.resolution_method, "source": resolved.source,
               "latency_ms": int((time.monotonic() - started) * 1000)},
    )
    return resolved


async def extract_location(text: str) -> ResolvedLocation | None:
    """Pull a location out of a full question. Returns None when none is present."""
    if not text:
        return None
    coords = detect.parse_coordinates(text, inline=True)
    if coords:
        return _from_coordinates(text, *coords)
    pincode = detect.parse_pincode(text, inline=True)
    if pincode:
        return await resolve_location(pincode)
    phrase = normalize.extract_place_phrase(text)
    if phrase:
        return await resolve_location(phrase)
    return None


def _from_coordinates(raw: str, lat: float, lon: float) -> ResolvedLocation:
    return ResolvedLocation(
        raw=raw, lat=lat, lon=lon, confidence=1.0, source="gps",
        normalized_name=f"{lat:.5f},{lon:.5f}", resolution_method="coordinates",
    )


async def _resolve_pincode(raw: str, pincode: str) -> ResolvedLocation:
    """PIN -> district/state via India Post, then geocode that place for coordinates."""
    place = None
    if settings.geocoding_enabled:
        try:
            place = await _PINCODE_PROVIDER.lookup(pincode)
        except Exception as exc:
            logger.warning("location.provider_failed",
                           extra={"provider": _PINCODE_PROVIDER.name, "error": type(exc).__name__})

    if place is not None:
        try:
            candidate = await _first_dominant(place.as_query())
        except LocationAmbiguousError:
            candidate = None
        if candidate is not None:
            return ResolvedLocation(
                raw=raw, lat=candidate.lat, lon=candidate.lon,
                district=place.district, state=place.state, pincode=pincode,
                confidence=0.95, source="pincode", normalized_name=place.district,
                administrative_hierarchy=[place.district, place.state],
                resolution_method="pincode", country="India",
            )

    seeded = seed.seed_pincode(pincode)
    if seeded:
        logger.info("location.seed_fallback", extra={"method": "pincode"})
        return ResolvedLocation(
            raw=raw, lat=seeded["lat"], lon=seeded["lon"], district=seeded["district"],
            state=seeded["state"], pincode=pincode, confidence=0.9, source="pincode_seed",
            normalized_name=seeded["district"],
            administrative_hierarchy=[seeded["district"], seeded["state"]],
            resolution_method="pincode", country="India",
        )
    raise LocationNotFoundError(raw, f"PIN code {pincode} could not be resolved")


async def _resolve_place(raw: str) -> ResolvedLocation:
    query = normalize.normalize_query(raw)
    if not query:
        raise LocationNotFoundError(raw, "Location required but empty")

    ambiguous: list[tuple[float, LocationCandidate]] = []
    if settings.geocoding_enabled:
        for provider in _GEOCODERS:
            try:
                candidates = await provider.search(query)
            except Exception as exc:
                # One provider failing must never end the chain.
                logger.warning("location.provider_failed",
                               extra={"provider": provider.name, "error": type(exc).__name__})
                continue
            winner, dominant, ranked = ranking.select(candidates, query, settings.geocoding_dominance_margin)
            if winner is not None and dominant:
                gap = ranked[0][0] - ranked[1][0] if len(ranked) > 1 else settings.geocoding_dominance_margin * 3
                return _from_candidate(raw, query, winner,
                                       ranking.confidence_for(gap, settings.geocoding_dominance_margin))
            if ranked and not ambiguous:
                # Hold the ambiguity, but let the next provider try for a clean answer.
                ambiguous = ranked

    if ambiguous:
        raise LocationAmbiguousError(raw, [candidate.summary() for _, candidate in ambiguous[:5]])

    seeded = seed.seed_place(query)
    if seeded:
        logger.info("location.seed_fallback", extra={"method": "gazetteer"})
        return ResolvedLocation(
            raw=raw, lat=seeded["lat"], lon=seeded["lon"], district=seeded["district"],
            state=seeded["state"], confidence=0.9, source="gazetteer_seed",
            normalized_name=seeded["name"],
            administrative_hierarchy=[seeded["district"], seeded["state"]],
            resolution_method="offline_gazetteer", country="India",
        )
    raise LocationNotFoundError(raw, "No geocoding provider could resolve this location")


async def _first_dominant(query: str) -> LocationCandidate | None:
    """Geocode a already-normalized query, returning only an unambiguous winner."""
    for provider in _GEOCODERS:
        try:
            candidates = await provider.search(query)
        except Exception as exc:
            logger.warning("location.provider_failed",
                           extra={"provider": provider.name, "error": type(exc).__name__})
            continue
        winner, dominant, _ = ranking.select(candidates, query, settings.geocoding_dominance_margin)
        if winner is not None and dominant:
            return winner
    return None


def _from_candidate(raw: str, query: str, candidate: LocationCandidate, confidence: float) -> ResolvedLocation:
    hierarchy = [part for part in (candidate.admin2, candidate.admin1, candidate.country) if part]
    return ResolvedLocation(
        raw=raw, lat=candidate.lat, lon=candidate.lon,
        district=candidate.admin2, state=candidate.admin1,
        pincode=candidate.postcode, timezone=candidate.timezone, confidence=confidence,
        source=candidate.provider, normalized_name=candidate.name,
        administrative_hierarchy=hierarchy, resolution_method="geocoded",
        country=candidate.country or candidate.country_code,
    )
