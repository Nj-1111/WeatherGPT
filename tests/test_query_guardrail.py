"""The guardrail decision funnel: one LLM call classifies a query into a strict action,
never left to the LLM's own judgment beyond the fixed decision-tree rules in the prompt.

`small_llm` is stubbed directly rather than the HTTP layer beneath it.
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
from app.services.input_pipeline import query_guardrail


def _stub_llm(monkeypatch, payload: dict | None, *, available: bool = True):
    async def fake_small_llm(messages, **kwargs):
        if not available:
            return LLMResult(tier="small", available=False, error="stubbed unavailable")
        return LLMResult(tier="small", available=True, text=json.dumps(payload))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)


def test_llm_accepts_weather_full(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Bangalore"],
                            "time_phrases": ["tomorrow"], "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("kal banagalore mein barish hogi kya"))
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL
    assert result.location == "Bangalore"
    assert result.time == "tomorrow"
    assert result.extraction_source == "llm"


def test_llm_reports_detected_lang(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Kolkata"],
                            "time_phrases": [], "verify_candidate": None, "clarify_reason": None,
                            "confidence": 0.9, "detected_lang": "bn"})
    result = asyncio.run(query_guardrail.run_guardrail("কলকাতায় এখন কি বৃষ্টি হচ্ছে"))
    assert result.detected_lang == "bn"


def test_llm_omitting_detected_lang_defaults_to_en(monkeypatch):
    """Back-compat: every payload in this file predating detected_lang omits the key."""
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Bangalore"],
                            "time_phrases": ["tomorrow"], "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("will it rain in Bangalore tomorrow"))
    assert result.detected_lang == "en"


def test_llm_accepts_location_only(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_location_only", "locations": ["Bangalore"],
                            "time_phrases": [], "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.95})
    result = asyncio.run(query_guardrail.run_guardrail("what are the coordinates of Bangalore?"))
    assert result.action == GuardrailAction.ACCEPT_LOCATION_ONLY
    assert result.location == "Bangalore"


def test_llm_rejects_off_topic(monkeypatch):
    _stub_llm(monkeypatch, {"action": "reject_off_topic", "locations": [], "time_phrases": [],
                            "verify_candidate": None, "clarify_reason": None, "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("who is the prime minister of India?"))
    assert result.action == GuardrailAction.REJECT_OFF_TOPIC


def test_llm_clarifies_garbled_input(monkeypatch):
    _stub_llm(monkeypatch, {"action": "clarify", "locations": [], "time_phrases": [],
                            "verify_candidate": None, "clarify_reason": "garbled_input",
                            "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("asdkj qwoeiu"))
    assert result.action == GuardrailAction.CLARIFY
    assert result.clarify_reason == ClarifyReason.GARBLED_INPUT


def test_llm_clarifies_no_location(monkeypatch):
    _stub_llm(monkeypatch, {"action": "clarify", "locations": [], "time_phrases": ["tomorrow"],
                            "verify_candidate": None, "clarify_reason": "no_location",
                            "confidence": 0.8})
    result = asyncio.run(query_guardrail.run_guardrail("will it rain tomorrow?"))
    assert result.action == GuardrailAction.CLARIFY
    assert result.clarify_reason == ClarifyReason.NO_LOCATION


def test_llm_verifies_ambiguous_location(monkeypatch):
    _stub_llm(monkeypatch, {"action": "verify", "locations": [], "time_phrases": [],
                            "verify_candidate": "Bangalore", "clarify_reason": None,
                            "confidence": 0.6})
    result = asyncio.run(query_guardrail.run_guardrail("weather in bnglr"))
    assert result.action == GuardrailAction.VERIFY
    assert result.verify_candidate == "Bangalore"


def test_llm_flags_unsupported_disaster_topic(monkeypatch):
    _stub_llm(monkeypatch, {"action": "unsupported_topic", "locations": [], "time_phrases": [],
                            "verify_candidate": None, "clarify_reason": None,
                            "unsupported_topic": "earthquake", "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("was there an earthquake in Delhi?"))
    assert result.action == GuardrailAction.UNSUPPORTED_TOPIC
    assert result.unsupported_topic == "earthquake"


def test_location_and_absolute_date_are_separated(monkeypatch):
    """The A4 bug: location and a trailing absolute date must not merge."""
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Rajkot"],
                            "time_phrases": ["2026-08-01"], "verify_candidate": None,
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
    """The deterministic fallback never attempts language detection (docs/REPORT.md's minimal
    lang-match scope) — it always carries the field's default."""
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("কলকাতায় এখন কি বৃষ্টি হচ্ছে"))
    assert result.detected_lang == "en"


