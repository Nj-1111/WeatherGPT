"""Guardrail decision funnel: one LLM call classifies a raw query into one strict action (GuardrailAction, replacing a bare is_weather_related boolean) via a fixed decision-tree checklist (_SYSTEM_PROMPT), never the LLM's own judgment — non-ACCEPT user-facing text always comes from render_guardrail_message's fixed templates, never LLM prose. Wired into app/main.py's _weather_request as the sole topic/dispatch gate; degrades to a deterministic fallback on any LLM outage or unparseable output."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.llm.client import small_llm
from app.orchestrator.retrieval_planner import ALL_CAPABILITIES, GUIDANCE_FLAGS, has_word
from app.prompts.loader import load_prompt
from app.rade.v2 import CLARIFYING_FIELDS
from app.schemas.query import ClarifyReason, GuardrailAction, GuardrailDecision
from app.services.cache import TTLCache
from app.services.input_pipeline.normalize import extract_place_phrase
from app.services.input_pipeline.safety import TOPIC_WORDS, check_question

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = load_prompt("guardrail")

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_VALID_ACTIONS = {member.value for member in GuardrailAction}
_VALID_CLARIFY_REASONS = {member.value for member in ClarifyReason}
_VALID_CAPABILITIES = set(ALL_CAPABILITIES)
_VALID_PAIRING_MODES = {"locations_x_shared_time", "times_x_shared_location", "full_cross_product"}
_VALID_CAPABILITY_CONFIDENCE = {"low", "medium", "high"}
_APPARENT_CONTEXT_MAX_LENGTH = 200
_LOCATION_MAX_LENGTH = 256
_TIME_PHRASE_MAX_LENGTH = 128


def _sanitize_capabilities(raw: object) -> list[str] | None:
    """Filters to the closed vocabulary — drops anything the LLM invented rather than
    rejecting the whole decision. Returns None (invalid, triggers a retry) only for the one
    case that's genuinely malformed: guidance flags with no real data capability alongside
    them, which carry no data and are meaningless alone."""
    if not isinstance(raw, list):
        return []
    caps = list(dict.fromkeys(c for c in raw if isinstance(c, str) and c in _VALID_CAPABILITIES))
    if caps and all(c in GUIDANCE_FLAGS for c in caps):
        return None
    return caps


def _sanitize_confidence_per_capability(raw: object, capabilities: list[str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in capabilities and v in _VALID_CAPABILITY_CONFIDENCE}


def _sanitize_string_list(raw: object, max_length: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip()[:max_length] for item in raw if isinstance(item, str) and str(item).strip()]

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
    # action is load-bearing (the real dispatch key) — invalid here means the whole decision
    # is untrustworthy, so this is the one field that still discards everything and retries.
    action = data.get("action")
    if action not in _VALID_ACTIONS:
        return None

    # Every field below this point degrades independently: invalid/missing input defaults
    # safely rather than losing an otherwise-good `action` classification to a retry.
    clarify_reason = data.get("clarify_reason")
    if clarify_reason not in _VALID_CLARIFY_REASONS:
        clarify_reason = None
    apparent_context = data.get("apparent_context") or None
    if apparent_context is not None:
        apparent_context = str(apparent_context).strip()[:_APPARENT_CONTEXT_MAX_LENGTH] or None
    pairing_mode = data.get("pairing_mode")
    if pairing_mode not in _VALID_PAIRING_MODES:
        pairing_mode = "locations_x_shared_time"
    capabilities = _sanitize_capabilities(data.get("capabilities"))
    if capabilities is None:
        # Guidance flags with no real data capability — genuinely malformed, not just
        # incomplete; the one non-`action` field still worth a retry over.
        return None
    capabilities_version = data.get("capabilities_version")
    if not isinstance(capabilities_version, str) or not capabilities_version:
        capabilities_version = "v1"

    reasoning = data.get("reasoning")
    if reasoning:
        logger.info("query_guardrail.reasoning", extra={"reasoning": str(reasoning)[:200]})

    try:
        return GuardrailDecision(
            original_text=raw_text,
            action=GuardrailAction(action),
            locations=_sanitize_string_list(data.get("locations"), _LOCATION_MAX_LENGTH),
            time_phrases=_sanitize_string_list(data.get("time_phrases"), _TIME_PHRASE_MAX_LENGTH),
            pairing_mode=pairing_mode,
            verify_candidate=data.get("verify_candidate") or None,
            clarify_reason=ClarifyReason(clarify_reason) if clarify_reason else None,
            unsupported_topic=data.get("unsupported_topic") or None,
            confidence=float(data.get("confidence", 0.0)),
            extraction_source="llm",
            detected_lang=(data.get("detected_lang") or "en").strip() or "en",
            apparent_context=apparent_context,
            capabilities=capabilities,
            capabilities_version=capabilities_version,
            confidence_per_capability=_sanitize_confidence_per_capability(
                data.get("confidence_per_capability"), capabilities),
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
    locations = [location] if location else []
    if _is_location_only_phrasing(casefolded):
        location = location or _extract_location_only_phrase(text)
        if location:
            return GuardrailDecision(original_text=text, action=GuardrailAction.ACCEPT_LOCATION_ONLY,
                                     locations=[location], confidence=1.0, extraction_source="deterministic_fallback")
        return GuardrailDecision(original_text=text, action=GuardrailAction.CLARIFY,
                                 clarify_reason=ClarifyReason.NO_LOCATION, confidence=1.0,
                                 extraction_source="deterministic_fallback")
    try:
        check_question(text)
        is_weather_related = True
    except Exception:
        is_weather_related = False
    action = GuardrailAction.ACCEPT_WEATHER_FULL if is_weather_related else GuardrailAction.REJECT_OFF_TOPIC
    # capabilities stays empty here deliberately: retrieval_planner's own keyword matching
    # already decides what to fetch from the raw question text independent of capabilities
    # (capabilities only widens coverage beyond what keywords catch, which the deterministic
    # path — itself keyword-only — cannot do any better on anyway).
    return GuardrailDecision(original_text=text, action=action, locations=locations,
                             confidence=1.0, extraction_source="deterministic_fallback")


# One LLM call per distinct question, not per request. A fallback decision is never stored — it's a degraded read and must not outlive the outage that produced it.
_decision_cache = TTLCache(settings.guardrail_cache_max_entries)


_LLM_KWARGS = {
    "temperature": 0.0,
    # A tightly-budgeted structured-output call — reasoning depth isn't needed for a
    # fixed decision-tree classification, and a reasoning-capable model burns its hidden
    # reasoning against this same max_tokens budget (see llm/client.py's note). 280 was
    # enough for the primary endpoint but confirmed live to truncate the fallback
    # endpoint's response mid-JSON (finish_reason "length" at ~260 tokens); 500 gave
    # headroom on both. Raised to 1100 (2026-09-08) for the 3rd chain endpoint
    # (nvidia/nemotron-3-super-120b-a12b:free via OpenRouter): its reasoning trace lands in
    # a separate `reasoning` field (reasoning_format: hidden respected), but still spends
    # from this same budget, and a history-bearing rule-0 continuation prompt needs >500 to
    # avoid truncating before the JSON answer. Harmless headroom for Groq/Gemini too.
    "max_tokens": 1100,
    "reasoning_effort": "low",
}


def _format_history(history: list[dict[str, Any]] | None) -> str:
    """Renders the last few turns as a user-message preamble (never the system prompt, so the decision-tree rules stay history-independent and the LLM call structure unchanged). Empty for the common stateless case."""
    if not history:
        return ""
    lines = [f"{turn.get('role', 'user')}: {turn.get('text', '')}" for turn in history if turn.get("text")]
    return "Recent conversation:\n" + "\n".join(lines) + "\n\n" if lines else ""


async def run_guardrail(raw_text: str, history: list[dict[str, Any]] | None = None) -> GuardrailDecision:
    text = (raw_text or "").strip()
    history_block = _format_history(history)
    # Keyed on history too: the same question text can mean different things depending on
    # conversation context, so a stateless cache-by-text-alone would serve a stale/wrong
    # decision to a different conversation. Turns with no history hash the same as before,
    # so the common case (repeat, stateless queries) keeps its existing cache benefit.
    key = f"{text.casefold()}::{history_block}"
    cached = await _decision_cache.get(key)
    if cached is not None:
        return cached.value.model_copy(update={"original_text": text})
    user_content = f"{history_block}Current message: {text}" if history_block else text
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_content}]
    result = await small_llm(messages, **_LLM_KWARGS)
    if not result.available or not result.text:
        logger.info("query_guardrail.llm_unavailable", extra={"error": result.error})
        return _deterministic_fallback(text)
    parsed = _parse(text, result.text)
    if parsed is None:
        # One bounded retry with the failure fed back — not a loop — before falling back
        # deterministically. Only reached on the already-rare invalid-`action`/guidance-flag
        # cases; every other field degrades independently inside _parse() without retrying.
        retry_messages = [
            *messages,
            {"role": "assistant", "content": result.text},
            {"role": "user", "content": "That was not valid JSON matching the required schema "
                                        "and rules. Reply again with ONLY the corrected JSON object."},
        ]
        retry_result = await small_llm(retry_messages, **_LLM_KWARGS)
        parsed = (_parse(text, retry_result.text)
                 if retry_result.available and retry_result.text else None)
        if parsed is None:
            return _deterministic_fallback(text)
    await _decision_cache.put(key, parsed, settings.guardrail_cache_ttl_seconds)
    return parsed


def resolve_confirmed_location(original_text: str, confirmed_location: str) -> GuardrailDecision:
    """A VERIFY candidate the user just confirmed next turn — the original classification stopped at rule 5 without deciding location-only vs weather-full, so the same branch _deterministic_fallback applies, just with the location already known."""
    action = (GuardrailAction.ACCEPT_LOCATION_ONLY if _is_location_only_phrasing(original_text.casefold())
             else GuardrailAction.ACCEPT_WEATHER_FULL)
    return GuardrailDecision(original_text=original_text, action=action, locations=[confirmed_location],
                             confidence=1.0, extraction_source="confirmed")


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10}


def _parse_followup_answer(text: str, domain: str) -> dict:
    """A one-sentence answer to a closed clarifying question — a fixed keyword parse, same
    invariant as the rest of this module. Declarative over rade.v2.CLARIFYING_FIELDS instead
    of one hardcoded parser per domain: a new domain's fields need a dict entry there, not a
    new function here. Unrecognized text yields an empty update rather than guessing."""
    casefolded = text.casefold()
    update: dict[str, object] = {}
    for field, spec in CLARIFYING_FIELDS.get(domain, {}).items():
        if spec["kind"] == "solo_or_count":
            if has_word(casefolded, spec["solo_words"]):
                update[field] = 1
            else:
                digit_match = re.search(r"\b(\d+)\b", casefolded)
                if digit_match:
                    update[field] = int(digit_match.group(1))
                else:
                    for word, value in _NUMBER_WORDS.items():
                        if has_word(casefolded, (word,)):
                            update[field] = value
                            break
        elif spec["kind"] == "enum":
            for value, words in spec["values"].items():
                if has_word(casefolded, words):
                    update[field] = value
                    break
    return update


_CLARIFY_MESSAGES: dict[ClarifyReason, str] = {
    ClarifyReason.GARBLED_INPUT: "I couldn't understand that — could you rephrase?",
    ClarifyReason.NO_LOCATION: "Which location is this about?",
}

# Static translations only, no LLM call — these 4 messages never change, so a table is
# cheaper and faster than a per-request translation call. Extend per language as needed;
# an unlisted detected_lang falls back to English rather than failing.
_REJECT_OFF_TOPIC_HI = ("मैं केवल मौसम, समुद्री/मछली पकड़ने की स्थिति, पहाड़ी मौसम, "
                        "मौसम-जनित आपदा जोखिम, यात्रा योजना, या स्थान से जुड़े सवालों के जवाब दे सकता हूँ।")
_CLARIFY_MESSAGES_HI: dict[ClarifyReason, str] = {
    ClarifyReason.GARBLED_INPUT: "मुझे समझ नहीं आया — क्या आप फिर से बता सकते हैं?",
    ClarifyReason.NO_LOCATION: "यह किस जगह के बारे में है?",
}
_TRANSLATIONS: dict[str, dict[str, Any]] = {
    "hi": {"reject_off_topic": _REJECT_OFF_TOPIC_HI, "clarify": _CLARIFY_MESSAGES_HI},
}


def render_guardrail_message(decision: GuardrailDecision) -> str | None:
    """Fixed, deterministic wording for every non-ACCEPT action; returns None for ACCEPT_LOCATION_ONLY/ACCEPT_WEATHER_FULL, which continue into the pipeline instead. Templates are translated from a static table when detected_lang has one (see _TRANSLATIONS) — never via an LLM call, since the wording never changes."""
    lang = _TRANSLATIONS.get(decision.detected_lang, {})
    if decision.action == GuardrailAction.REJECT_OFF_TOPIC:
        return lang.get("reject_off_topic",
                        "I can only answer questions about weather, marine/fishing conditions, "
                        "mountain weather, weather-driven disaster risk, travel planning, or location.")
    if decision.action == GuardrailAction.CLARIFY:
        messages = lang.get("clarify", _CLARIFY_MESSAGES)
        return messages.get(decision.clarify_reason or ClarifyReason.GARBLED_INPUT,
                            _CLARIFY_MESSAGES[ClarifyReason.GARBLED_INPUT])
    if decision.action == GuardrailAction.VERIFY:
        return f"Did you mean {decision.verify_candidate}? Reply to confirm or rephrase."
    if decision.action == GuardrailAction.UNSUPPORTED_TOPIC:
        return (f"I don't have data for {decision.unsupported_topic}. I can help with "
                "weather-driven risks (cyclone, flood, storm, heat-wave warnings), "
                "marine/fishing conditions, mountain weather, and travel/route planning.")
    return None
