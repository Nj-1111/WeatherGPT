import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.main import app
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance


def _evidence():
    now = datetime.now(timezone.utc) + timedelta(days=1)
    geometry = Geometry(type="Point", coordinates=[79.0882, 21.1458])
    provenance = Provenance(original_source="fixture", transformations=["fixture test"])
    return [
        CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast", variable="precipitation_amount", value=2.0, unit="mm", statistic="accumulation", geometry=geometry, valid_from=now, valid_to=now, accumulation_window_hours=1, provenance=provenance),
        CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast", variable="precipitation_probability", value=.6, probability=.6, unit="probability", statistic="probability", geometry=geometry, valid_from=now, valid_to=now, provenance=provenance),
    ]


async def _post(path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body)


def test_unknown_location_is_structured_error():
    response = asyncio.run(_post("/wio/query", {"question": "weather tomorrow", "location": {"raw": "AtlantisXYZ"}}))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "LOCATION_NOT_FOUND"


def test_wio_is_evidence_backed(monkeypatch):
    async def fake_retrieve(*args, **kwargs):
        return _evidence(), {"sources": {"fixture": {"status": "ok"}}, "partial": False}
    monkeypatch.setattr("app.main.retrieve", fake_retrieve)
    response = asyncio.run(_post("/wio/query", {"question": "Will it rain in Nagpur tomorrow?"}))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["wio"]["evidence"]
    assert data["agents"][-2]["agent_name"] == "reviewer"
    assert data["agents"][-2]["status"] == "success"


def _temperature_only_evidence():
    now = datetime.now(timezone.utc)  # "right now" resolves to today's window, not tomorrow's
    geometry = Geometry(type="Point", coordinates=[79.0882, 21.1458])
    provenance = Provenance(original_source="fixture", transformations=["fixture test"])
    return [CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast",
                                    variable="temperature_2m", value=31.5, unit="C", statistic="instant",
                                    geometry=geometry, valid_from=now, valid_to=now, provenance=provenance)]


def test_temperature_only_answer_is_not_falsely_reported_as_no_evidence(monkeypatch):
    """wio.weather.summary is only ever written by the rain panel — a question that never
    fetches precipitation (no rain keyword) used to make _synthesize claim no evidence
    existed at all, even with a fully populated temperature panel."""
    async def fake_retrieve(*args, **kwargs):
        return _temperature_only_evidence(), {"sources": {"fixture": {"status": "ok"}}, "partial": False}
    monkeypatch.setattr("app.main.retrieve", fake_retrieve)
    response = asyncio.run(_post("/query", {"question": "what is the temperature in Nagpur right now"}))
    assert response.status_code == 200, response.text
    answer = response.json()["answer"]
    assert "No compatible weather evidence" not in answer
    assert "31.5" in answer


def _marine_flow_evidence():
    """A caution-band wave height (RADE recommends 'go' but flags a caution note) plus the
    precipitation amount/probability RADE's scenario generator needs to score anything at
    all — a bare amount with no probability record leaves scenarios empty and RADE defers."""
    now = datetime.now(timezone.utc) + timedelta(days=1)
    geometry = Geometry(type="Point", coordinates=[76.3, 9.9])
    provenance = Provenance(original_source="fixture", transformations=["fixture test"])
    return [
        CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast", variable="precipitation_amount",
                                value=0.0, unit="mm", statistic="accumulation", geometry=geometry,
                                valid_from=now, valid_to=now, accumulation_window_hours=1, provenance=provenance),
        CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast", variable="precipitation_probability",
                                value=0.1, probability=0.1, unit="probability", statistic="probability",
                                geometry=geometry, valid_from=now, valid_to=now, provenance=provenance),
        CanonicalEvidenceObject(source="OPEN_METEO_MARINE", evidence_class="forecast", variable="wave_height",
                                value=1.6, unit="m", statistic="instant", geometry=geometry,
                                valid_from=now, valid_to=now, provenance=provenance),
    ]


def test_two_turn_followup_flow_asks_and_uses_answer(monkeypatch):
    """Turn 1 (fishing, no crew/boat known): a borderline-margin wave height still
    recommends 'go' but appends a clarifying follow-up question (domain-general
    CLARIFYING_FIELDS mechanism, not marine-specific) and stores pending session state.
    Turn 2 ("small boat, four of us", same session, no location repeated): the guardrail
    is not re-invoked (the pending follow-up is consumed instead) and the answer flips the
    recommendation away from 'go'."""
    async def fake_retrieve(*args, **kwargs):
        return _marine_flow_evidence(), {"sources": {"fixture": {"status": "ok"}}, "partial": False}
    monkeypatch.setattr("app.main.retrieve", fake_retrieve)

    guardrail_calls = 0
    import app.main as main_module
    real_run_guardrail = main_module.run_guardrail

    async def counting_run_guardrail(text):
        nonlocal guardrail_calls
        guardrail_calls += 1
        return await real_run_guardrail(text)
    monkeypatch.setattr("app.main.run_guardrail", counting_run_guardrail)

    body_1 = {"question": "should I go fishing tomorrow", "session_id": "s-marine-flow",
              "location": {"latitude": 9.9, "longitude": 76.3}}
    response_1 = asyncio.run(_post("/query", body_1))
    assert response_1.status_code == 200, response_1.text
    data_1 = response_1.json()
    assert data_1["decision"]["recommended_action"] == "go"
    assert "crew" in data_1["answer"] and "boat" in data_1["answer"]
    assert guardrail_calls == 1

    body_2 = {"question": "small boat, four of us", "session_id": "s-marine-flow"}
    response_2 = asyncio.run(_post("/query", body_2))
    assert response_2.status_code == 200, response_2.text
    data_2 = response_2.json()
    assert data_2["decision"]["recommended_action"] != "go"
    assert guardrail_calls == 1  # not re-invoked on turn 2 — the pending follow-up short-circuits it
