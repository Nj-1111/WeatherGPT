"""Deterministic input gate, applied before any network call.

Strict by design: a question with no weather term is rejected rather than guessed at,
because every accepted request spends upstream quota under our IP.
"""
from __future__ import annotations

import re

from app.config import settings
from app.errors import WeatherGPTError
from app.orchestrator.retrieval_planner import has_word

_TOPIC_WORDS = (
    "weather", "forecast", "rain", "rainfall", "rains", "raining", "shower", "showers",
    "temperature", "temp", "hot", "cold", "heat", "humid", "humidity", "wind", "windy",
    "gust", "storm", "thunderstorm", "cyclone", "flood", "fog", "snow", "hail", "monsoon",
    "climate", "sunny", "cloudy", "cloud", "warning", "warnings", "alert", "alerts",
    "precipitation", "drizzle", "mausam", "baarish", "barish", "hawa",
    "spray", "spraying", "irrigate", "irrigation", "harvest", "harvesting", "travel",
    "drive", "driving", "fish", "fishing", "sail", "sowing",
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


def check_question(question: str) -> None:
    """Raise WeatherGPTError if the question must not reach retrieval."""
    if not settings.guardrail_enabled:
        return
    text = (question or "").strip()
    reason = None
    if len(text) < settings.guardrail_min_chars:
        reason = "Question is too short to identify a weather request."
    elif len(text) > settings.guardrail_max_chars:
        reason = f"Question exceeds {settings.guardrail_max_chars} characters."
    elif len(text.split()) > settings.guardrail_max_words:
        reason = f"Question exceeds {settings.guardrail_max_words} words."
    elif _CONTROL.search(text):
        reason = "Question contains control characters."
    elif _INJECTION.search(text):
        reason = "Question contains instruction-injection patterns."
    elif _URL.search(text):
        reason = "Question contains a URL."
    elif not has_word(text.casefold(), _TOPIC_WORDS):
        reason = "Question is not a weather request. Ask about weather conditions, or a weather-dependent decision."
    if reason:
        raise WeatherGPTError("QUESTION_REJECTED", reason, {"question_length": len(text)}, 400)
