"""Real multi-agent orchestrator — 8 agents, structured AgentResult, 4-model queue."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from app.agents.base import AgentResult, Claim
from app.agents.verification import check_prose_grounding, verify_claim
from app.config import settings
from app.llm.client import DETERMINISTIC, Tier, big_llm, is_configured, small_llm
from app.prompts.loader import load_prompt
from app.schemas.ceo import CanonicalEvidenceObject
from app.services.input_pipeline.context_retriever import context_retriever

logger = logging.getLogger(__name__)

# Claims whose value is a decision or user-context fact, not a measurement — no evidence record to recompute them from.
_NOT_MEASURED = {"op": "none"}


async def run_context_agent(user_context: dict[str, Any], question: str) -> AgentResult:
    start=time.time()
    claims=[]
    if user_context:
        for k,v in list(user_context.items())[:5]:
            # Context is not weather evidence; its provenance is the user-context repository.
            claims.append(Claim(claim=f"context.{k}", value=v, evidence_ids=[], confidence=0.9, extra={"derivation": _NOT_MEASURED}))
    return AgentResult(agent_name="context", claims=claims, confidence=0.9, execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC, status="success")

async def run_forecast_agent(wio) -> AgentResult:
    start=time.time()
    claims=[]
    # Claims read off the fused panels so they cite exactly the numbers shown (slicing unranked CEOs cited unrelated evidence); each carries its derivation so the reviewer can recompute it instead of trusting the number.
    for name, panel, value_key, derivation in (
        ("precipitation_amount", wio.weather.rain, "value_mm",
         lambda p: {"op": "sum", "variable": p.get("variable"), "unit": p.get("unit"),
                    "accumulation_window_hours": p.get("accumulation_hours")}),
        ("temperature_max", wio.weather.temperature, "max",
         lambda p: {"op": "max", "variable": p.get("variable"), "unit": p.get("unit")}),
        ("wind_speed", wio.weather.wind, "value_kmh",
         lambda p: {"op": "max", "variable": p.get("variable"), "unit": p.get("unit")}),
    ):
        if panel:
            derived = derivation(panel)
            claims.append(Claim(claim=name, value=panel.get(value_key), unit=derived.get("unit"),
                                evidence_ids=list(panel.get("evidence_ids", [])), confidence=0.85,
                                extra={"derivation": derived}))
    evidence_ids=[eid for c in claims for eid in c.evidence_ids]
    return AgentResult(agent_name="forecast", claims=claims, evidence_ids=evidence_ids, confidence=0.85, execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC, status="success" if claims else "partial")

async def run_warning_agent(ceos: list[CanonicalEvidenceObject]) -> AgentResult:
    start=time.time()
    warnings=[c for c in ceos if c.evidence_class=="warning"]
    claims=[]
    for w in warnings:
        claims.append(Claim(claim="warning", value=w.warning_severity, unit="severity", evidence_ids=[w.evidence_id], confidence=0.95,
                            extra={"derivation": {"op": "identity", "field": "warning_severity", "unit": "severity"}}))
    return AgentResult(agent_name="warning", claims=claims, evidence_ids=[w.evidence_id for w in warnings], confidence=0.95, warnings=[w.raw_value for w in warnings if w.raw_value], execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC, status="success")

async def run_historical_agent(ceos: list[CanonicalEvidenceObject]) -> AgentResult:
    start=time.time()
    hist=[c for c in ceos if c.evidence_class in ("reanalysis","climate","observation")]
    claims=[]
    for h in hist[:2]:
        claims.append(Claim(claim=h.variable, value=h.value, unit=h.unit, evidence_ids=[h.evidence_id], confidence=0.7,
                            extra={"derivation": {"op": "identity", "field": "value", "unit": h.unit}}))
    return AgentResult(agent_name="historical", claims=claims, evidence_ids=[c.evidence_id for c in hist[:2]], confidence=0.7, execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC, status="success" if hist else "partial")

async def run_observation_agent(ceos: list[CanonicalEvidenceObject]) -> AgentResult:
    start=time.time()
    obs=[c for c in ceos if c.evidence_class=="observation"]
    claims=[Claim(claim=c.variable, value=c.value, unit=c.unit, evidence_ids=[c.evidence_id], confidence=0.8,
                  extra={"derivation": {"op": "identity", "field": "value", "unit": c.unit}}) for c in obs[:2]]
    return AgentResult(agent_name="observation", claims=claims, evidence_ids=[c.evidence_id for c in obs[:2]], confidence=0.8, execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC, status="success" if obs else "partial")

async def run_decision_agent(result, evidence_ids: list[str]) -> AgentResult:
    start=time.time()
    if result is None:
        return AgentResult(agent_name="decision", claims=[], confidence=0.5, status="partial", execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC)
    claims=[Claim(claim="recommended_action", value=result.recommended_action, evidence_ids=evidence_ids, confidence=result.confidence, extra={"derivation": _NOT_MEASURED})]
    for scenario in result.scenarios[:2]:
        claims.append(Claim(claim=f"scenario_{scenario.name}", value=scenario.probability, unit="probability", evidence_ids=evidence_ids, confidence=result.confidence, extra={"derivation": _NOT_MEASURED}))
    return AgentResult(agent_name="decision", claims=claims, confidence=result.confidence, execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC, status="success")

async def run_reviewer_agent(agent_results: list[AgentResult], wio, ceos: list[CanonicalEvidenceObject], comparisons=None) -> AgentResult:
    """The anti-hallucination gate: citing real evidence is necessary but not sufficient — the cited value is recomputed and must match; free-text claims are instead checked for quantities the pipeline never produced."""
    start=time.time()
    by_id={c.evidence_id: c for c in ceos}
    errors: list[str]=[]
    warnings: list[str]=[]
    for r in agent_results:
        for c in r.claims:
            unknown=[eid for eid in c.evidence_ids if not eid or eid not in by_id]
            for eid in unknown:
                errors.append(f"{r.agent_name} claim {c.claim} references unknown evidence {eid!r}")
            # An explanation restates panels rather than citing its own record; the grounding check below is the stronger constraint on what it may say.
            if not c.evidence_ids and not c.claim.startswith("context.") and c.claim != "explanation":
                errors.append(f"{r.agent_name} claim {c.claim} has no evidence")
                continue
            if unknown:
                continue
            if c.claim == "explanation":
                ungrounded=check_prose_grounding(str(c.value or ""), wio, comparisons)
                if not ungrounded:
                    continue
                detail=f"explanation states {', '.join(ungrounded)}, which no evidence supports"
                if settings.reviewer_prose_failure_mode == "fail":
                    errors.append(detail)
                else:
                    # Suppressed rather than fatal: the guarantee is no fabricated number reaches the user, not that a third-party model can 503 the API.
                    warnings.append(f"{detail}; explanation suppressed")
                    r.claims=[claim for claim in r.claims if claim is not c]
                    r.errors.append(detail)
                    r.status="partial"
                    logger.warning("reviewer.explanation_suppressed", extra={"ungrounded": ungrounded})
                continue
            claim_errors, claim_warnings=verify_claim(c, [by_id[eid] for eid in c.evidence_ids])
            errors.extend(f"{r.agent_name} {message}" for message in claim_errors)
            warnings.extend(f"{r.agent_name} {message}" for message in claim_warnings)
    status="success" if not errors else "partial"
    return AgentResult(agent_name="reviewer", claims=[], confidence=0.9, errors=errors, warnings=warnings, status=status, execution_time_ms=int((time.time()-start)*1000), model=DETERMINISTIC)

_EXPLANATION_SYSTEM = load_prompt("explanation")

# Deterministic lead-in check only — never the raw text itself reaches the fact sheet (see
# the comment below); a derived boolean carries no injection surface the way free text would.
_GREETING_LEAD = re.compile(r"^\s*(?:hi+|hello+|hey+|hiya|yo|namaste)\b[,!.\s]*", re.IGNORECASE)


def _opens_with_greeting(text: str) -> bool:
    return bool(_GREETING_LEAD.match(text))


def _is_romanized(text: str) -> bool:
    """True when every alphabetic character is Latin-script — i.e. the text is written in
    the Latin alphabet even though its detected language's native script isn't (Hinglish,
    Banglish, romanized Tamil, ...). {lang} alone tells the explanation model which
    *language* to answer in, not which *script* — without this, "aj ke brishti hobe
    kolkataye" (Bengali written in Latin letters) got answered in full Bengali script,
    which the user never wrote and likely can't read as easily. Codepoint check only; the
    raw text itself never reaches the fact sheet."""
    return bool(text) and all(ord(ch) < 0x0370 for ch in text if ch.isalpha())


def _comparison_lines(comparisons) -> list[str]:
    """Each additional (location, time) pair's fused panels, so a multi-location question is
    answered about every location instead of only the primary one. Same shape as the primary
    facts below — values only, still never the raw question."""
    lines: list[str] = []
    for other in comparisons or []:
        name = (other.query.resolved_location.get("normalized_name")
                or other.query.resolved_location.get("raw", "unknown"))
        facts = [f"summary {other.weather.summary or 'no compatible evidence'}"]
        rain = other.weather.rain or {}
        if rain.get("value_mm") is not None:
            facts.append(f"precipitation {rain['value_mm']} mm over the window"
                         + (f", peak probability {round(rain['probability'] * 100)}%"
                            if rain.get("probability") is not None else ""))
        temperature = other.weather.temperature or {}
        if temperature.get("min") is not None:
            facts.append(f"temperature {temperature['min']} to {temperature['max']} {temperature.get('unit')}")
        wind = other.weather.wind or {}
        if wind.get("value_kmh") is not None:
            facts.append(f"wind up to {wind['value_kmh']} km/h")
        if other.official_warning.active:
            facts.append(f"official {other.official_warning.severity} warning: {other.official_warning.event}")
        lines.append(f"- {name}: " + "; ".join(facts))
    return lines


def _fact_sheet(wio, decision, context_docs: list[str] | None = None, comparisons=None) -> str:
    # The raw question is deliberately excluded — it's the one caller-controlled string reaching the model, and check_prose_grounding only constrains numbers, not instructions; query.intent gives the same orientation from a closed, pipeline-derived set. Whether it opened with a greeting is sent as a derived boolean instead, for behavior_rules.md's greeting-acknowledgment rule.
    lines=[f"Intent: {wio.query.intent or 'general weather'}",
           f"Location: {wio.query.resolved_location.get('normalized_name') or wio.query.resolved_location.get('raw', 'unknown')}",
           f"Window: {wio.query.valid_from} to {wio.query.valid_to}",
           f"Assessment: {wio.weather.summary or 'no compatible evidence'}"]
    if wio.query.apparent_context:
        lines.append(f"Apparent context: {wio.query.apparent_context}")
    if _opens_with_greeting(wio.query.raw_text):
        lines.append("User opened with a greeting.")
    if wio.query.lang != "en" and _is_romanized(wio.query.raw_text):
        lines.append(f"The user wrote in {wio.query.lang} but in the Latin alphabet "
                     "(romanized), not the native script. Reply the same way — Latin "
                     "alphabet, not native script.")
    rain=wio.weather.rain or {}
    if rain:
        lines.append(f"Precipitation: {rain.get('value_mm')} mm total over the window"
                     + (f", peak hourly {rain.get('peak_hourly_mm')} mm" if rain.get("peak_hourly_mm") is not None else "")
                     + (f", peak probability {round(rain['probability'] * 100)}%" if rain.get("probability") is not None else "")
                     + f" (source {rain.get('source')})")
    temperature=wio.weather.temperature or {}
    if temperature:
        lines.append(f"Temperature: {temperature.get('min')} to {temperature.get('max')} {temperature.get('unit')} (source {temperature.get('source')})")
    wind=wio.weather.wind or {}
    if wind:
        lines.append(f"Wind: maximum {wind.get('value_kmh')} km/h (source {wind.get('source')})")
    marine=wio.weather.marine or {}
    if marine:
        lines.append(
            f"Marine conditions: wave height up to {marine.get('wave_height_m', 'unknown')} m, "
            f"current velocity up to {marine.get('current_velocity_kmh', 'unknown')} km/h, "
            f"sea surface temperature {marine.get('sea_surface_temp_c', 'unknown')} C "
            f"(source {marine.get('source')})")
    if wio.official_warning.active:
        lines.append(f"Official warning: {wio.official_warning.severity} {wio.official_warning.event} from {wio.official_warning.authority}")
    lines.append(f"Source agreement: {wio.agreement.status}. {wio.agreement.notes}".strip())
    if wio.disagreements:
        lines.append("Disagreements: " + "; ".join(wio.disagreements))
    if decision is not None and decision.claims:
        action=next((c for c in decision.claims if c.claim == "recommended_action"), None)
        if action is not None:
            lines.append(f"Recommended action: {action.value}")
    missing_fields=wio.query.resolved_location.get("followup_missing_fields")
    if missing_fields:
        lines.append("Unresolved factor that could change this: "
                     + " and ".join(field.replace("_", " ") for field in missing_fields) + ".")
    comparison_lines = _comparison_lines(comparisons)
    if comparison_lines:
        primary = (wio.query.resolved_location.get("normalized_name")
                   or wio.query.resolved_location.get("raw", "unknown"))
        lines.append(f"This question compares several locations. The figures above are for {primary}. "
                     "Other locations asked about:")
        lines.extend(comparison_lines)
        lines.append("Cover every location listed, not only the first.")
    if context_docs:
        lines.append("Reference material:\n" + "\n".join(context_docs))
    return "\n".join(lines)


def _panel_evidence_ids(wio) -> list[str]:
    # Marine included so a marine-only query (no rain/temperature/wind evidence) doesn't produce an empty list here, which would make run_explanation_agent silently skip exactly the queries this exists to describe.
    ids: list[str]=[]
    for panel in (wio.weather.rain, wio.weather.temperature, wio.weather.wind, wio.weather.marine):
        for eid in (panel or {}).get("evidence_ids", []):
            if eid not in ids:
                ids.append(eid)
    return ids

def _requires_big_llm(wio, decision: AgentResult | None) -> bool:
    """Deterministic complexity trigger for the big tier: fires only for a RADE decision that couldn't resolve confidently (deferred = confidence 0) or disagreeing fused sources, never for restating one panel value. `decision.claims` is checked, not just `decision is not None`, because `run_decision_agent` returns empty claims at confidence=0.5 when RADE never ran — that must not look "low confidence"."""
    if wio.disagreements:
        return True
    return (decision is not None and bool(decision.claims)
            and decision.confidence < settings.big_llm_complexity_confidence_threshold)


