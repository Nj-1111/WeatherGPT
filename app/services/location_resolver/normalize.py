"""Deterministic text normalization. No NLP, no LLM — regex and a small alias table.

Multilingual extraction is intentionally out of scope here; it can be added later as a
separate layer that feeds normalized Romanized text into this module.
"""
from __future__ import annotations

import re

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
}

# Phrases that precede a place name in a weather question.
_LEAD_PATTERNS = [
    r"\bweather\s+(?:in|at|for|around|near)\b",
    r"\bforecast\s+(?:in|at|for|around|near)\b",
    r"\btemperature\s+(?:in|at|for|around|near)\b",
    r"\brain(?:fall)?\s+(?:in|at|for|around|near)\b",
    r"\b(?:will|is|does)\s+it\s+rain\s+(?:in|at|for|around|near)\b",
    r"\b(?:in|at|for|around|near)\b",
]

# Trailing time expressions that are not part of the place name.
_TRAILING_TIME = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|now|currently|"
    r"day\s+after\s+tomorrow|next\s+week|this\s+week|this\s+weekend|weekend|"
    r"next\s+\w+day|coming\s+\w+day|"
    r"in\s+\d+\s+hours?|next\s+\d+\s+days?|"
    r"morning|afternoon|evening|night)\b.*$",
    re.IGNORECASE,
)

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


def extract_place_phrase(text: str) -> str | None:
    """Pull a probable place name out of a full question, deterministically.

    "will it rain in Indore tomorrow" -> "Indore"
    "will it rain tomorrow"           -> None
    """
    if not text:
        return None
    for pattern in _LEAD_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        tail = text[match.end():]
        tail = _TRAILING_TIME.sub("", tail)
        tail = _PUNCT_EDGES.sub("", _WHITESPACE.sub(" ", tail).strip())
        # Place names are short; a long tail means the regex caught a sentence, not a place.
        if tail and len(tail.split()) <= 5:
            return tail
    return None
