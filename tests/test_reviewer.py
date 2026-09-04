"""The anti-hallucination gate: claimed values are recomputed from the evidence they cite."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.agents.orchestrator import (
    run_all_agents,
    run_explanation_agent,
    run_forecast_agent,
    run_reviewer_agent,
)
from app.agents.verification import check_prose_grounding
from app.llm.client import LLMResult
from app.main import app
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance
from app.services.wio_builder import build_wio

START = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=23, minutes=59)
LOCATION = {"lat": 22.72, "lon": 75.86}


def _ceo(variable, value, hour=0, *, unit="mm", window=None, statistic="accumulation",
         probability=None, evidence_class="forecast"):
    valid = START + timedelta(hours=hour)
    return CanonicalEvidenceObject(
        source="OPEN_METEO", evidence_class=evidence_class, variable=variable, value=value, unit=unit,
        statistic=statistic, geometry=Geometry(type="GridCell", coordinates=[75.86, 22.72]),
        issued_at=START, valid_from=valid, valid_to=valid, accumulation_window_hours=window,
        probability=probability, provenance=Provenance(original_source="OPEN_METEO"))


def _evidence():
    """Rain amounts plus a probability record and an m/s wind record.

    The probability CEO lands inside the rain panel's evidence_ids and the wind is in m/s
    while the panel reports km/h — both are places a naive verifier false-fails.
    """
    ceos = [_ceo("precipitation_amount", value, hour, window=1)
            for hour, value in enumerate([0.0, 1.2, 0.8, 0.4])]
    ceos.append(_ceo("precipitation_probability", 0.75, 1, unit="probability",
                     statistic="probability", probability=0.75))
    ceos.append(_ceo("wind_speed", 5.0, 1, unit="m/s", statistic="instant"))
    ceos.append(_ceo("temperature_2m", 31.5, 1, unit="C", statistic="instant"))
    return ceos


def _wio(ceos):
    return build_wio("will it rain in Indore tomorrow", LOCATION, START, END, "short", ceos)


def _review(claims_result, ceos, wio):
    return asyncio.run(run_reviewer_agent([claims_result], wio, ceos))


def test_correct_panel_claims_verify():
    ceos = _evidence()
    wio = _wio(ceos)
    forecast = asyncio.run(run_forecast_agent(wio))
    reviewer = _review(forecast, ceos, wio)
    assert reviewer.status == "success", reviewer.errors
    assert reviewer.errors == []
    # All three panels claimed, including the m/s-sourced wind converted to km/h.
    assert {c.claim for c in forecast.claims} == {"precipitation_amount", "temperature_max", "wind_speed"}
    assert next(c for c in forecast.claims if c.claim == "wind_speed").value == 18.0


def test_tampered_value_is_rejected_even_though_the_citation_is_real():
    ceos = _evidence()
    wio = _wio(ceos)
    forecast = asyncio.run(run_forecast_agent(wio))
    rain = next(c for c in forecast.claims if c.claim == "precipitation_amount")
    assert rain.value == 2.4
    rain.value = 40.0  # same real evidence_ids, fabricated total
    reviewer = _review(forecast, ceos, wio)
    assert reviewer.status == "partial"
    assert any("precipitation_amount" in error and "40.0" in error for error in reviewer.errors)


def test_tampered_maximum_is_rejected():
    ceos = _evidence()
    wio = _wio(ceos)
    forecast = asyncio.run(run_forecast_agent(wio))
    wind = next(c for c in forecast.claims if c.claim == "wind_speed")
    wind.value = 120.0
    reviewer = _review(forecast, ceos, wio)
    assert reviewer.status == "partial"
    assert any("wind_speed" in error for error in reviewer.errors)


def test_unknown_evidence_id_still_fails():
    ceos = _evidence()
    wio = _wio(ceos)
    forecast = asyncio.run(run_forecast_agent(wio))
    forecast.claims[0].evidence_ids = ["not-a-real-id"]
    reviewer = _review(forecast, ceos, wio)
    assert reviewer.status == "partial"
    assert any("unknown evidence" in error for error in reviewer.errors)


def test_claim_without_a_derivation_warns_but_does_not_fail():
    ceos = _evidence()
    wio = _wio(ceos)
    forecast = asyncio.run(run_forecast_agent(wio))
    forecast.claims[0].extra = {}
    reviewer = _review(forecast, ceos, wio)
    assert reviewer.status == "success"
    assert any("declares no derivation" in warning for warning in reviewer.warnings)


def test_rounding_is_not_treated_as_fabrication():
    ceos = _evidence()
    wio = _wio(ceos)
    forecast = asyncio.run(run_forecast_agent(wio))
    temperature = next(c for c in forecast.claims if c.claim == "temperature_max")
    temperature.value = 31.5
    assert _review(forecast, ceos, wio).status == "success"


def test_prose_grounding_accepts_pipeline_numbers_and_rejects_invented_ones():
    wio = _wio(_evidence())
    grounded = "Expect about 2.4 mm of rain with a peak probability near 75%, and a high of 31.5 C."
    assert check_prose_grounding(grounded, wio) == []
    assert check_prose_grounding("Expect around 45 mm of rain.", wio) == ["45 mm"]
    # Rounding a real figure is allowed; inventing one is not.
    assert check_prose_grounding("Roughly 2 mm of rain.", wio) == []


def test_prose_grounding_ignores_bare_numerals():
    wio = _wio(_evidence())
    assert check_prose_grounding("Conditions over the next 24 hours across 3 sources.", wio) == []


def test_explanation_agent_is_inert_without_an_llm():
    wio = _wio(_evidence())
    result = asyncio.run(run_explanation_agent(wio, None))
    assert result.claims == []
    assert result.status == "success"


def _with_llm(monkeypatch, text):
    async def fake_small(messages, **kwargs):
        return LLMResult(tier="small", available=True, text=text, model="test-model", host="test.invalid")
    monkeypatch.setattr("app.agents.orchestrator.small_llm", fake_small)
    monkeypatch.setattr("app.agents.orchestrator.is_configured", lambda tier: True)


def test_ungrounded_explanation_is_suppressed_and_the_request_survives(monkeypatch):
    _with_llm(monkeypatch, "Heavy rain of 45 mm is expected.")
    ceos = _evidence()
    wio = _wio(ceos)
    agents = asyncio.run(run_all_agents(ceos, wio, {}, "en", None))
    explanation = next(a for a in agents if a.agent_name == "explanation")
    reviewer = next(a for a in agents if a.agent_name == "reviewer")
    assert explanation.claims == []
    assert any("45 mm" in error for error in explanation.errors)
    assert reviewer.status == "success"  # suppression is not a reason to reject the answer
    assert any("suppressed" in warning for warning in reviewer.warnings)


def test_grounded_explanation_reaches_the_answer(monkeypatch):
    _with_llm(monkeypatch, "Around 2.4 mm of rain is expected, peaking near 75%.")
    ceos = _evidence()
    wio = _wio(ceos)
    agents = asyncio.run(run_all_agents(ceos, wio, {}, "en", None))
    explanation = next(a for a in agents if a.agent_name == "explanation")
    assert explanation.claims and "2.4 mm" in explanation.claims[0].value
    assert next(a for a in agents if a.agent_name == "reviewer").status == "success"


def test_llm_failure_degrades_to_the_template_answer(monkeypatch):
    async def dead(messages, **kwargs):
        return LLMResult(tier="small", available=False, error="every endpoint failed")
    monkeypatch.setattr("app.agents.orchestrator.small_llm", dead)
    monkeypatch.setattr("app.agents.orchestrator.is_configured", lambda tier: True)
    ceos = _evidence()
    agents = asyncio.run(run_all_agents(ceos, _wio(ceos), {}, "en", None))
    explanation = next(a for a in agents if a.agent_name == "explanation")
    assert explanation.status == "partial" and explanation.claims == []
    assert next(a for a in agents if a.agent_name == "reviewer").status == "success"


async def _post(path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body)


def test_fabricated_value_returns_503_end_to_end(monkeypatch):
    async def fake_retrieve(*args, **kwargs):
        return _evidence(), {"sources": {"fixture": {"status": "ok"}}, "partial": False}

    async def tampering_forecast(wio):
        result = await run_forecast_agent(wio)
        for claim in result.claims:
            if claim.claim == "precipitation_amount":
                claim.value = 40.0
        return result

    monkeypatch.setattr("app.main.retrieve", fake_retrieve)
    monkeypatch.setattr("app.agents.orchestrator.run_forecast_agent", tampering_forecast)
    response = asyncio.run(_post("/wio/query", {"question": "Will it rain in Nagpur tomorrow?"}))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REVIEW_FAILED"