def test_llm_unavailable_falls_back_to_clarify_on_keyword_miss(monkeypatch):
    """A keyword miss during an LLM outage must never be treated as proof of being off-topic
    — the fallback has no reliable way to judge relevance in that state, so it asks rather
    than falsely rejects. This holds for English text that just isn't weather-related..."""
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("who is the prime minister of India"))
    assert result.action == GuardrailAction.CLARIFY
    assert result.clarify_reason == ClarifyReason.SERVICE_DEGRADED


def test_llm_unavailable_falls_back_to_clarify_for_uncovered_language(monkeypatch):
    """...and identically for a genuine weather question in a language the fixed keyword
    list has no coverage for (Bengali, in Latin transliteration) — this is the actual bug:
    the old fallback rejected this exact question as off-topic simply because its words
    weren't in an English/Hindi-only list."""
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("kolkata te ajke brishti hobe ki"))
    assert result.action == GuardrailAction.CLARIFY
    assert result.clarify_reason == ClarifyReason.SERVICE_DEGRADED


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
        payload = json.dumps({"action": "accept_weather_full", "locations": ["Pune"],
                              "time_phrases": ["today"], "verify_candidate": None,
                              "clarify_reason": None, "confidence": 0.9})
        return LLMResult(tier="small", available=True, text=f"```json\n{payload}\n```")
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    result = asyncio.run(query_guardrail.run_guardrail("weather in Pune today"))
    assert result.location == "Pune"
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL


def test_unknown_action_value_falls_back(monkeypatch):
    async def fake_small_llm(messages, **kwargs):
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "nonsense", "locations": ["Delhi"], "time_phrases": [],
            "verify_candidate": None, "clarify_reason": None, "confidence": 0.8}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    result = asyncio.run(query_guardrail.run_guardrail("weather in Delhi"))
    assert result.extraction_source == "deterministic_fallback"
    assert result.action == GuardrailAction.ACCEPT_WEATHER_FULL


def test_llm_reports_capabilities(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Kochi"],
                            "time_phrases": ["tomorrow"], "confidence": 0.9,
                            "capabilities": ["marine", "wind"],
                            "confidence_per_capability": {"marine": "high", "wind": "low"}})
    result = asyncio.run(query_guardrail.run_guardrail("should I go sailing near Kochi tomorrow"))
    assert result.capabilities == ["marine", "wind"]
    assert result.confidence_per_capability == {"marine": "high", "wind": "low"}
    assert result.capabilities_version == "v1"


def test_invented_capability_name_is_dropped_not_fatal(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Kochi"],
                            "time_phrases": [], "confidence": 0.9,
                            "capabilities": ["marine", "made_up_capability"]})
    result = asyncio.run(query_guardrail.run_guardrail("weather near Kochi"))
    assert result.capabilities == ["marine"]


def test_confidence_per_capability_entry_for_unselected_capability_is_dropped(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Kochi"],
                            "time_phrases": [], "confidence": 0.9, "capabilities": ["marine"],
                            "confidence_per_capability": {"marine": "high", "wind": "low"}})
    result = asyncio.run(query_guardrail.run_guardrail("weather near Kochi"))
    assert result.confidence_per_capability == {"marine": "high"}


