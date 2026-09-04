"""Deterministic input gate, applied before any network call.

Strict by design: a question with no weather term is rejected rather than guessed at,
because every accepted request spends upstream quota under our IP.
"""
from __future__ import annotations

import re

from app.config import settings
from app.errors import WeatherGPTError
from app.orchestrator.retrieval_planner import has_word

TOPIC_WORDS = (
    "weather", "forecast", "rain", "rainfall", "rains", "raining", "shower", "showers",
    "temperature", "temp", "hot", "cold", "heat", "humid", "humidity", "wind", "windy",
    "gust", "storm", "thunderstorm", "cyclone", "flood", "flooding", "fog", "snow", "hail", "monsoon",
    "climate", "sunny", "cloudy", "cloud", "warning", "warnings", "alert", "alerts",
    "precipitation", "drizzle", "mausam", "baarish", "barish", "hawa",
    "spray", "spraying", "irrigate", "irrigation", "harvest", "harvesting", "travel",
    "drive", "driving", "fish", "fishing", "sail", "sowing",
    # marine/fishing conditions, mountain weather, route/disaster framing — widened
    # alongside query_guardrail.py's own scope broadening
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


def _fast_reason(text: str) -> str | None:
    """Length/control-character/injection/URL checks only — no topic judgement.

    Cheap and unconditional: it must run before any LLM call so a junk or
    injection-shaped payload never reaches (and never costs) the extractor.
    """
    if len(text) < settings.guardrail_min_chars:
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
    """Raise WeatherGPTError on the cheap, deterministic checks only.

    Topic relevance is NOT checked here — that's the guardrail's job
    (services/query_guardrail.py). This exists so garbage/injection input is
    rejected before it ever reaches the LLM, not instead of the topic check.
    """
    if not settings.guardrail_enabled:
        return
    reason = _fast_reason((question or "").strip())
    if reason:
        raise WeatherGPTError("QUESTION_REJECTED", reason, {"question_length": len(question or "")}, 400)


def check_question(question: str) -> None:
    """Full deterministic gate: fast checks plus the topic-word check.

    Used directly when the guardrail runs standalone, and as the fallback path
    inside query_guardrail.run_guardrail when the LLM is unavailable — so topic
    relevance still gets *some* check rather than none.
    """
    if not settings.guardrail_enabled:
        return
    text = (question or "").strip()
    reason = _fast_reason(text)
    if reason is None and not has_word(text.casefold(), TOPIC_WORDS):
        reason = "Question is not a weather request. Ask about weather conditions, or a weather-dependent decision."
    if reason:
        raise WeatherGPTError("QUESTION_REJECTED", reason, {"question_length": len(text)}, 400)
