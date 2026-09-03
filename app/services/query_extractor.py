"""Unified LLM extraction funnel: location/time/intent/topic in one call.

Mandatory on the request path (per explicit product decision — see CLAUDE.md's
"deterministic first" pattern, which this intentionally overrides for query
understanding). Because that makes every request depend on an external LLM host,
this degrades to the existing deterministic tools (guardrail.check_question +
location_resolver.normalize.extract_place_phrase) rather than hard-failing when
the LLM is unavailable or returns unparseable output — the alternative is that a
Groq outage takes the whole API down.
"""
from __future__ import annotations

import json
import logging
import re

from app.llm.client import small_llm
from app.schemas.query import NormalizedQuery, QueryIntent
from app.services import guardrail
from app.services.location_resolver.normalize import extract_place_phrase

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a strict information-extraction function, not a conversational assistant.

Given raw user text — which may contain typos, speech-to-text transcription errors, or \
Hinglish/mixed-language phrasing — extract:

1. normalized_location: the city, town, or place name being asked about, corrected for \
spelling/transcription errors, in standard English (e.g. "Bangalore" not "banagalore" or \
"banglr"). Do not include state names, dates, or time words. null if no place is named.
2. normalized_time: the temporal expression for when the weather is being asked about \
(e.g. "tomorrow", "kal", "2026-08-01", "this evening"), as the shortest phrase that \
captures it, or an ISO date if an absolute date is given. null if no time is mentioned.
3. intent: one of "current", "forecast", "alerts", "historical", "unknown".
4. is_weather_related: true only if the text asks about weather, climate, or a \
weather-dependent decision (spraying, travel, irrigation, fishing, etc). General-knowledge \
or off-topic questions must be false.
5. confidence_score: your confidence (0.0-1.0) in the location and intent extraction. Use a \
low score for garbled, ambiguous, or very short input.

Rules:
- Hinglish/Romanized Hindi is common: "kal" = tomorrow, "parso"/"parsO" = day after \
tomorrow, "barish"/"baarish" = rain, "mausam" = weather.
- Location and time are ALWAYS separate fields, even when adjacent in the text: \
"Rajkot on 2026-08-01" -> normalized_location="Rajkot", normalized_time="2026-08-01".
- Output ONLY a single valid JSON object matching this shape, no markdown fences, no \
commentary:
{"normalized_location": string|null, "normalized_time": string|null, "intent": \
"current"|"forecast"|"alerts"|"historical"|"unknown", "is_weather_related": boolean, \
"confidence_score": number}
"""

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_VALID_INTENTS = {member.value for member in QueryIntent}


def _parse(raw_text: str, llm_text: str) -> NormalizedQuery | None:
    cleaned = _JSON_FENCE.sub("", llm_text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("query_extractor.unparseable_output", extra={"raw_length": len(llm_text)})
        return None
    intent = data.get("intent")
    if intent not in _VALID_INTENTS:
        intent = QueryIntent.UNKNOWN.value
    try:
        return NormalizedQuery(
            original_text=raw_text,
            normalized_location=data.get("normalized_location") or None,
            normalized_time=data.get("normalized_time") or None,
            intent=QueryIntent(intent),
            is_weather_related=bool(data.get("is_weather_related", False)),
            confidence_score=float(data.get("confidence_score", 0.0)),
            # The LLM call succeeded and returned a well-formed read — this is a "llm"
            # extraction regardless of the confidence value it reported. Whether that
            # confidence clears query_understanding_confidence_threshold is a separate,
            # later decision (main._understand_query) about whether to proceed with the
            # request, not about which code path produced the read.
            extraction_source="llm",
        )
    except (TypeError, ValueError):
        logger.warning("query_extractor.invalid_fields", extra={"raw_length": len(llm_text)})
        return None


def _deterministic_fallback(raw_text: str) -> NormalizedQuery:
    """Used when the LLM is unavailable or returns unparseable output.

    Reuses the existing deterministic gate/extractor rather than duplicating logic —
    same topic words, same place-phrase regex the rest of the app relies on.
    """
    try:
        guardrail.check_question(raw_text)
        is_weather_related = True
    except Exception:
        is_weather_related = False
    return NormalizedQuery(
        original_text=raw_text,
        normalized_location=extract_place_phrase(raw_text),
        normalized_time=None,
        intent=QueryIntent.UNKNOWN,
        is_weather_related=is_weather_related,
        # 1.0/0.0, not something in between: the deterministic gate is a keyword
        # match, a binary decision, not a graded read. A mid-range score here
        # would fall below query_understanding_confidence_threshold and reject
        # every legitimate question whenever the LLM is merely unconfigured
        # (e.g. no key set) rather than genuinely wrong about the input.
        confidence_score=1.0 if is_weather_related else 0.0,
        extraction_source="deterministic_fallback",
    )


async def extract_and_normalize(raw_text: str) -> NormalizedQuery:
    text = (raw_text or "").strip()
    result = await small_llm(
        [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": text}],
        temperature=0.0,
        max_tokens=200,
    )
    if not result.available or not result.text:
        logger.info("query_extractor.llm_unavailable", extra={"error": result.error})
        return _deterministic_fallback(text)
    parsed = _parse(text, result.text)
    if parsed is None:
        return _deterministic_fallback(text)
    return parsed