def test_guidance_flag_alone_triggers_retry_then_falls_back(monkeypatch):
    """travel_safety_guidance with no data capability alongside it is genuinely malformed
    (it carries no data of its own) — this must survive a bounded retry, not crash, and
    land on the deterministic fallback if the retry doesn't fix it either."""
    calls = 0

    async def fake_small_llm(messages, **kwargs):
        nonlocal calls
        calls += 1
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "accept_weather_full", "locations": ["Kochi"], "time_phrases": [],
            "confidence": 0.9, "capabilities": ["travel_safety_guidance"]}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    result = asyncio.run(query_guardrail.run_guardrail("weather near Kochi"))
    assert calls == 2, "one retry, then fall back — not a loop"
    assert result.extraction_source == "deterministic_fallback"


def test_guidance_flag_retry_succeeds_when_second_response_is_valid(monkeypatch):
    calls = 0

    async def fake_small_llm(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResult(tier="small", available=True, text=json.dumps({
                "action": "accept_weather_full", "locations": ["Kochi"], "time_phrases": [],
                "confidence": 0.9, "capabilities": ["travel_safety_guidance"]}))
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "accept_weather_full", "locations": ["Kochi"], "time_phrases": [],
            "confidence": 0.9, "capabilities": ["travel_safety_guidance", "marine"]}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    result = asyncio.run(query_guardrail.run_guardrail("weather near Kochi"))
    assert calls == 2
    assert result.extraction_source == "llm"
    assert set(result.capabilities) == {"travel_safety_guidance", "marine"}


def test_invalid_pairing_mode_defaults_to_locations_x_shared_time(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Delhi", "Mumbai"],
                            "time_phrases": ["this weekend"], "confidence": 0.9,
                            "pairing_mode": "not_a_real_mode"})
    result = asyncio.run(query_guardrail.run_guardrail("compare Delhi and Mumbai this weekend"))
    assert result.pairing_mode == "locations_x_shared_time"


def test_multiple_locations_and_times_are_preserved(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Delhi", "Mumbai"],
                            "time_phrases": ["this weekend"], "confidence": 0.9,
                            "pairing_mode": "locations_x_shared_time"})
    result = asyncio.run(query_guardrail.run_guardrail("compare Delhi and Mumbai this weekend"))
    assert result.locations == ["Delhi", "Mumbai"]
    assert result.location == "Delhi"
    assert result.time_phrases == ["this weekend"]


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


def test_llm_reports_apparent_context(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Kochi"],
                            "time_phrases": ["tomorrow"], "verify_candidate": None,
                            "clarify_reason": None, "confidence": 0.9,
                            "apparent_context": "a fisherman planning a trip"})
    result = asyncio.run(query_guardrail.run_guardrail("should I go fishing near Kochi tomorrow"))
    assert result.apparent_context == "a fisherman planning a trip"


def test_llm_omitting_apparent_context_defaults_to_none(monkeypatch):
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Pune"],
                            "time_phrases": [], "verify_candidate": None, "clarify_reason": None,
                            "confidence": 0.9})
    result = asyncio.run(query_guardrail.run_guardrail("weather in Pune"))
    assert result.apparent_context is None


def test_overlong_apparent_context_is_truncated_not_discarded(monkeypatch):
    """apparent_context is additive, not load-bearing like action — an overlong value must
    be truncated rather than lose the whole decision to a fallback re-run."""
    _stub_llm(monkeypatch, {"action": "accept_weather_full", "locations": ["Pune"],
                            "time_phrases": [], "verify_candidate": None, "clarify_reason": None,
                            "confidence": 0.9, "apparent_context": "x" * 500})
    result = asyncio.run(query_guardrail.run_guardrail("weather in Pune"))
    assert result.apparent_context is not None and len(result.apparent_context) <= 200
    assert result.extraction_source == "llm"
    assert result.location == "Pune"


def test_deterministic_fallback_never_guesses_apparent_context(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("should I go fishing near Kochi tomorrow"))
    assert result.apparent_context is None
    assert result.extraction_source == "deterministic_fallback"


