"""WeatherGPT modular-monolith API.  Weather truth is assembled before language synthesis."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from app.adapters.http import close_client
from app.adapters.registry import health_all
from app.agents.orchestrator import run_all_agents
from app.config import settings
from app.errors import WeatherGPTError
from app.llm.client import is_configured
from app.logging_config import configure_logging, request_id_var
from app.orchestrator.retrieval_planner import build_retrieval_plan, has_word
from app.rade.v2 import decide
from app.schemas.api import (
    ContextRequest,
    DecisionRequest,
    FeedbackRequest,
    LocationInput,
    LocationOnlyResponse,
    QueryRequestV1,
)
from app.schemas.location import ResolvedLocation
from app.schemas.query import GuardrailAction, GuardrailDecision
from app.services import disambiguation, location_resolver, session_router
from app.services.auth import key_scope, verify_api_key
from app.services.cache import weather_cache
from app.services.evidence_store import evidence_store
from app.services.guardrail import check_question_fast
from app.services.location_resolver import (
    LocationAmbiguousError,
    LocationNotFoundError,
    extract_location,
    resolve_location,
)
from app.services.query_guardrail import (
    render_guardrail_message,
    resolve_confirmed_location,
    run_guardrail,
)
from app.services.rate_limit import RateLimiter
from app.services.retrieval import retrieve
from app.services.semantic_gate import validated_evidence
from app.services.temporal_align import filter_by_window
from app.services.time_parser import parse_time_window
from app.services.wio_builder import build_wio, filter_covered_warnings
from app.storage import memory_store

configure_logging()
logger = logging.getLogger(__name__)
_rate_limiter = RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_per_day)
_UNGATED_PATHS = {"/", "/health", "/api/v1/health"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await close_client()


app = FastAPI(title="WeatherGPT", version="2.0.0",
              description="Evidence-backed weather intelligence", lifespan=lifespan)
_started_at = time.monotonic()
_metrics: dict[str, float] = {"requests": 0, "errors": 0, "wio_latency_ms_total": 0, "rade_latency_ms_total": 0}


def _envelope(status: int, code: str, message: str, request_id: str, headers=None) -> JSONResponse:
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"code": code, "message": message, "details": {}, "request_id": request_id}})


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        request_id_var.set(request_id)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.request_max_bytes:
            return _envelope(413, "REQUEST_TOO_LARGE", "Request exceeds configured size limit", request_id)
        if settings.api_keys and request.url.path not in _UNGATED_PATHS:
            matched_key = verify_api_key(request.headers.get("Authorization"))
            if matched_key is None:
                client = request.client.host if request.client else "unknown"
                logger.warning("request.auth_failed", extra={"client": client, "path": request.url.path})
                return _envelope(401, "UNAUTHORIZED", "Missing or invalid API key", request_id,
                                 headers={"WWW-Authenticate": "Bearer"})
            request.state.api_key_scope = key_scope(matched_key)
        if settings.rate_limit_enabled and request.url.path not in _UNGATED_PATHS:
            client = request.client.host if request.client else "unknown"
            retry_after = _rate_limiter.check(client)
            if retry_after is not None:
                logger.warning("request.rate_limited", extra={"client": client, "path": request.url.path})
                return _envelope(429, "RATE_LIMITED", "Too many requests; retry later.", request_id,
                                 headers={"Retry-After": str(retry_after)})
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = str(round((time.monotonic() - started) * 1000, 1))
        return response


app.add_middleware(RequestIDMiddleware)
if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def _scoped_id(request: Request, raw_id: str) -> str:
    """Prefixes a client-supplied user_id/session_id with the calling key's scope, so one
    caller's sloppy or reused id can never read/overwrite another caller's stored state.
    No-op (raw id, unchanged) when the key gate is off, e.g. in tests/dev."""
    scope = getattr(request.state, "api_key_scope", None)
    return f"{scope}:{raw_id}" if scope else raw_id


def _error(request: Request, error: WeatherGPTError) -> JSONResponse:
    if error.status_code >= 500:
        _metrics["errors"] += 1
    return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code, "message": error.message, "details": error.details, "request_id": request.state.request_id}})


@app.exception_handler(WeatherGPTError)
async def weathergpt_error(request: Request, exc: WeatherGPTError):
    return _error(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return _error(request, WeatherGPTError("VALIDATION_ERROR", "Invalid request", {"errors": exc.errors()}, 422))


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    logger.exception("request.unhandled_error", extra={"path": request.url.path})
    return _error(request, WeatherGPTError("INTERNAL_ERROR", "Internal server error", {}, 500))


async def _resolve_location(location: LocationInput | None, question: str,
                            extracted_phrase: str | None = None,
                            session_id: str | None = None) -> ResolvedLocation:
    try:
        # A half-supplied coordinate used to be ignored, and the caller was then told to
        # "provide a city, pincode, or latitude/longitude" — the thing they had just done.
        # Only raised when nothing else can resolve the request, so a lat sent alongside a
        # place name still resolves by name exactly as before.
        if location and not location.raw and (location.latitude is None) != (location.longitude is None):
            missing = "longitude" if location.longitude is None else "latitude"
            raise WeatherGPTError("LOCATION_INCOMPLETE",
                                  f"Coordinates need both latitude and longitude; {missing} is missing.",
                                  {"missing": missing}, 422)
        if location and location.has_coordinates():
            return ResolvedLocation(raw=location.raw or "coordinates", lat=location.latitude, lon=location.longitude,
                                    confidence=1.0, source="request", normalized_name=location.raw, resolution_method="coordinates")
        if location and location.raw:
            return await resolve_location(location.raw)
        # Prefer the LLM-extracted location phrase (already typo-corrected) over the
        # deterministic regex extractor; fall back to it only when nothing was extracted.
        # Routed through the module attribute (not the name imported into this module)
        # so it resolves dynamically, same as extract_location()'s internal call —
        # tests patch app.services.location_resolver.resolve_location, not this module's.
        if extracted_phrase:
            return await location_resolver.resolve_location(extracted_phrase)
        extracted = await extract_location(question)
        if extracted:
            return extracted
    except LocationAmbiguousError as exc:
        if session_id and settings.follow_up_context_enabled:
            await session_router.store_pending_disambiguation(session_id, question, exc.candidates)
        options = "; ".join(
            f"{i + 1}) {c['name']}" + (f", {c['state']}" if c.get("state") else "")
            for i, c in enumerate(exc.candidates))
        message = f'Multiple locations match "{question}". Did you mean: {options}? Reply with the number or the place name.'
        raise WeatherGPTError("LOCATION_AMBIGUOUS", message, {"candidates": exc.candidates}, 409) from exc
    except LocationNotFoundError as exc:
        raise WeatherGPTError("LOCATION_NOT_FOUND", str(exc), {"raw": exc.raw}, 404) from exc
    raise WeatherGPTError("LOCATION_REQUIRED", "Provide a city, pincode, or latitude/longitude; no location is stored or inferred.", {}, 422)


_GUARDRAIL_ERROR_CODES = {
    GuardrailAction.REJECT_OFF_TOPIC: "QUESTION_REJECTED",
    GuardrailAction.CLARIFY: "CLARIFICATION_NEEDED",
    GuardrailAction.VERIFY: "LOCATION_VERIFICATION_NEEDED",
    GuardrailAction.UNSUPPORTED_TOPIC: "TOPIC_NOT_SUPPORTED",
}
_AFFIRMATION_WORDS = ("yes", "yeah", "yep", "yup", "correct", "right", "haan", "ha", "sahi")


async def _resolve_guardrail_decision(req: QueryRequestV1, request: Request) -> GuardrailDecision | None:
    """Runs before any location/time/retrieval work. None means disabled — callers fall
    back to the original regex-only, no-topic-gate behavior untouched.

    A pending disambiguation (picking between LocationAmbiguousError's candidates) takes
    priority over a pending VERIFY, then a pending VERIFY takes priority over a fresh
    guardrail run — each is checked and consumed in turn so a session can only ever be
    mid-way through one conversational follow-up at a time.
    """
    if not settings.query_understanding_enabled:
        return None
    if req.session_id and settings.follow_up_context_enabled:
        scoped = _scoped_id(request, req.session_id)
        pending_disambiguation = await session_router.consume_pending_disambiguation(scoped)
        if pending_disambiguation is not None:
            original_text, candidates = pending_disambiguation
            matched = await disambiguation.match_candidate(req.question, candidates)
            if matched is not None:
                location_str = (matched["name"]
                                + (f", {matched['state']}" if matched.get("state") else "")
                                + (f", {matched['country']}" if matched.get("country") else ""))
                return resolve_confirmed_location(original_text, location_str)
            # No confident match: drop it and treat this message as a fresh question.
        pending = await session_router.consume_pending_verification(scoped)
        if pending is not None and has_word(req.question.casefold(), _AFFIRMATION_WORDS):
            original_text, candidate = pending
            return resolve_confirmed_location(original_text, candidate)
    return await run_guardrail(req.question)


async def _weather_request(req: QueryRequestV1, request: Request) -> ResolvedLocation | tuple[Any, list, dict[str, Any], list, Any, Any]:
    started = time.monotonic()
    check_question_fast(req.question)
    guard_decision = await _resolve_guardrail_decision(req, request)
    # Caller override takes precedence; otherwise use the guardrail's detection of the
    # question's own language; "en" only if neither is available.
    effective_lang = req.language or (guard_decision.detected_lang if guard_decision else None) or "en"

    if guard_decision is not None and guard_decision.action in _GUARDRAIL_ERROR_CODES:
        if guard_decision.action == GuardrailAction.VERIFY and req.session_id and settings.follow_up_context_enabled:
            await session_router.store_pending_verification(
                _scoped_id(request, req.session_id), guard_decision.original_text, guard_decision.verify_candidate or "")
        message = render_guardrail_message(guard_decision)
        assert message is not None  # guaranteed for every action in _GUARDRAIL_ERROR_CODES
        raise WeatherGPTError(_GUARDRAIL_ERROR_CODES[guard_decision.action], message,
                              {"action": guard_decision.action.value}, 400)

    if guard_decision is not None and guard_decision.action == GuardrailAction.ACCEPT_LOCATION_ONLY:
        # Nothing else runs: no time parsing, no retrieval, no fusion, no agents, no RADE.
        return await _resolve_location(req.location, req.question, guard_decision.location,
                                       session_id=_scoped_id(request, req.session_id) if req.session_id else None)

    # guard_decision is None (disabled) or ACCEPT_WEATHER_FULL — the full pipeline, unchanged.
    # An explicit request-level location always wins, exactly as before — the follow-up
    # short-circuit only ever fires when the caller supplied no location this turn either
    # way (neither `location` nor a location phrase the guardrail found).
    has_explicit_location = bool(req.location and (req.location.raw or req.location.has_coordinates()))
    cached_context = None
    if (req.session_id and guard_decision is not None and not has_explicit_location
            and settings.follow_up_context_enabled):
        cached_context = await session_router.evaluate_follow_up(_scoped_id(request, req.session_id), guard_decision)

    if cached_context is not None:
        location = ResolvedLocation(
            raw=cached_context.resolved_location_name, lat=cached_context.resolved_lat,
            lon=cached_context.resolved_lon, timezone=cached_context.timezone, confidence=1.0,
            source="session_context", normalized_name=cached_context.resolved_location_name,
            resolution_method="follow_up_cache")
        if guard_decision and guard_decision.time:
            # A new time phrase was given even though the location was carried over —
            # re-derive the window rather than silently reusing a stale one.
            valid_from, valid_to, horizon, time_confidence = parse_time_window(
                guard_decision.time, tz=req.timezone or location.timezone)
        else:
            valid_from, valid_to = cached_context.valid_from, cached_context.valid_to
            horizon, time_confidence = cached_context.horizon, cached_context.time_confidence
    else:
        extracted_phrase = guard_decision.location if guard_decision else None
        time_text = (guard_decision.time if guard_decision and guard_decision.time
                    else req.question)
        location = await _resolve_location(req.location, req.question, extracted_phrase,
                                           session_id=_scoped_id(request, req.session_id) if req.session_id else None)
        valid_from, valid_to, horizon, time_confidence = parse_time_window(
            time_text, tz=req.timezone or location.timezone)

    if req.session_id and settings.follow_up_context_enabled:
        await session_router.store_context(_scoped_id(request, req.session_id), location, valid_from, valid_to, horizon, time_confidence)

    plan = build_retrieval_plan(req.question, horizon)
    evidence, retrieval_status = await retrieve(plan, lat=location.lat, lon=location.lon, valid_from=valid_from, valid_to=valid_to)
    evidence = filter_by_window(evidence, valid_from.astimezone(timezone.utc), valid_to.astimezone(timezone.utc))
    evidence, semantic_rejections = validated_evidence(evidence)
    resolved_location = location.model_dump()
    evidence = filter_covered_warnings(evidence, resolved_location)
    evidence_store.add_many(evidence)
    wio = build_wio(req.question, resolved_location, valid_from, valid_to, horizon, evidence, lang=effective_lang)
    wio.query.intent = plan.decision_context or horizon
    wio.query.resolved_location["time_resolution_confidence"] = time_confidence
    wio.query.resolved_location["retrieval_plan"] = plan.model_dump()
    wio.query.resolved_location["retrieval_status"] = retrieval_status
    if semantic_rejections:
        wio.agreement.notes = (wio.agreement.notes + " ").strip() + "Some incompatible evidence was rejected."
    profile = dict(req.profile)
    if req.user_id:
        profile.update({key: value["value"] for key, value in memory_store.get_context(_scoped_id(request, req.user_id)).items() if key not in profile})
    decision = None
    if plan.decision_context:
        decision = decide(wio, profile, getattr(req, "decision_type", None) or req.question)
    agents = await run_all_agents(evidence, wio, profile, wio.query.lang, decision)
    reviewer = next((result for result in agents if result.agent_name == "reviewer"), None)
    if reviewer is None or reviewer.status != "success":
        errors = reviewer.errors if reviewer else ["reviewer agent did not run"]
        raise WeatherGPTError("REVIEW_FAILED", "Evidence-grounding review failed", {"errors": errors}, 503)
    _metrics["wio_latency_ms_total"] += (time.monotonic() - started) * 1000
    return wio, evidence, retrieval_status, agents, profile, decision


def _synthesize(wio, decision=None, agents=None) -> str:
    parts: list[str] = []
    if wio.weather.summary:
        parts.append(wio.weather.summary)
    else:
        parts.append("No compatible weather evidence was available for the requested time window.")
    if wio.official_warning.active:
        parts.append(f"Official {wio.official_warning.severity} warning: {wio.official_warning.event}.")
    if decision:
        parts.append(f"Recommendation: {decision.recommended_action}. {decision.rationale}")
    cited = [e.evidence_id for e in wio.evidence[:3]]
    parts.append(f"Confidence context: {wio.agreement.status}. Backed by {len(wio.evidence)} evidence records"
                 + (f", including {', '.join(cited)}." if cited else "."))
    # Appended last, and only if it survived the reviewer's grounding check.
    explanation = next((claim.value for result in (agents or []) if result.agent_name == "explanation"
                        for claim in result.claims if claim.claim == "explanation"), None)
    if explanation:
        parts.append(str(explanation))
    return " ".join(parts)


@app.get("/")
async def root():
    return {"service": "WeatherGPT", "version": app.version, "openapi": "/openapi.json"}


@app.get("/health")
@app.get("/api/v1/health")
async def health():
    sources = await health_all()
    return {"status": "ok", "liveness": True, "readiness": any(s.get("available") for s in sources.values()),
            "uptime_s": int(time.monotonic() - _started_at), "sources": sources,
            "database": {"available": True, "driver": "sqlite"}, "cache": weather_cache.status(),
            "llm": {"enabled": settings.llm_enabled,
                    "small_tier_configured": is_configured("small"),
                    "big_tier_configured": is_configured("big"),
                    "role": "transport only; it never selects a source or originates a value",
                    "checked": False},
            "models": {"bias_correction": "not wired; responses use raw uncorrected forecast evidence"}}


def _location_only_response(location: ResolvedLocation, request: Request) -> LocationOnlyResponse:
    name = location.normalized_name or location.raw
    return LocationOnlyResponse(answer=f"{name} is at {location.lat}, {location.lon}.",
                                location=location, request_id=request.state.request_id)


@app.post("/wio/query")
@app.post("/api/v1/wio/query")
async def wio_query_v1(req: QueryRequestV1, request: Request):
    _metrics["requests"] += 1
    result = await _weather_request(req, request)
    if isinstance(result, ResolvedLocation):
        return _location_only_response(result, request)
    wio, evidence, retrieval_status, agents, _, _ = result
    return {"wio": wio, "evidence_count": len(evidence), "retrieval": retrieval_status, "agents": agents, "request_id": request.state.request_id}


@app.post("/query")
@app.post("/api/v1/query")
async def query_v1(req: QueryRequestV1, request: Request):
    _metrics["requests"] += 1
    result = await _weather_request(req, request)
    if isinstance(result, ResolvedLocation):
        return _location_only_response(result, request)
    wio, _, retrieval_status, agents, _, decision = result
    return {"answer": _synthesize(wio, decision, agents), "wio": wio, "decision": decision, "agents": agents,
            "retrieval": retrieval_status, "request_id": request.state.request_id}


@app.post("/decision")
@app.post("/rade/advise")
@app.post("/api/v1/decision")
async def decision_endpoint(req: DecisionRequest, request: Request):
    _metrics["requests"] += 1
    started = time.monotonic()
    result = await _weather_request(req, request)
    if isinstance(result, ResolvedLocation):
        return _location_only_response(result, request)
    wio, evidence, retrieval_status, agents, profile, rade_result = result
    if rade_result is None:
        rade_result = decide(wio, profile, req.decision_type or req.question)
    rade_result.evidence_ids = [item.evidence_id for item in evidence]
    _metrics["rade_latency_ms_total"] += (time.monotonic() - started) * 1000
    return {"decision": rade_result, "wio": wio, "agents": agents, "retrieval": retrieval_status, "request_id": request.state.request_id}


@app.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str):
    evidence = evidence_store.get(evidence_id)
    if evidence is None:
        raise WeatherGPTError("EVIDENCE_NOT_FOUND", "Evidence is absent or expired from this process", {"evidence_id": evidence_id}, 404)
    return evidence


@app.post("/context")
@app.post("/api/v1/context")
async def post_context(req: ContextRequest, request: Request):
    memory_store.upsert_fact(_scoped_id(request, req.user_id), req.fact.fact, req.fact.value, req.fact.confidence, req.fact.source, req.fact.confirmed,
                req.fact.expiry.isoformat() if req.fact.expiry else None)
    return {"status": "ok", "user_id": req.user_id, "fact": req.fact.fact}


@app.post("/feedback")
@app.post("/api/v1/feedback")
async def post_feedback(req: FeedbackRequest, request: Request):
    memory_store.add_feedback(_scoped_id(request, req.user_id), req.decision_id or "unspecified", "stored with decision", str(req.actual_outcome), req.user_feedback or "")
    return {"status": "recorded"}


@app.get("/warnings/active")
async def active_warnings(request: Request, location: str, question: str = "warnings today"):
    result = await _weather_request(QueryRequestV1(question=question, location=LocationInput(raw=location)), request)
    if isinstance(result, ResolvedLocation):
        return _location_only_response(result, request)
    wio, _, retrieval, _, _, _ = result
    return {"warnings": [wio.official_warning] if wio.official_warning.active else [], "retrieval": retrieval}


@app.get("/forecast")
async def forecast(request: Request, location: str, question: str = "weather today"):
    result = await _weather_request(QueryRequestV1(question=question, location=LocationInput(raw=location)), request)
    if isinstance(result, ResolvedLocation):
        return _location_only_response(result, request)
    wio, evidence, retrieval, _, _, _ = result
    return {"wio": wio, "retrieval": retrieval, "evidence_count": len(evidence)}


@app.get("/metrics")
async def metrics():
    requests = _metrics["requests"] or 1
    return {**_metrics, "wio_latency_ms_mean": _metrics["wio_latency_ms_total"] / requests,
            "rade_latency_ms_mean": _metrics["rade_latency_ms_total"] / requests, "cache": weather_cache.status()}
