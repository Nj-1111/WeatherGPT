"""Fault tolerance and plan-driven filtering in the retrieval layer."""
import asyncio

import httpx
import pytest

from app.orchestrator.retrieval_planner import build_retrieval_plan
from app.schemas.ceo import CanonicalEvidenceObject, Provenance
from app.services.retrieval import CircuitBreaker, _cache_key, _is_retryable, _wanted


def _ceo(variable="temperature_2m", evidence_class="forecast"):
    return CanonicalEvidenceObject(source="OPEN_METEO", evidence_class=evidence_class, variable=variable,
                                   value=1.0, unit="C", statistic="instant",
                                   provenance=Provenance(original_source="test"))


def _response(status):
    request = httpx.Request("GET", "https://example.test")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


@pytest.mark.parametrize("exc,expected", [
    (httpx.TimeoutException("slow"), True),
    (httpx.ConnectError("down"), True),
    (asyncio.TimeoutError(), True),
    (_response(503), True),
    (_response(429), True),
    (_response(404), False),
    (_response(401), False),
    (RuntimeError("IMD_API_KEY missing"), False),
])
def test_only_transient_failures_are_retried(exc, expected):
    assert _is_retryable(exc) is expected


def test_circuit_opens_after_threshold_and_recovers_after_reset():
    breaker = CircuitBreaker(threshold=2, reset_seconds=300)
    assert not breaker.is_open("CAP")
    breaker.record_failure("CAP")
    assert not breaker.is_open("CAP"), "one failure must not open the circuit"
    breaker.record_failure("CAP")
    assert breaker.is_open("CAP")
    breaker.record_success("CAP")
    assert not breaker.is_open("CAP")


def test_circuit_half_opens_once_the_reset_window_passes():
    breaker = CircuitBreaker(threshold=1, reset_seconds=0)
    breaker.record_failure("CAP")
    assert not breaker.is_open("CAP"), "a trial request should be allowed through"


def test_plan_variables_filter_evidence():
    plan = build_retrieval_plan("will it rain tomorrow", "short")
    items = [_ceo("precipitation_amount"), _ceo("temperature_2m"), _ceo("humidity"), _ceo("cloud_cover")]
    kept = {item.variable.value for item in _wanted(items, plan)}
    assert kept == {"precipitation_amount"}, "only planned variables survive"


def test_warnings_are_never_filtered_out_by_the_variable_list():
    plan = build_retrieval_plan("will it rain tomorrow", "short")
    warning = _ceo("heavy_rain_warning", evidence_class="warning")
    assert _wanted([warning], plan) == [warning]


def test_cache_key_groups_nearby_coordinates():
    a = _cache_key("OPEN_METEO", {"lat": 22.7196, "lon": 75.8577})
    b = _cache_key("OPEN_METEO", {"lat": 22.7201, "lon": 75.8583})
    far = _cache_key("OPEN_METEO", {"lat": 28.6139, "lon": 77.2090})
    assert a == b, "one city should share a cache entry against a ~27km source grid"
    assert a != far


def test_substring_keywords_do_not_hijack_the_plan():
    assert build_retrieval_plan("weather in Goa tomorrow", "short").decision_context is None
    assert build_retrieval_plan("mango season rainfall", "short").decision_context is None
    assert build_retrieval_plan("should I spray today", "short").decision_context == "spray"
    assert build_retrieval_plan("driving to Pune tomorrow", "short").decision_context == "travel"


def test_marine_question_selects_marine_variables_and_sources():
    plan = build_retrieval_plan("wave height at Chennai tomorrow", "short")
    assert "wave_height" in plan.variables
    assert "OPEN_METEO_MARINE" in plan.sources
    assert "STORMGLASS" in plan.sources


def test_non_marine_question_does_not_fetch_marine_sources():
    plan = build_retrieval_plan("will it rain tomorrow", "short")
    assert "wave_height" not in plan.variables
    assert "OPEN_METEO_MARINE" not in plan.sources


def test_fishing_decision_also_pulls_marine_data():
    plan = build_retrieval_plan("should I go fishing tomorrow", "short")
    assert plan.decision_context == "marine"
    assert "wave_height" in plan.variables


def test_capability_widens_coverage_beyond_keywords():
    # "safe near the coast" hits no marine keyword at all — only the capability triggers it.
    plan = build_retrieval_plan("is it safe near the coast", "short", capabilities=["marine"])
    assert "wave_height" in plan.variables
    assert "OPEN_METEO_MARINE" in plan.sources


def test_capability_never_removes_a_keyword_trigger():
    plan = build_retrieval_plan("will it rain tomorrow", "short", capabilities=[])
    assert "precipitation_amount" in plan.variables


def test_humidity_pressure_cloud_cover_visibility_capabilities_add_their_variable():
    plan = build_retrieval_plan("general question", "short",
                                capabilities=["humidity", "pressure", "cloud_cover", "visibility"])
    assert "humidity" in plan.variables
    assert "pressure_msl" in plan.variables
    assert "cloud_cover" in plan.variables
    assert "visibility" in plan.variables


def test_heat_stress_capability_pulls_temperature_wind_and_humidity():
    plan = build_retrieval_plan("general question", "short", capabilities=["heat_stress"])
    assert "temperature_2m" in plan.variables
    assert "wind_speed" in plan.variables
    assert "humidity" in plan.variables


def test_extreme_events_capability_pulls_imd_without_warning_keyword():
    plan = build_retrieval_plan("general question", "short", capabilities=["extreme_events"])
    assert plan.need_warnings
    assert "IMD" in plan.sources


def test_travel_safety_guidance_capability_alone_adds_no_variables():
    # Guidance-only capability carries no data of its own; it's a prompt flag, not a fetch trigger.
    baseline = build_retrieval_plan("general question", "short", capabilities=[])
    guided = build_retrieval_plan("general question", "short", capabilities=["travel_safety_guidance"])
    assert set(guided.variables) == set(baseline.variables)
    assert set(guided.sources) == set(baseline.sources)