def test_resolve_confirmed_location_never_guesses_apparent_context():
    decision = query_guardrail.resolve_confirmed_location("weather for a fishing trip near kochi", "Kochi")
    assert decision.apparent_context is None


@pytest.mark.parametrize("answer,domain,expected", [
    ("alone", "marine", {"crew_size": 1}),
    ("with 4 of us", "marine", {"crew_size": 4}),
    ("three of us", "marine", {"crew_size": 3}),
    ("small boat, four of us", "marine", {"crew_size": 4, "boat_size": "small"}),
    ("a large trawler", "marine", {"boat_size": "large"}),
    ("not sure", "marine", {}),
    ("alone", "spray", {}),  # a domain with no declared CLARIFYING_FIELDS is a no-op
])
def test_parse_followup_answer(answer, domain, expected):
    assert query_guardrail._parse_followup_answer(answer, domain) == expected


# --- session-aware guardrail: history is plumbed into the prompt and the cache key ---

def test_history_is_rendered_into_the_user_message(monkeypatch):
    captured = {}

    async def fake_small_llm(messages, **kwargs):
        captured["messages"] = messages
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "accept_weather_full", "locations": ["Newtown"], "time_phrases": [],
            "confidence": 0.9}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)

    history = [{"role": "user", "text": "will it rain in Newtown"},
              {"role": "assistant", "text": "Newtown", "action": "accept_weather_full"}]
    asyncio.run(query_guardrail.run_guardrail("can I go play in the evening", history=history))

    user_content = captured["messages"][1]["content"]
    assert "Recent conversation:" in user_content
    assert "will it rain in Newtown" in user_content
    assert "Current message: can I go play in the evening" in user_content


def test_no_history_omits_the_recent_conversation_block(monkeypatch):
    captured = {}

    async def fake_small_llm(messages, **kwargs):
        captured["messages"] = messages
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "accept_weather_full", "locations": ["Pune"], "time_phrases": [], "confidence": 0.9}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)

    asyncio.run(query_guardrail.run_guardrail("weather in Pune"))
    assert captured["messages"][1]["content"] == "weather in Pune"


def test_different_histories_for_the_same_question_do_not_share_a_cache_entry(monkeypatch):
    calls = 0

    async def fake_small_llm(messages, **kwargs):
        nonlocal calls
        calls += 1
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "accept_weather_full", "locations": ["Pune"], "time_phrases": [], "confidence": 0.9}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)

    asyncio.run(query_guardrail.run_guardrail("and tomorrow?", history=[{"role": "user", "text": "weather in Pune"}]))
    asyncio.run(query_guardrail.run_guardrail("and tomorrow?", history=[{"role": "user", "text": "weather in Delhi"}]))
    assert calls == 2, "different conversations must not collide on question text alone"


def test_same_question_and_history_hits_the_cache(monkeypatch):
    calls = 0

    async def fake_small_llm(messages, **kwargs):
        nonlocal calls
        calls += 1
        return LLMResult(tier="small", available=True, text=json.dumps({
            "action": "accept_weather_full", "locations": ["Pune"], "time_phrases": [], "confidence": 0.9}))
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)

    history = [{"role": "user", "text": "weather in Pune"}]
    asyncio.run(query_guardrail.run_guardrail("and tomorrow?", history=history))
    asyncio.run(query_guardrail.run_guardrail("and tomorrow?", history=history))
    assert calls == 1


# --- session-aware guardrail: multilingual fixed templates ---

def test_render_message_translates_reject_off_topic_for_detected_hindi():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="x", action=GuardrailAction.REJECT_OFF_TOPIC, detected_lang="hi")
    message = query_guardrail.render_guardrail_message(decision)
    assert message != query_guardrail.render_guardrail_message(
        GuardrailDecision(original_text="x", action=GuardrailAction.REJECT_OFF_TOPIC, detected_lang="en"))
    assert "मौसम" in message


def test_render_message_translates_clarify_for_detected_hindi():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="x", action=GuardrailAction.CLARIFY,
                                 clarify_reason=ClarifyReason.NO_LOCATION, detected_lang="hi")
    assert query_guardrail.render_guardrail_message(decision) == "यह किस जगह के बारे में है?"


