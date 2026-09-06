"""The guardrail decision funnel: one LLM call classifies a query into a strict action,
never left to the LLM's own judgment beyond the fixed decision-tree rules in the prompt.

`small_llm` is stubbed directly, mirroring tests/test_robust_pipeline.py's approach for
the sibling query_extractor.py module.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
import pytest

from app.config import LLMEndpoint, settings
from app.llm.client import LLMResult
from app.schemas.query import ClarifyReason, GuardrailAction
from app.services import query_guardrail


def _stub_llm(monkeypatch, payload: dict | None, *, available: bool = True):
    async def fake_small_llm(messages, **kwargs):
        if not available:
            return LLMResult(tier="small", available=False, error="stubbed unavailable")
        return LLMResult(tier="small", available=True, text=json.dumps(payload))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)


def test_llm_accepts_weather_full(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "location": "Bangalore",
                            "time": "tomorrow", "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("kal banagalore mein barish hogi kya"))
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL
    assert result.location == "Bangalore"
    assert result.time == "tomorrow"
    assert result.extraction_source == "llm"


def test_llm_reports_detected_lang(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "location": "Kolkata",
                            "time": None, "verify_candidate": None, "clarify_reason": None,
                            "confidence": 0.9, "detected_lang": "bn"})
    result = asyncio.run(query_guardrail.run_guardrail("কলকাতায় এখন কি বৃষ্টি হচ্ছে"))
    assert result.detected_lang == "bn"


def test_llm_omitting_detected_lang_defaults_to_en(monkeypatch):
    """Back-compat: every payload in this file predating detected_lang omits the key."""
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "location": "Bangalore",
                            "time": "tomorrow", "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("will it rain in Bangalore tomorrow"))
    assert result.detected_lang == "en"


def test_llm_accepts_location_only(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_location_only", "location": "Bangalore",
                            "time": None, "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.95})
    result = asyncio.run(query_guardrail.run_guardrail("what are the coordinates of Bangalore?"))
    assert result.action == GuardrailAction.ACCEPT_LOCATION_ONLY
    assert result.location == "Bangalore"


def test_llm_rejects_off_topic(monkeypatch):
    _stub_llm(monkeypatch, {"action": "reject_off_topic", "location": None, "time": None,
                            "verify_candidate": None, "clarify_reason": None, "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("who is the prime minister of India?"))
    assert result.action == GuardrailAction.REJECT_OFF_TOPIC


def test_llm_clarifies_garbled_input(monkeypatch):
    _stub_llm(monkeypatch, {"action": "clarify", "location": None, "time": None,
                            "verify_candidate": None, "clarify_reason": "garbled_input",
                            "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("asdkj qwoeiu"))
    assert result.action == GuardrailAction.CLARIFY
    assert result.clarify_reason == ClarifyReason.GARBLED_INPUT


def test_llm_clarifies_no_location(monkeypatch):
    _stub_llm(monkeypatch, {"action": "clarify", "location": None, "time": "tomorrow",
                            "verify_candidate": None, "clarify_reason": "no_location",
                            "confidence": 0.8})
    result = asyncio.run(query_guardrail.run_guardrail("will it rain tomorrow?"))
    assert result.action == GuardrailAction.CLARIFY
    assert result.clarify_reason == ClarifyReason.NO_LOCATION


def test_llm_verifies_ambiguous_location(monkeypatch):
    _stub_llm(monkeypatch, {"action": "verify", "location": None, "time": None,
                            "verify_candidate": "Bangalore", "clarify_reason": None,
                            "confidence": 0.6})
    result = asyncio.run(query_guardrail.run_guardrail("weather in bnglr"))
    assert result.action == GuardrailAction.VERIFY
    assert result.verify_candidate == "Bangalore"


def test_llm_flags_unsupported_disaster_topic(monkeypatch):
    _stub_llm(monkeypatch, {"action": "unsupported_topic", "location": None, "time": None,
                            "verify_candidate": None, "clarify_reason": None,
                            "unsupported_topic": "earthquake", "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("was there an earthquake in Delhi?"))
    assert result.action == GuardrailAction.UNSUPPORTED_TOPIC
    assert result.unsupported_topic == "earthquake"


def test_location_and_absolute_date_are_separated(monkeypatch):
    """The A4 bug: location and a trailing absolute date must not merge."""
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "location": "Rajkot",
                            "time": "2026-08-01", "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.95})
    result = asyncio.run(query_guardrail.run_guardrail("rainfall in Rajkot on 2026-08-01"))
    assert result.location == "Rajkot"
    assert result.time == "2026-08-01"


def test_gemini_endpoint_failure_degrades_to_deterministic_fallback(monkeypatch):
    """End-to-end through the real gateway (not the small_llm stub used above): a Gemini
    endpoint configured in settings that fails at the HTTP layer must never surface as an
    exception or a 500 — the guardrail must still return a valid decision."""
    class _FailingClient:
        async def post(self, url, **kwargs):
            raise httpx.ConnectTimeout("gemini unreachable")

    endpoint = LLMEndpoint(model="gemini-1.5-flash",
                           base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                           api_key="invalid-or-expired-key")
    monkeypatch.setattr("app.llm.client.settings", dataclasses.replace(
        settings, llm_enabled=True, small_llm_chain=(endpoint,)))
    monkeypatch.setattr("app.llm.client.get_client", lambda: _FailingClient())

    result = asyncio.run(query_guardrail.run_guardrail("will it rain in Indore tomorrow"))
    assert result.extraction_source == "deterministic_fallback"
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL
    assert result.location == "Indore"


def test_llm_unavailable_falls_back_for_weather_question(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("will it rain in Indore tomorrow"))
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL
    assert result.location == "Indore"
    assert result.extraction_source == "deterministic_fallback"


def test_llm_unavailable_falls_back_detected_lang_stays_en(monkeypatch):
    """The deterministic fallback never attempts language detection (io.md's minimal
    lang-match scope) — it always carries the field's default."""
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("কলকাতায় এখন কি বৃষ্টি হচ্ছে"))
    assert result.detected_lang == "en"


