"""Guardrail decision funnel: classify a raw query into one strict action, in one LLM call.

Replaces a bare is_weather_related boolean with a real dispatch key (GuardrailAction) so
"accept, but only the location resolver is needed" is a decision the pipeline can act on,
not metadata nobody reads. The LLM applies a fixed decision-tree checklist (see
_SYSTEM_PROMPT) rather than judging on its own; the user-facing text for every
non-ACCEPT action is rendered by render_guardrail_message from a fixed template, never
composed by the LLM.

Wired into app/main.py's _weather_request as the sole topic/dispatch gate. Degrades to a
deterministic fallback when the LLM is unavailable or returns unparseable output, for the
same reason as every other LLM call in this app: an outage must never take the API down.
"""
from __future__ import annotations

import json
import logging
import re

from app.llm.client import small_llm
from app.orchestrator.retrieval_planner import has_word
from app.schemas.query import ClarifyReason, GuardrailAction, GuardrailDecision
from app.services.guardrail import TOPIC_WORDS, check_question
from app.services.location_resolver.normalize import extract_place_phrase

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a strict query classifier, not a conversational assistant.

In scope: weather (current/forecast/climate), marine and fishing conditions, mountain \
and trek weather, weather-driven disaster risk (cyclone, flood, storm, heat wave), \
travel and route planning around weather, and place identity/coordinates.

Given raw user text — which may contain typos, speech-to-text errors, or Hinglish/mixed-\
language phrasing — apply these rules IN ORDER and stop at the first match. Do not use \
judgment beyond what each rule states.

1. The text has no discernible topic at all (gibberish, keyboard mash, empty of meaning)
   -> action="clarify", clarify_reason="garbled_input"
2. The text asks about a natural-disaster type this system has NO data for — earthquake, \
tsunami, wildfire/fire, landslide, volcanic activity, drought — as opposed to the \
in-scope disaster types above
   -> action="unsupported_topic", unsupported_topic="<hazard name, e.g. earthquake>"
3. The text is clearly about something outside the in-scope list above (sports, cooking, \
coding, general chat, politics, etc.)
   -> action="reject_off_topic"
4. The text is in-scope-shaped, but no place name can be identified at all
   -> action="clarify", clarify_reason="no_location"
5. A place name can be identified, but you are not confident it is the exact place meant \
(typo, ambiguous short name, colloquial spelling)
   -> action="verify", verify_candidate="<your best-guess corrected place name>"
6. The text asks ONLY for a place's identity or coordinates -- no weather variable, no \
forecast or time question
   -> action="accept_location_only", location="<place>"
7. Otherwise (in-scope, place identified with confidence)
   -> action="accept_weather_full", location="<place>", time="<time phrase if any, else null>"

Hinglish/Romanized Hindi is common: "kal" = tomorrow, "parso" = day after tomorrow, \
"barish"/"baarish" = rain, "mausam" = weather. Location and time are always separate \
fields even when adjacent: "Rajkot on 2026-08-01" -> location="Rajkot", time="2026-08-01".

Output ONLY a single valid JSON object, no markdown fences, no commentary:
{"action": "accept_location_only"|"accept_weather_full"|"reject_off_topic"|"clarify"|\
"verify"|"unsupported_topic", "location": string|null, "time": string|null, \
"verify_candidate": string|null, "clarify_reason": "garbled_input"|"no_location"|null, \
"unsupported_topic": string|null, "confidence": number}
"""

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_VALID_ACTIONS = {member.value for member in GuardrailAction}
_VALID_CLARIFY_REASONS = {member.value for member in ClarifyReason}

# Deterministic-fallback-only signal: "coordinates of X" phrasing without any weather
# word alongside it. Kept separate from guardrail.TOPIC_WORDS so a question that
# mentions both ("weather and coordinates of Pune") still reads as a weather request.
_LOCATION_ONLY_WORDS = (
    "coordinates", "coordinate", "latitude", "longitude", "geocode", "pincode",
    "pin code",
)

# extract_place_phrase's patterns are tuned for weather phrasing ("weather in X"); a
# location-only ask ("coordinates of X") needs its own narrow lead pattern instead of
# broadening that shared regex, which free words like "of" would make trigger-happy for
# every other caller.
_LOCATION_ONLY_LEAD = re.compile(
    r"\b(?:coordinates?|latitude|longitude|geocode|pincode|pin\s*code)s?\s+(?:of|for)\s+",
    re.IGNORECASE,
)

# Disaster types with no backing data source in this app — checked before the normal
# topic gate so these get an honest UNSUPPORTED_TOPIC instead of being silently answered
# with irrelevant generic weather data (or bluntly rejected as if off-topic, which they
# aren't). Distinct from guardrail.TOPIC_WORDS' cyclone/flood/storm, which ARE backed by
# CAP and stay on the normal accept path.
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
        )
    except (TypeError, ValueError):
        logger.warning("query_guardrail.invalid_fields", extra={"raw_length": len(llm_text)})
        return None


def _deterministic_fallback(raw_text: str) -> GuardrailDecision:
    """Used when the LLM is unavailable or returns unparseable output.

    Deliberately never returns CLARIFY(garbled_input) or VERIFY — telling "garbled" apart
    from "off-topic", or judging a name ambiguous, needs real language understanding, not
    a keyword match. CLARIFY(no_location) and UNSUPPORTED_TOPIC are the two exceptions:
    both are still binary keyword checks, not a graded read.
    """
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
    return GuardrailDecision(original_text=text, action=action, location=location,
                             confidence=1.0, extraction_source="deterministic_fallback")


async def run_guardrail(raw_text: str) -> GuardrailDecision:
    text = (raw_text or "").strip()
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
    return parsed


def resolve_confirmed_location(original_text: str, confirmed_location: str) -> GuardrailDecision:
    """A VERIFY candidate the user just confirmed on the next turn. The original VERIFY
    classification stopped at rule 5 without ever deciding location-only vs weather-full
    (rules 6/7) — same branch _deterministic_fallback applies, just with the location
    already known instead of re-extracted."""
    action = (GuardrailAction.ACCEPT_LOCATION_ONLY if _is_location_only_phrasing(original_text.casefold())
             else GuardrailAction.ACCEPT_WEATHER_FULL)
    return GuardrailDecision(original_text=original_text, action=action, location=confirmed_location,
                             confidence=1.0, extraction_source="confirmed")


_CLARIFY_MESSAGES: dict[ClarifyReason, str] = {
    ClarifyReason.GARBLED_INPUT: "I couldn't understand that — could you rephrase?",
    ClarifyReason.NO_LOCATION: "Which location is this about?",
}


def render_guardrail_message(decision: GuardrailDecision) -> str | None:
    """The fixed, deterministic wording for every non-ACCEPT action. Returns None for
    ACCEPT_LOCATION_ONLY/ACCEPT_WEATHER_FULL — those continue into the pipeline instead
    of returning a message here."""
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