def test_render_message_falls_back_to_english_for_an_unlisted_language():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="x", action=GuardrailAction.REJECT_OFF_TOPIC, detected_lang="fr")
    assert query_guardrail.render_guardrail_message(decision) == query_guardrail.render_guardrail_message(
        GuardrailDecision(original_text="x", action=GuardrailAction.REJECT_OFF_TOPIC, detected_lang="en"))


def test_llm_accepts_greeting(monkeypatch):
    _stub_llm(monkeypatch, {"action": "greeting", "locations": [], "time_phrases": [],
                            "verify_candidate": None, "clarify_reason": None, "confidence": 1.0})
    result = asyncio.run(query_guardrail.run_guardrail("hi"))
    assert result.action == GuardrailAction.GREETING


def test_deterministic_fallback_accepts_greeting(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    for text in ("hi", "hello", "hey there", "who are you", "what can you do"):
        result = asyncio.run(query_guardrail.run_guardrail(text))
        assert result.action == GuardrailAction.GREETING, text
    assert result.extraction_source == "deterministic_fallback"


def test_deterministic_fallback_does_not_treat_embedded_greeting_as_bare(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(query_guardrail.run_guardrail("hii will it rain in Pune tomorrow"))
    assert result.action != GuardrailAction.GREETING


def test_render_message_greeting_is_none():
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="hi", action=GuardrailAction.GREETING)
    assert query_guardrail.render_guardrail_message(decision) is None


def _stub_llm_text(monkeypatch, text: str | None, *, available: bool = True):
    captured: dict = {}

    async def fake_small_llm(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        if not available or text is None:
            return LLMResult(tier="small", available=False, error="stubbed unavailable")
        return LLMResult(tier="small", available=True, text=text)
    monkeypatch.setattr(query_guardrail, "small_llm", fake_small_llm)
    return captured


def test_generate_greeting_reply_calls_llm(monkeypatch):
    from app.schemas.query import GuardrailDecision
    captured = _stub_llm_text(monkeypatch, "Hey there! Ready to check the skies whenever you are.")
    decision = GuardrailDecision(original_text="hi", action=GuardrailAction.GREETING)
    reply = asyncio.run(query_guardrail.generate_greeting_reply(decision))
    assert reply == "Hey there! Ready to check the skies whenever you are."
    assert captured["kwargs"]["temperature"] > 0


def test_generate_greeting_reply_varies(monkeypatch):
    from app.schemas.query import GuardrailDecision
    decision = GuardrailDecision(original_text="hi", action=GuardrailAction.GREETING)
    _stub_llm_text(monkeypatch, "Hey! What's the weather question on your mind?")
    first = asyncio.run(query_guardrail.generate_greeting_reply(decision))
    _stub_llm_text(monkeypatch, "Hello there, happy to help with anything weather-related.")
    second = asyncio.run(query_guardrail.generate_greeting_reply(decision))
    assert first != second


def test_generate_greeting_reply_falls_back_on_llm_failure(monkeypatch):
    from app.schemas.query import GuardrailDecision
    _stub_llm_text(monkeypatch, None, available=False)
    decision = GuardrailDecision(original_text="hi", action=GuardrailAction.GREETING)
    reply = asyncio.run(query_guardrail.generate_greeting_reply(decision))
    assert reply == query_guardrail._GREETING_FALLBACK


def test_generate_greeting_reply_sends_detected_language(monkeypatch):
    from app.schemas.query import GuardrailDecision
    captured = _stub_llm_text(monkeypatch, "Namaste! Aap kis jagah ka mausam jaanna chahenge?")
    decision = GuardrailDecision(original_text="namaste", action=GuardrailAction.GREETING, detected_lang="hi")
    asyncio.run(query_guardrail.generate_greeting_reply(decision))
    user_message = captured["messages"][-1]["content"]
    assert "hi" in user_message
