"""Deterministic input gate, applied before any network call. Only ever a positive fast-path
(a keyword hit lets a request skip ahead) — a miss is never treated as proof a question is
off-topic, since a fixed keyword list cannot cover every language. Real topic/language
judgement is the guardrail LLM's job (query_guardrail.py); when that's unavailable, an
unmatched question degrades to a clarifying question, never a false rejection."""
from __future__ import annotations

import re

from app.config import settings
from app.errors import WeatherGPTError
from app.orchestrator.retrieval_planner import has_word

# A positive-only accelerator: a hit lets the deterministic fallback accept immediately
# without waiting on anything else. A miss proves nothing — it is never used to reject.
TOPIC_WORDS = (
    "weather", "forecast", "rain", "rainfall", "rains", "raining", "shower", "showers",
    "temperature", "temp", "hot", "cold", "heat", "humid", "humidity", "wind", "windy",
    "gust", "storm", "thunderstorm", "cyclone", "flood", "flooding", "fog", "snow", "hail", "monsoon",
    "climate", "sunny", "cloudy", "cloud", "warning", "warnings", "alert", "alerts",
    "precipitation", "drizzle", "mausam", "baarish", "barish", "hawa",
    "spray", "spraying", "irrigate", "irrigation", "harvest", "harvesting", "travel",
    "drive", "driving", "fish", "fishing", "sail", "sowing",
    # marine/fishing, mountain weather, route/disaster framing — widened alongside query_guardrail.py's own scope broadening
    "wave", "waves", "current", "currents", "swell", "tide", "tides", "ocean", "sea",
    "mountain", "mountains", "trek", "trekking", "hiking", "summit", "altitude",
    "route", "commute", "journey", "disaster", "evacuate", "evacuation",
)

_INJECTION = re.compile(
    r"ignore\s+(?:the\s+)?(?:previous|above|prior|all)"
    r"|disregard\s+(?:the\s+)?(?:previous|above|prior|all)"
    r"|system\s+prompt|you\s+are\s+now|act\s+as\s+(?:a|an|if)|pretend\s+to\s+be"
    r"|jailbreak|reveal\s+your|repeat\s+your\s+instructions"
    r"|^\s*(?:system|assistant|user)\s*:"
    r"|<\||```|\{\{|\$\{",
    re.IGNORECASE | re.MULTILINE,
)
_URL = re.compile(r"https?://", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Exempted from the length floor below: real, short, unambiguous greetings that would
# otherwise be rejected as "too short" before query_guardrail.py ever gets to classify them
# as GuardrailAction.GREETING. Narrower than TOPIC_WORDS deliberately — only exact short
# greetings, not a general carve-out from the junk filter.
_SHORT_GREETINGS = {"hi", "hey", "yo"}


def _fast_reason(text: str) -> str | None:
    """Length/control-character/injection/URL checks only, no topic judgement — cheap and unconditional, must run before any LLM call so junk/injection-shaped input never reaches (or costs) the extractor."""
    if len(text) < settings.guardrail_min_chars and text.casefold() not in _SHORT_GREETINGS:
        return "Question is too short to identify a weather request."
    if len(text) > settings.guardrail_max_chars:
        return f"Question exceeds {settings.guardrail_max_chars} characters."
    if len(text.split()) > settings.guardrail_max_words:
        return f"Question exceeds {settings.guardrail_max_words} words."
    if _CONTROL.search(text):
        return "Question contains control characters."
    if _INJECTION.search(text):
        return "Question contains instruction-injection patterns."
    if _URL.search(text):
        return "Question contains a URL."
    return None


def check_question_fast(question: str) -> None:
    """Raise WeatherGPTError on the cheap, deterministic checks only — topic relevance is NOT checked here (that's query_guardrail.py's job); this exists so garbage/injection input is rejected before it reaches the LLM, not instead of the topic check."""
    if not settings.guardrail_enabled:
        return
    reason = _fast_reason((question or "").strip())
    if reason:
        raise WeatherGPTError("QUESTION_REJECTED", reason, {"question_length": len(question or "")}, 400)


def is_definitely_weather_related(question: str) -> bool:
    """Positive-only signal: True means a known weather/marine/travel word matched (safe to
    fast-path an accept). False means nothing matched — that is NOT evidence of being
    off-topic, only that this fixed list didn't recognize the wording or language. Callers
    must never turn False into a rejection; that judgement call belongs to the guardrail LLM."""
    return has_word((question or "").strip().casefold(), TOPIC_WORDS)
