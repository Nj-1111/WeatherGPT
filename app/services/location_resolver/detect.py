"""Pure input detection — coordinates and Indian PIN codes. No network, no I/O."""
from __future__ import annotations

import re

_COORD_EXACT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*$")
_COORD_INLINE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_PINCODE_EXACT = re.compile(r"^\s*(\d{6})\s*$")
_PINCODE_INLINE = re.compile(r"\b(\d{6})\b")


def parse_coordinates(text: str, inline: bool = False) -> tuple[float, float] | None:
    """Return (lat, lon) if the text is/contains a valid coordinate pair, else None.

    Returns None for out-of-range values rather than raising — the caller decides
    whether an out-of-range pair is a hard error or just 'not coordinates'.
    """
    if not text:
        return None
    match = (_COORD_INLINE.search(text) if inline else _COORD_EXACT.match(text))
    if not match:
        return None
    try:
        lat, lon = float(match.group(1)), float(match.group(2))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def looks_like_coordinates(text: str) -> bool:
    """True when the text is shaped like a coordinate pair, valid range or not.

    Lets the resolver reject "91.0, 200.0" as invalid coordinates instead of
    silently handing it to a geocoder as a place name.
    """
    return bool(text and _COORD_EXACT.match(text))


def parse_pincode(text: str, inline: bool = False) -> str | None:
    """Return a 6-digit Indian PIN code if present. Indian PINs never start with 0."""
    if not text:
        return None
    match = (_PINCODE_INLINE.search(text) if inline else _PINCODE_EXACT.match(text))
    if not match:
        return None
    pin = match.group(1)
    return pin if pin[0] != "0" else None