def test_llm_unavailable_falls_back_for_off_topic(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("who is the prime minister of India"))
    assert result.action == GuardrailAction.REJECT_OFF_TOPIC


def test_llm_unavailable_falls_back_for_location_only(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("coordinates of Mumbai"))
    assert result.action == GuardrailAction.ACCEPT_LOCATION_ONLY
    assert result.location == "Mumbai"


def test_llm_unavailable_falls_back_to_clarify_no_location_when_place_missing(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("what are the coordinates"))
    assert result.action == GuardrailAction.CLARIFY
    assert result.clarify_reason == ClarifyReason.NO_LOCATION


def test_llm_unavailable_falls_back_to_unsupported_topic_for_earthquake(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("was there an earthquake in Delhi?"))
    assert result.action == GuardrailAction.UNSUPPORTED_TOPIC
    assert result.unsupported_topic == "earthquake"


@pytest.mark.parametrize("question", [
    "is a cyclone coming to Chennai",
    "will there be flooding in Mumbai",
    "what's the safest route to Pune avoiding the storm",
    "should I go fishing near Kochi tomorrow",
])
def test_llm_unavailable_still_accepts_widened_topics_as_weather(monkeypatch, question):
    """The broadening must widen acceptance, not just add a new rejection path."""
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail(question))
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL


def test_malformed_llm_json_falls_back(monkeypatch):
    async def fake_small_llm(messages, **kwargs):
        return LLMResult(tier="small", available=True, text="not valid json at all")
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    result = asyncio.run(query_guardrail.run_guardrail("will it rain in Indore tomorrow"))
    assert result.extraction_source == "deterministic_fallback"
    assert result.location == "Indore"


def test_markdown_fenced_json_is_stripped(monkeypatch):
    async def fake_small_llm(messages, **kwargs):
        payload = json.dumps({"action": "accept_weather_full", "location": "Pune",
                              "time": "today", "verify_candidate": None,
                              "clarify_reason": None, "confidence": 0.9})
        return LLMResult(tier="small", available=True, text=f"```json\n{payload}\n```")
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    result = asyncio.run(query_guardrail.run_guardrail("weather in Pune today"))
    assert result.location == "Pune"
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL


def test_unknown_action_value_falls_back(monkeypatch):
    async def fake_small_llm(messages, **kwargs):
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "nonsense", "location": "Delhi", "time": None,
            "verify_candidate": None, "clarify_reason": None, "confidence": 0.8}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    result = asyncio.run(query_guardrail.run_guardrail("weather in Delhi"))
    assert result.extraction_source == "deterministic_fallback"
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL


def test_resolve_confirmed_location_only():
    decision = query_guardrail.resolve_confirmed_location("coordinates of bnglr", "Bangalore")
    assert decision.action == GuardrailAction.ACCEPT_LOCATION_ONLY
    assert decision.location == "Bangalore"
    assert decision.extraction_source == "confirmed"


def test_resolve_confirmed_location_weather_full():
    decision = query_guardrail.resolve_confirmed_location("weather in bnglr", "Bangalore")
    assert decision.action == GuardrailAction.ACCEPT_WEATHER_FULL
    assert decision.location == "Bangalore"


def test_render_message_reject_off_topic():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="x", action=GuardrailAction.REJECT_OFF_TOPIC)
    assert "weather" in query_guardrail.render_guardrail_message(decision)


def test_render_message_unsupported_topic_includes_the_topic():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="x", action=GuardrailAction.UNSUPPORTED_TOPIC,
                                 unsupported_topic="earthquake")
    message = query_guardrail.render_guardrail_message(decision)
    assert "earthquake" in message
    assert "cyclone" in message


def test_render_message_clarify_no_location():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="x", action=GuardrailAction.CLARIFY,
                                 clarify_reason=ClarifyReason.NO_LOCATION)
    assert query_guardrail.render_guardrail_message(decision) == "Which location is this about?"


def test_render_message_verify_includes_candidate():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="x", action=GuardrailAction.VERIFY,
                                 verify_candidate="Bangalore")
    message = query_guardrail.render_guardrail_message(decision)
    assert "Bangalore" in message


def test_render_message_none_for_accept_actions():
    from app.schemas.query import GuardrailDecision
    for action in (GuardrailAction.ACCEPT_LOCATION_ONLY, GuardrailAction.ACCEPT_WEATHER_FULL):
        decision = GuardrailDecision(original_text="x", action=action)
        assert query_guardrail.render_guardrail_message(decision) is None
