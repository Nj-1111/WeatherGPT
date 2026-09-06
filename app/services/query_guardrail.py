"""Guardrail decision funnel: one LLM call classifies a raw query into one strict action (GuardrailAction, replacing a bare is_weather_related boolean) via a fixed decision-tree checklist (_SYSTEM_PROMPT), never the LLM's own judgment — non-ACCEPT user-facing text always comes from render_guardrail_message's fixed templates, never LLM prose. Wired into app/main.py's _weather_request as the sole topic/dispatch gate; degrades to a deterministic fallback on any LLM outage or unparseable output."""
from __future__ import annotations

import json
import logging
import re

from app.config import settings
from app.llm.client import small_llm
from app.orchestrator.retrieval_planner import _DECISION_KEYWORDS, _MARINE_WORDS, has_word
from app.prompts.loader import load_prompt
from app.schemas.query import ClarifyReason, GuardrailAction, GuardrailDecision, Persona
from app.services.cache import TTLCache
from app.services.guardrail import TOPIC_WORDS, check_question
from app.services.location_resolver.normalize import extract_place_phrase

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = load_prompt("guardrail")

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_VALID_ACTIONS = {member.value for member in GuardrailAction}
_VALID_CLARIFY_REASONS = {member.value for member in ClarifyReason}
_VALID_PERSONAS = {member.value for member in Persona}

# Fallback's marine-persona signal matches retrieval_planner's need_marine condition exactly, so it never fires without retrieval also planning marine data; the LLM's own rule is deliberately broader (beach/coastal phrasing too), covered separately by build_retrieval_plan's `persona` param.
_PERSONA_MARINE_WORDS = _DECISION_KEYWORDS["marine"] + _MARINE_WORDS

# Deterministic-fallback-only signal: "coordinates of X" with no weather word — kept separate from guardrail.TOPIC_WORDS so "weather and coordinates of Pune" still reads as a weather request.
_LOCATION_ONLY_WORDS = (
    "coordinates", "coordinate", "latitude", "longitude", "geocode", "pincode",
    "pin code",
)

# extract_place_phrase's patterns are tuned for "weather in X"; "coordinates of X" needs its own narrow lead pattern rather than broadening that shared regex, which "of" would make trigger-happy for every other caller.
_LOCATION_ONLY_LEAD = re.compile(
    r"\b(?:coordinates?|latitude|longitude|geocode|pincode|pin\s*code)s?\s+(?:of|for)\s+",
    re.IGNORECASE,
)

# Disaster types with no backing data source — checked before the topic gate so these get an honest UNSUPPORTED_TOPIC rather than irrelevant weather data or a bare off-topic rejection; distinct from TOPIC_WORDS' cyclone/flood/storm, which ARE backed by CAP.
_UNSUPPORTED_DISASTER_WORDS = ("earthquake", "tsunami", "wildfire", "landslide", "volcano",
                              "volcanic", "drought")


def _extract_location_only_phrase(text: str) -> str | None:
    match = _LOCATION_ONLY_LEAD.search(text)
    if not match:
        return None
    tail = text[match.end():].strip(" ?.!")
    if tail and len(tail.split()) <= 5:
        return tail
    return None


def _is_location_only_phrasing(casefolded_text: str) -> bool:
    return has_word(casefolded_text, _LOCATION_ONLY_WORDS) and not has_word(casefolded_text, TOPIC_WORDS)


