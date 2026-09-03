"""The unified LLM extraction funnel: location/time separation, Hinglish/STT typos,
off-topic guardrail, and the deterministic fallback when the LLM is unavailable.

The LLM gateway itself is exercised in test_llm_client.py; here `small_llm` is stubbed
directly so these tests check the extractor's own parsing/validation/fallback logic,
not the HTTP transport.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.llm.client import LLMResult
from app.schemas.query import QueryIntent
from app.services import query_extractor


def _stub_llm(monkeypatch, payload: dict | None, *, available: bool = True):
    async def fake_small_llm(messages, **kwargs):
        if not available:
            return LLMResult(tier="small", available=False, error="stubbed unavailable")
        return LLMResult(tier="small", available=True, text=json.dumps(payload))
    monkeypatch.setattr(query_extractor, "small_llm", fake_small_llm)


def test_hinglish_stt_extraction(monkeypatch):
    _stub_llm(monkeypatch, {
        "normalized_location": "Bangalore", "normalized_time": "tomorrow",
        "intent": "forecast", "is_weather_related": True, "confidence_score": 0.9,
    })
    result = asyncio.run(query_extractor.extract_and_normalize("kal banagalore mein barish hogi kya"))
    assert result.normalized_location == "Bangalore"
    assert result.normalized_time == "tomorrow"
    assert result.is_weather_related is True
    assert result.intent == QueryIntent.FORECAST
    assert result.extraction_source == "llm"


def test_clean_query_with_llm_down_is_marked_deterministic_fallback(monkeypatch):
    """Cost/latency observability: a read that never touched the LLM must say so."""
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_extractor.extract_and_normalize("weather in Mumbai"))
    assert result.normalized_location == "Mumbai"
    assert result.extraction_source == "deterministic_fallback"


def test_location_and_absolute_date_are_separated(monkeypatch):
    """The A4 bug: location and a trailing absolute date must not merge."""
    _stub_llm(monkeypatch, {
        "normalized_location": "Rajkot", "normalized_time": "2026-08-01",
        "intent": "forecast", "is_weather_related": True, "confidence_score": 0.95,
    })
    result = asyncio.run(query_extractor.extract_and_normalize("rainfall in Rajkot on 2026-08-01"))
    assert result.normalized_location == "Rajkot"
    assert result.normalized_time == "2026-08-01"


def test_off_topic_question_is_flagged(monkeypatch):
    _stub_llm(monkeypatch, {
        "normalized_location": None, "normalized_time": None,
        "intent": "unknown", "is_weather_related": False, "confidence_score": 0.95,
    })
    result = asyncio.run(query_extractor.extract_and_normalize("Who is the prime minister of India?"))
    assert result.is_weather_related is False


def test_severe_typo_is_corrected(monkeypatch):
    _stub_llm(monkeypatch, {
        "normalized_location": "Mumbai", "normalized_time": None,
        "intent": "current", "is_weather_related": True, "confidence_score": 0.85,
    })
    result = asyncio.run(query_extractor.extract_and_normalize("wether in mumba"))
    assert result.normalized_location == "Mumbai"


def test_llm_unavailable_falls_back_to_deterministic_gate(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_extractor.extract_and_normalize("will it rain in Indore tomorrow"))
    assert result.is_weather_related is True
    assert result.normalized_location == "Indore"
    # Must clear the 0.7 confidence gate: an unconfigured LLM must not silently
    # 400 every legitimate weather question.
    assert result.confidence_score >= 0.7


def test_llm_unavailable_fallback_still_rejects_off_topic(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_extractor.extract_and_normalize("who is the prime minister of India"))
    assert result.is_weather_related is False


def test_malformed_llm_json_falls_back(monkeypatch):
    async def fake_small_llm(messages, **kwargs):
        return LLMResult(tier="small", available=True, text="not valid json at all")
    monkeypatch.setattr(query_extractor, "small_llm", fake_small_llm)
    result = asyncio.run(query_extractor.extract_and_normalize("will it rain in Indore tomorrow"))
    assert result.normalized_location == "Indore"


def test_markdown_fenced_json_is_stripped(monkeypatch):
    async def fake_small_llm(messages, **kwargs):
        payload = json.dumps({
            "normalized_location": "Pune", "normalized_time": "today",
            "intent": "current", "is_weather_related": True, "confidence_score": 0.9,
        })
        return LLMResult(tier="small", available=True, text=f"```json\n{payload}\n```")
    monkeypatch.setattr(query_extractor, "small_llm", fake_small_llm)
    result = asyncio.run(query_extractor.extract_and_normalize("weather in Pune today"))
    assert result.normalized_location == "Pune"


@pytest.mark.parametrize("bad_intent", ["nonsense", "", None])
def test_unknown_intent_value_falls_back_to_unknown_enum(monkeypatch, bad_intent):
    async def fake_small_llm(messages, **kwargs):
        return LLMResult(tier="small", available=True, text=json.dumps({
            "normalized_location": "Delhi", "normalized_time": None,
            "intent": bad_intent, "is_weather_related": True, "confidence_score": 0.8,
        }))
    monkeypatch.setattr(query_extractor, "small_llm", fake_small_llm)
    result = asyncio.run(query_extractor.extract_and_normalize("weather in Delhi"))
    assert result.intent == QueryIntent.UNKNOWN
