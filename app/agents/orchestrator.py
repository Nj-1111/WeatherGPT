"""Real multi-agent orchestrator — 8 agents, structured AgentResult, 4-model queue."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.agents.base import AgentResult, Claim
from app.agents.verification import check_prose_grounding, verify_claim
from app.config import settings
from app.llm.client import DETERMINISTIC, Tier, big_llm, is_configured, small_llm
from app.schemas.ceo import CanonicalEvidenceObject

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

async def run_reviewer_agent(agent_results: list[AgentResult], wio, ceos: list[CanonicalEvidenceObject]) -> AgentResult:
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
                ungrounded=check_prose_grounding(str(c.value or ""), wio)
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

_EXPLANATION_SYSTEM = (
    "You explain a weather assessment that has already been computed from retrieved evidence. "
    "Use ONLY the figures in the fact sheet. Never introduce a number, quantity, date or place "
    "that is not there, never estimate, and never contradict the assessment. If a figure is "
    "absent, say it is not available. Answer in {lang}, in at most {max_words} words, as plain "
    "prose with no headings, no lists and no markdown. Tone: {tone_directive}."
)


def _fact_sheet(wio, decision) -> str:
    # The raw question is deliberately excluded — it's the one caller-controlled string reaching the model, and check_prose_grounding only constrains numbers, not instructions; query.intent gives the same orientation from a closed, pipeline-derived set.
    lines=[f"Intent: {wio.query.intent or 'general weather'}",
           f"Location: {wio.query.resolved_location.get('normalized_name') or wio.query.resolved_location.get('raw', 'unknown')}",
           f"Window: {wio.query.valid_from} to {wio.query.valid_to}",
           f"Assessment: {wio.weather.summary or 'no compatible evidence'}"]
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
    if wio.query.persona == "marine":
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


async def run_explanation_agent(wio, decision: AgentResult, lang: str = "en") -> AgentResult:
    """The one agent that calls an LLM — it explains, never originates a number. Everything it writes passes the reviewer's grounding check; any failure (dead endpoint, exhausted chain, empty reply) degrades to the deterministic template. Prose over fused panels is a small-tier job; the big tier wakes only via `_requires_big_llm`'s trigger, never by asking the small model if it feels out of its depth, and an unconfigured big tier is a no-op fallback to small, not an error."""
    start=time.time()
    def _result(claims, status, model=DETERMINISTIC, errors=None) -> AgentResult:
        return AgentResult(agent_name="explanation", claims=claims, confidence=0.85 if claims else 0.5,
                           execution_time_ms=int((time.time()-start)*1000),
                           model=model, status=status, errors=errors or [])
    panel_ids=_panel_evidence_ids(wio)
    if not panel_ids:
        return _result([], "success")
    tone_directive = settings.rade_marine_tone_directive if wio.query.persona == "marine" else settings.explanation_tone_directive
    messages=[{"role": "system", "content": _EXPLANATION_SYSTEM.format(
                  lang=lang, max_words=settings.llm_max_words, tone_directive=tone_directive)},
              {"role": "user", "content": _fact_sheet(wio, decision)}]
    tier: Tier = "big" if _requires_big_llm(wio, decision) and is_configured("big") else "small"
    tier_llm = big_llm if tier == "big" else small_llm
    result=await tier_llm(messages, max_tokens=settings.llm_max_words * 4)
    if not result.available:
        # Never fatal — an unconfigured or unreachable model must not cost the user an answer.
        return _result([], "success" if not is_configured(tier) else "partial",
                       errors=[] if not is_configured(tier) else [f"explanation unavailable: {result.error}"])
    text=(result.text or "").strip()
    if not text:
        return _result([], "partial", result.tier, ["explanation model returned empty text"])
    return _result([Claim(claim="explanation", value=text, evidence_ids=panel_ids, confidence=0.7,
                          extra={"derivation": _NOT_MEASURED})], "success", result.tier)

async def run_all_agents(ceos: list[CanonicalEvidenceObject], wio, user_context: dict[str, Any], lang: str = "en", decision=None) -> list[AgentResult]:
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
    explanation = await run_explanation_agent(wio, decision_result, lang)
    reviewer = await run_reviewer_agent([*results, explanation], wio, ceos)
    results.append(reviewer)
    results.append(explanation)
    return results