def _parse(raw_text: str, llm_text: str) -> GuardrailDecision | None:
    cleaned = _JSON_FENCE.sub("", llm_text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("query_guardrail.unparseable_output", extra={"raw_length": len(llm_text)})
        return None
    action = data.get("action")
    if action not in _VALID_ACTIONS:
        return None
    clarify_reason = data.get("clarify_reason")
    if clarify_reason not in _VALID_CLARIFY_REASONS:
        clarify_reason = None
    # persona is additive, not load-bearing like action: a right-action-wrong-persona LLM output shouldn't lose the whole decision to a fallback re-run.
    persona = data.get("persona")
    if persona not in _VALID_PERSONAS:
        persona = Persona.NONE.value
    try:
        return GuardrailDecision(
            original_text=raw_text,
            action=GuardrailAction(action),
            location=data.get("location") or None,
            time=data.get("time") or None,
            verify_candidate=data.get("verify_candidate") or None,
            clarify_reason=ClarifyReason(clarify_reason) if clarify_reason else None,
            unsupported_topic=data.get("unsupported_topic") or None,
            confidence=float(data.get("confidence", 0.0)),
            extraction_source="llm",
            detected_lang=(data.get("detected_lang") or "en").strip() or "en",
            persona=Persona(persona),
        )
    except (TypeError, ValueError):
        logger.warning("query_guardrail.invalid_fields", extra={"raw_length": len(llm_text)})
        return None


def _deterministic_fallback(raw_text: str) -> GuardrailDecision:
    """Used when the LLM is unavailable or returns unparseable output. Never returns CLARIFY(garbled_input) or VERIFY — those need real language understanding, not a keyword match; CLARIFY(no_location) and UNSUPPORTED_TOPIC are the exceptions since both are still binary keyword checks."""
    text = (raw_text or "").strip()
    casefolded = text.casefold()
    unsupported = next((word for word in _UNSUPPORTED_DISASTER_WORDS if has_word(casefolded, (word,))), None)
    if unsupported:
        return GuardrailDecision(original_text=text, action=GuardrailAction.UNSUPPORTED_TOPIC,
                                 unsupported_topic=unsupported, confidence=1.0,
                                 extraction_source="deterministic_fallback")
    location = extract_place_phrase(text)
    if _is_location_only_phrasing(casefolded):
        location = location or _extract_location_only_phrase(text)
        if location:
            return GuardrailDecision(original_text=text, action=GuardrailAction.ACCEPT_LOCATION_ONLY,
                                     location=location, confidence=1.0, extraction_source="deterministic_fallback")
        return GuardrailDecision(original_text=text, action=GuardrailAction.CLARIFY,
                                 clarify_reason=ClarifyReason.NO_LOCATION, confidence=1.0,
                                 extraction_source="deterministic_fallback")
    try:
        check_question(text)
        is_weather_related = True
    except Exception:
        is_weather_related = False
    action = GuardrailAction.ACCEPT_WEATHER_FULL if is_weather_related else GuardrailAction.REJECT_OFF_TOPIC
    persona = Persona.MARINE if action == GuardrailAction.ACCEPT_WEATHER_FULL and has_word(casefolded, _PERSONA_MARINE_WORDS) else Persona.NONE
    return GuardrailDecision(original_text=text, action=action, location=location,
                             confidence=1.0, extraction_source="deterministic_fallback", persona=persona)


# One LLM call per distinct question, not per request. A fallback decision is never stored — it's a degraded read and must not outlive the outage that produced it.
_decision_cache = TTLCache(settings.guardrail_cache_max_entries)


async def run_guardrail(raw_text: str) -> GuardrailDecision:
    text = (raw_text or "").strip()
    key = text.casefold()
    cached = await _decision_cache.get(key)
    if cached is not None:
        return cached.value.model_copy(update={"original_text": text})
    result = await small_llm(
        [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": text}],
        temperature=0.0,
        max_tokens=200,
    )
    if not result.available or not result.text:
        logger.info("query_guardrail.llm_unavailable", extra={"error": result.error})
        return _deterministic_fallback(text)
    parsed = _parse(text, result.text)
    if parsed is None:
        return _deterministic_fallback(text)
    await _decision_cache.put(key, parsed, settings.guardrail_cache_ttl_seconds)
    return parsed


def resolve_confirmed_location(original_text: str, confirmed_location: str) -> GuardrailDecision:
    """A VERIFY candidate the user just confirmed next turn — the original classification stopped at rule 5 without deciding location-only vs weather-full, so the same branch _deterministic_fallback applies, just with the location already known."""
    casefolded = original_text.casefold()
    action = (GuardrailAction.ACCEPT_LOCATION_ONLY if _is_location_only_phrasing(casefolded)
             else GuardrailAction.ACCEPT_WEATHER_FULL)
    persona = Persona.MARINE if action == GuardrailAction.ACCEPT_WEATHER_FULL and has_word(casefolded, _PERSONA_MARINE_WORDS) else Persona.NONE
    return GuardrailDecision(original_text=original_text, action=action, location=confirmed_location,
                             confidence=1.0, extraction_source="confirmed", persona=persona)


_SOLO_WORDS = ("alone", "solo", "single", "myself")
_SMALL_BOAT_WORDS = ("small", "dinghy", "kayak", "canoe")
_LARGE_BOAT_WORDS = ("large", "big", "trawler")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10}


def _parse_crew_boat_answer(text: str) -> dict:
    """A one-sentence answer to "alone or with a crew? small boat or large?" — a fixed keyword parse, same invariant as the rest of this module; unrecognized text yields an empty update rather than guessing."""
    casefolded = text.casefold()
    update: dict[str, object] = {}
    if has_word(casefolded, _SOLO_WORDS):
        update["crew_size"] = 1
    else:
        digit_match = re.search(r"\b(\d+)\b", casefolded)
        if digit_match:
            update["crew_size"] = int(digit_match.group(1))
        else:
            for word, value in _NUMBER_WORDS.items():
                if has_word(casefolded, (word,)):
                    update["crew_size"] = value
                    break
    if has_word(casefolded, _SMALL_BOAT_WORDS):
        update["boat_size"] = "small"
    elif has_word(casefolded, _LARGE_BOAT_WORDS):
        update["boat_size"] = "large"
    return update


_CLARIFY_MESSAGES: dict[ClarifyReason, str] = {
    ClarifyReason.GARBLED_INPUT: "I couldn't understand that — could you rephrase?",
    ClarifyReason.NO_LOCATION: "Which location is this about?",
}


def render_guardrail_message(decision: GuardrailDecision) -> str | None:
    """Fixed, deterministic wording for every non-ACCEPT action; returns None for ACCEPT_LOCATION_ONLY/ACCEPT_WEATHER_FULL, which continue into the pipeline instead."""
    if decision.action == GuardrailAction.REJECT_OFF_TOPIC:
        return ("I can only answer questions about weather, marine/fishing conditions, "
                "mountain weather, weather-driven disaster risk, travel planning, or location.")
    if decision.action == GuardrailAction.CLARIFY:
        return _CLARIFY_MESSAGES.get(decision.clarify_reason or ClarifyReason.GARBLED_INPUT,
                                     _CLARIFY_MESSAGES[ClarifyReason.GARBLED_INPUT])
    if decision.action == GuardrailAction.VERIFY:
        return f"Did you mean {decision.verify_candidate}? Reply to confirm or rephrase."
    if decision.action == GuardrailAction.UNSUPPORTED_TOPIC:
        return (f"I don't have data for {decision.unsupported_topic}. I can help with "
                "weather-driven risks (cyclone, flood, storm, heat-wave warnings), "
                "marine/fishing conditions, mountain weather, and travel/route planning.")
    return None
