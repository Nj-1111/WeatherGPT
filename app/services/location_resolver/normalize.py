"""Deterministic text normalization. No NLP, no LLM — regex and a small alias table.

Multilingual extraction is intentionally out of scope here; it can be added later as a
separate layer that feeds normalized Romanized text into this module.
"""
from __future__ import annotations

import re

from app.services.time_parser import MONTH_PATTERN

# Historical/colloquial renames. This is alias normalization, NOT a gazetteer — geocoding
# providers supply the actual coordinates. Kept deliberately small; entries earn their
# place only when providers genuinely fail on the old name (verified: "Bombay" resolves
# to Bombay, New York on Open-Meteo without this).
ALIASES = {
    "bombay": "Mumbai",
    "calcutta": "Kolkata",
    "madras": "Chennai",
    "bangalore": "Bengaluru",
    "poona": "Pune",
    "baroda": "Vadodara",
    "trivandrum": "Thiruvananthapuram",
    "cochin": "Kochi",
    "mysore": "Mysuru",
    "pondicherry": "Puducherry",
    "gurgaon": "Gurugram",
    "allahabad": "Prayagraj",
    "simla": "Shimla",
    "benares": "Varanasi",
    "banaras": "Varanasi",
    # Abbreviations and common typos of major Indian cities. "chenai" is the exact
    # live-verified typo that used to resolve to a French village.
    "bnglr": "Bengaluru",
    "blr": "Bengaluru",
    "mum": "Mumbai",
    "chenai": "Chennai",
    "hyd": "Hyderabad",
    "del": "Delhi",
    "dilli": "Delhi",
    "cbe": "Coimbatore",
    "vizag": "Visakhapatnam",
}

# Phrases that precede a place name in a weather question.
_LEAD_PATTERNS = [
    r"\bweather\s+(?:in|at|for|around|near)\b",
    r"\bforecast\s+(?:in|at|for|around|near)\b",
    r"\btemperature\s+(?:in|at|for|around|near)\b",
    r"\brain(?:fall)?\s+(?:in|at|for|around|near)\b",
    r"\b(?:will|is|does)\s+it\s+rain\s+(?:in|at|for|around|near)\b",
    # "to" catches disaster/route phrasing ("cyclone coming to X", "route to Y") that the
    # more specific weather-prefixed patterns above don't need to cover.
    r"\b(?:in|at|for|around|near|to)\b",
]

# Trailing RELATIVE time expressions that are not part of the place name.
_TRAILING_TIME = re.compile(
    r"\b(right\s+now|today|tonight|tomorrow|yesterday|now|currently|"
    r"day\s+after\s+tomorrow|next\s+week|this\s+week|this\s+weekend|weekend|"
    r"next\s+\w+day|coming\s+\w+day|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"in\s+\d+\s+hours?|next\s+\d+\s+days?|"
    r"morning|afternoon|evening|night)\b.*$",
    re.IGNORECASE,
)

# Trailing ABSOLUTE dates and clock times. A separate pattern because these carry digits
# and usually arrive attached to a preposition, where the relative words above do not.
# Without this the geocoder was handed "Rajkot on 2026-08-01" and answered 404 — after
# two upstream calls and 1.26s, for a question the time parser understood perfectly.
_TRAILING_ABSOLUTE = re.compile(
    rf"\s*(?:\b(?:on|at|for|from|during|by|before|after)\s+)?(?:"
    rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}"                                   # 2026-08-01
    rf"|\d{{1,2}}\s*[:.]\s*\d{{2}}\s*(?:[ap]\.?m\.?)?"                # 17:00, 5.30pm
    rf"|\d{{1,2}}\s*[ap]\.?m\.?(?![a-z])"                             # 5pm
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTH_PATTERN})\b"             # 5 aug, 3rd october
    rf"|(?:{MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?\b"             # august 5
    rf").*$",
    re.IGNORECASE,
)

# "at 6" is a time; a bare trailing number is not, so the preposition is required here.
_TRAILING_BARE_HOUR = re.compile(r"\s*\bat\s+\d{1,2}\s*(?:o'?clock)?\s*$", re.IGNORECASE)

# Removing "monday" from "Nagpur on monday" leaves the preposition stranded, and
# "Nagpur on" geocodes no better than "Nagpur on monday" did.
_DANGLING_PREPOSITION = re.compile(r"\s+\b(?:on|at|for|from|during|by|before|after|in)\s*$",
                                   re.IGNORECASE)

_PUNCT_EDGES = re.compile(r"^[\s,.;:!?'\"-]+|[\s,.;:!?'\"-]+$")
_WHITESPACE = re.compile(r"\s+")


def normalize_query(text: str) -> str:
    """Collapse whitespace, strip edge punctuation, apply alias mapping.

    Preserves internal commas — providers use "City, State" to disambiguate.
    """
    if not text:
        return ""
    cleaned = _WHITESPACE.sub(" ", text).strip()
    cleaned = _PUNCT_EDGES.sub("", cleaned)
    if not cleaned:
        return ""
    return _apply_aliases(cleaned)


def _apply_aliases(text: str) -> str:
    """Replace historical names token-wise, preserving any ', State' qualifier."""
    parts = [part.strip() for part in text.split(",")]
    head = parts[0]
    if head.casefold() in ALIASES:
        parts[0] = ALIASES[head.casefold()]
    return ", ".join(part for part in parts if part)


def cache_key(text: str) -> str:
    """Case- and whitespace-insensitive key so 'PATNA', 'patna', ' Patna ' share a cache entry."""
    return _WHITESPACE.sub(" ", (text or "").strip().casefold())


# Phrases that mean "wherever I am," not a real place name. A free-text geocoder has no way
# to know these aren't places — a fuzzy match can still land on some unrelated foreign
# village (verified live: "near me" -> Mme-Bafumen, Cameroon).
_SELF_REFERENTIAL_PHRASES = {
    "me", "near me", "my location", "my current location", "current location",
    "here", "my area", "my city", "nearby",
}


def is_self_referential(phrase: str) -> bool:
    return (phrase or "").strip().casefold() in _SELF_REFERENTIAL_PHRASES


def extract_place_phrase(text: str) -> str | None:
    """Pull a probable place name out of a full question, deterministically.

    "will it rain in Indore tomorrow" -> "Indore"
    "will it rain tomorrow"           -> None
    "weather near me"                 -> None (self-referential, not a place)
    """
    if not text:
        return None
    for pattern in _LEAD_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        tail = text[match.end():]
        for stripper in (_TRAILING_ABSOLUTE, _TRAILING_TIME, _TRAILING_BARE_HOUR,
                         _DANGLING_PREPOSITION):
            tail = stripper.sub("", tail)
        tail = _PUNCT_EDGES.sub("", _WHITESPACE.sub(" ", tail).strip())
        # Place names are short; a long tail means the regex caught a sentence, not a place.
        if tail and len(tail.split()) <= 5 and not is_self_referential(tail):
            return tail
    return None