async def run_explanation_agent(wio, decision: AgentResult, lang: str = "en", comparisons=None) -> AgentResult:
    """The one agent that calls an LLM — it explains, never originates a number. Everything it writes passes the reviewer's grounding check; any failure (dead endpoint, exhausted chain, empty reply) degrades to the deterministic template. Prose over fused panels is a small-tier job; the big tier wakes only via `_requires_big_llm`'s trigger, never by asking the small model if it feels out of its depth, and an unconfigured big tier is a no-op fallback to small, not an error."""
    start=time.time()
    def _result(claims, status, model=DETERMINISTIC, errors=None) -> AgentResult:
        return AgentResult(agent_name="explanation", claims=claims, confidence=0.85 if claims else 0.5,
                           execution_time_ms=int((time.time()-start)*1000),
                           model=model, status=status, errors=errors or [])
    panel_ids=_panel_evidence_ids(wio)
    if not panel_ids:
        return _result([], "success")
    # Query by intent, not the raw question — the fact sheet excludes the raw question for
    # the same reason (see _fact_sheet's comment); returns [] until a real retriever is wired.
    context_docs=await context_retriever.retrieve(wio.query.intent or "", k=3)
    # One location's budget per location asked about, so a comparison isn't truncated
    # mid-city — the cap is per answer, not per place.
    max_words=settings.llm_max_words * (1 + len(comparisons or []))
    messages=[{"role": "system", "content": _EXPLANATION_SYSTEM.format(
                  lang=lang, max_words=max_words)},
              {"role": "user", "content": _fact_sheet(wio, decision, context_docs, comparisons)}]
    tier: Tier = "big" if _requires_big_llm(wio, decision) and is_configured("big") else "small"
    tier_llm = big_llm if tier == "big" else small_llm
    result=await tier_llm(messages, max_tokens=max_words * 4)
    if not result.available:
        # Never fatal — an unconfigured or unreachable model must not cost the user an answer.
        return _result([], "success" if not is_configured(tier) else "partial",
                       errors=[] if not is_configured(tier) else [f"explanation unavailable: {result.error}"])
    text=(result.text or "").strip()
    if not text:
        return _result([], "partial", result.tier, ["explanation model returned empty text"])
    return _result([Claim(claim="explanation", value=text, evidence_ids=panel_ids, confidence=0.7,
                          extra={"derivation": _NOT_MEASURED})], "success", result.tier)

async def run_all_agents(ceos: list[CanonicalEvidenceObject], wio, user_context: dict[str, Any], lang: str = "en", decision=None, comparisons=None) -> list[AgentResult]:
    # Run independent agents concurrently
    forecast_task = run_forecast_agent(wio)
    warning_task = run_warning_agent(ceos)
    hist_task = run_historical_agent(ceos)
    obs_task = run_observation_agent(ceos)
    context_task = run_context_agent(user_context, "")
    results: list[AgentResult] = list(await asyncio.gather(forecast_task, warning_task, hist_task, obs_task, context_task))
    decision_result = await run_decision_agent(decision, [c.evidence_id for c in ceos])
    results.append(decision_result)
    # Produced before review so the reviewer can check it, but stays last in the returned list — it's the only agent whose output is written by a model.
    explanation = await run_explanation_agent(wio, decision_result, lang, comparisons)
    reviewer = await run_reviewer_agent([*results, explanation], wio, ceos, comparisons)
    results.append(reviewer)
    results.append(explanation)
    return results
